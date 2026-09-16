# Plan — tape_import / job-worker performance & fast-scan

Written during the `format-job-and-tape-targeting` debugging session; see
`SESSION_NOTES_tape_and_connections.md` for the connection/tape bug-fix
history this grew out of (job #45, a full-tape import of `AB26001L`, ~17TB /
375,423 files, is what surfaced all of this).

**Status:** items 1, 2, 3, and 5 are implemented (see item sections below).
Item 4 was left aside since item 5 covers the same integrity-vs-speed
tradeoff more explicitly.

## Background / findings

- **The read is the cost, not the hash.** `_import_sequence`/`_import_file`
  in `app/services/tape_import.py` call `sha256_file()`
  (`app/services/checksums.py`) on every file — a full byte-for-byte read.
  SHA256 itself is cheap relative to what one LTO-9 drive can even deliver
  (~300–400MB/s native ceiling); the CPU we observe during a long import is
  mostly hashing keeping pace with tape throughput, not hashing being the
  bottleneck. For a ~17TB tape this is fundamentally "read the whole tape off
  one physical drive" — no code change removes that ceiling, only the
  overhead layered on top of it.

- **Listing (files/folders/sizes) is metadata-only and doesn't touch tape
  media.** LTFS keeps its whole index in memory once a tape is mounted, so
  `scan_source()` (walk + stat + regex, no file content read) only touches
  that in-memory index. This is why a "list only, no hash" scan should be
  seconds-to-minutes rather than hours, even at 375K files.

- **The job worker is single-threaded and strictly serial.** `JobWorker` in
  `app/jobs/worker.py` runs exactly one job at a time regardless of which
  drive/resource each job needs. Confirmed live: a `write` job queued behind
  a long `tape_import` sat `queued` for hours even though the import was on a
  different drive and the two jobs share no hardware. This is a queue-level
  problem, not a hardware contention problem — the changer has 2 independent
  drives.

- **Related, since fixed** (was "already deferred" — resolved as part of
  landing item 3): `_run_job()` holding one DB session/transaction open for
  the entire job, confirmed via `pg_stat_activity` showing a connection
  "idle in transaction" for the full duration of job #45. Each of
  `tape_import`, `run_verify`, `run_restore`, and `run_write` now commits
  periodically (throttled, or right after the row-creating flush) instead
  of holding one transaction across the whole job's slow work — see item 3.

## Optimization items, roughly in recommended order

### 1. Progress reporting during scan+hash (cheap, do first) — ✅ done

No `progress()` call happens between "mounting" and "unloading" in
`_import_one_tape` — a multi-hour job looks completely frozen in the UI the
whole time. Emit progress every N files or every N bytes processed. Doesn't
change total runtime, but fixes the "is this stuck?" question for free.

**Effort:** small. **Risk:** low. **Depends on:** nothing.

**Implemented:** `_import_one_tape` now emits a progress update every ~5s
(or on the last unit) while scanning+hashing. Found and fixed a real bug
along the way: the progress callback opens its own DB session to write the
`Job` row, which deadlocked against the tape's own still-open catalog
transaction on SQLite (single-writer lock) — fixed by committing that
transaction right before invoking the callback. Covered by
`tests/test_tape_import.py::test_import_reports_intra_tape_scan_progress`.

### 2. Cut redundant stat calls in `scan_source` — ✅ done

`app/services/intake.py`'s per-file loop calls `fpath.is_file()`,
`fpath.is_symlink()`, and `fpath.stat()` — three separate FUSE round-trips
per file where one `os.stat()`/`os.lstat()` would do (derive is_file /
is_symlink from the single stat result). Only shaves the metadata-walk
phase, which is already the cheap phase relative to hashing — modest win on
its own, but directly relevant to item 5 (fast scan) since that mode's
*entire* cost becomes this walk.

**Effort:** small. **Risk:** low. **Depends on:** nothing.

**Implemented:** one `os.lstat()` per file now, reused for is-regular /
is-symlink / size everywhere (some files used to cost up to 5 round-trips,
in the leftovers loop's second pass). Covered by
`tests/test_intake.py::test_scan_does_exactly_one_stat_per_file` (asserts
the exact call count) plus symlink-skip and vanished-file regression tests.

### 3. Concurrent job execution keyed by drive — ✅ done

The real fix for "can a slow import block a write/restore" — yes, today,
confirmed live. Since the two drives are physically independent, the worker
could run one job per drive concurrently instead of one job globally.

Biggest-impact item on the list, and also the biggest change:
- Must fix the per-job DB session lifetime issue first (see "Related,
  already deferred" above) — concurrent long jobs would otherwise pin
  multiple idle-in-transaction connections and exhaust the pool faster.
- `_arm_lock` around the physical changer already exists and should still be
  correct under concurrency (it's scoped to the load/unload robot arm
  action, not the whole job) — but wants a deliberate re-check once jobs can
  actually overlap, not just an assumption it's fine.
- Need to decide how job claiming maps to drives — a job like `write` or
  `verify` names an explicit `drive` param already; `tape_import` claims
  whichever drives are free at start. The claim step in
  `JobWorker._claim_next()` would need to reserve a drive, not just a job.

**Effort:** medium-large. **Risk:** medium (concurrency correctness).
**Depends on:** the DB session lifetime fix.

**Implemented**, as a sequence of small commits (mirroring items 1/2/5's
cadence) rather than one large change — full detail in each commit message,
summarized here:
1. `SimulatedHardware` gained an internal `RLock` (must be reentrant —
   `unload()`/`clean_drive()` call back into `load()`/`unmount_ltfs()` on
   the same thread) around every method touching its shared in-memory
   state, previously safe only because one worker thread serialized every
   hardware call.
2. The changer-arm lock, previously two *separate* `Lock()` objects in
   `batch_format.py`/`tape_import.py` that never actually contended against
   each other, was unified into one lock inside `app.services.library`'s
   `load_tape`/`unload_tape`/`clean_drive` — which also, for free, gave
   `write`/`verify`/`restore` arm locking they never had at all before.
   Landing this exposed a real TOCTOU race: every caller resolved "any free
   slot" for `unload_tape` *before* calling it, which used to be safe only
   because that resolution happened inside the same caller-side lock — once
   the lock moved inside `unload_tape` itself, two drives unloading at once
   could both see the same slot as free. Fixed by resolving "any free slot"
   *inside* `unload_tape` (`slot=None`), atomically, under its own lock.
3. `run_verify`'s per-check loop got the same commit-before-progress fix
   already applied to `tape_import` (item 1).
4. `run_restore`'s per-file loop got the analogous fix — no deadlock to
   reproduce under SQLite here specifically (`_restore_one` never writes to
   the DB), but it still bounds how long the session's transaction/snapshot
   stays open, which is what actually matters once jobs run concurrently
   against a shared connection pool.
5. `app/jobs/drives.py`: the in-process drive-reservation registry —
   `drive_need(job)` maps each JobType to `ExactDrives` (write/verify/
   format, from `job.params["drive"]`), `AnyDrives` (restore: auto-picks
   one free drive, since restore never had a drive param at all before;
   batch_format/tape_import: up to as many drives as barcodes), or
   `NoDrive` (backup). Every claim reconciles against the *physical*
   hardware state, not just its own bookkeeping.
6. `JobWorker` restructured: each poll tick scans *every* queued job and
   starts whichever ones are currently claimable, each in its own thread,
   instead of claiming and running exactly one job at a time.
   `run_pending_jobs_inline()` (the test/CLI helper) rewritten to match —
   alternates "start everything startable" with "wait for something to
   finish" until the queue and in-flight set are both empty.

   Two more real bugs surfaced only once actual concurrent execution was
   exercised (both caught by tests before landing, not after):
   - `_scan_and_start()`'s first draft started each job's thread *while
     still holding that job's own claim-and-flip transaction open* —
     the same deadlock shape fixed in items 1–4, newly possible here since
     starting a thread now happens mid-scan instead of after a single claim.
     Fixed by giving each job's claim its own short transaction, committed
     before that job's thread starts.
   - `run_write`'s per-placement DB write (`_upsert_sequence`/`_upsert_item`
     flushing, then holding that transaction open through the whole
     copy+hash loop) meant two write jobs on *different* drives still ran
     fully serially on SQLite's single-writer lock — confirmed live with a
     concurrency-probing test before fixing it the same way as items 1–4:
     commit right after the flush, before the slow work begins.
   - `batch_format`/`tape_import` discover their own drives internally via
     `_free_drives()` (physical hardware state) — which can't tell "free"
     from "reserved by a different concurrently-running job that hasn't
     loaded a tape onto it yet". A batch_format job given drive 1 by the
     registry would still see drive 0 as physically free (if the other job
     hadn't loaded onto it yet) and grab that instead — a real hardware
     conflict, not just a test artifact. Fixed by threading the registry's
     actual claimed set into both functions (`claimed_drives` param, used
     when the call came through the worker; falls back to the old
     `_free_drives()` discovery for direct/test callers).

   Fairness/starvation for the scan-all-queued model is an accepted,
   undefended tradeoff at this app's scale (two drives, one operator) —
   not engineered around.

Tests: `tests/test_hardware_simulator.py`, `tests/test_library_arm_lock.py`,
the incremental-commit tests added to `test_pipeline.py`/`test_tape_import.py`,
`tests/test_jobs_drives.py`, and `tests/test_jobs_worker.py` (concurrency
proven via a shared-counter probe wrapping a slowed-down step, not
wall-clock timing — asserts genuine overlap for different-drive jobs and
zero overlap for same-drive/arm-lock cases). Full suite stress-run 15x
consecutively with no flakes before landing.

### 4. Policy lever: full-hash vs. sampled verify on import

`DEFAULT_VERIFY_SAMPLE_FRACTION` already exists for periodic *re*-verify of
tapes already in the catalog. `tape_import` (first-time cataloging of
pre-existing legacy tapes) always does 100% hashing, deliberately, since
it's the trust anchor for content the app has never seen before. Could make
this configurable (fast sampled import now, full verify scheduled for
later), but that's a real integrity-vs-speed tradeoff to make deliberately —
not a free win, and possibly superseded by item 5 below (which sidesteps the
question by not hashing *at all* on the fast path and treating it explicitly
as "not yet audited" rather than "sample-verified").

**Effort:** small-medium. **Risk:** low (it's opt-in). **Depends on:**
deciding whether item 5 replaces the need for this.

### 5. Fast-scan mode: list files/folders + capacity, no hashing — ✅ done

New idea from this session. A `tape_import` mode that walks the tape
(`scan_source()`, same as today) and records catalog entries **without**
calling `sha256_file()` at all — purely "here's what's on this tape, here's
how much space is used/free." Answers "is this tape worth a full audit?"
without paying the full-tape-read cost.

Design sketch:
- `run_tape_import` gets a mode flag (e.g. `verify: bool = True`) —
  `False` skips the `sha256_file()` calls in `_import_sequence`/
  `_import_file` entirely.
- Catalog entries from a fast scan get `verified_at = None` / no sha256.
  The existing idempotent-skip check (`if existing and existing.verified_at:
  return None`) already only skips *verified* entries, so a later full
  import/verify naturally re-processes anything a fast scan only listed —
  no special-casing needed there.
- Record `os.statvfs(mount)`-derived used/free space against the tape once
  mounted. Two uses:
  - Directly answers "is this worth auditing" (e.g. "80% full, 300K files,
    never hashed").
  - Corrects `capacity_native_bytes` from the guessed default (currently
    `DEFAULT_TAPE_CAPACITY_BYTES`, 18TB for LTO-9 on this VM) to the
    *measured* value for that specific cartridge, which can vary
    tape-to-tape (format overhead, partial writes).
- **Data model gap to resolve:** need something to distinguish "listed but
  never hash-verified" from "fully verified" at the *tape* level (not just
  per-span — `ContentTapeSpan.verified_at` already covers per-file), so the
  Tapes page can show it plainly. Candidate: a `last_scanned_at` column
  alongside the existing `last_verified_at`. One-column migration, small.
- UI: a mode choice on the tape_import form ("Fast scan" vs "Full import +
  verify").

**Effort:** medium. **Risk:** low-medium (mostly additive; the data-model
column is the only schema change). **Depends on:** nothing else on this
list, could be built independently and first if desired — it doesn't touch
the job-worker concurrency model at all, just what one `tape_import` run
does.

**Implemented:** `run_tape_import(..., verify: bool = True)`, threaded
through `_import_one_tape`/`_import_sequence`/`_import_file` — `verify=False`
skips all `sha256_file()` calls, and spans/containers from that pass keep
`verified_at=None`. `Tape.last_scanned_at` (migration `bfc167b72cb8`) is set
on every import; `last_verified_at` only on a full (hashing) pass, so the
Tapes list/detail pages can show "verified" vs "scanned only" vs "never
audited". `_measure_capacity_bytes()` corrects `capacity_native_bytes` from
`os.statvfs(mount)` — gated to `HARDWARE_BACKEND=real` only, since the
simulator's "mount" is a plain host directory and `statvfs()` on it would
report the *host's* free space, not a tape's. UI: a Mode selector (Full
import + verify / Fast scan) on the tape_import form. Covered by
`tests/test_tape_import.py` (`test_fast_scan_lists_content_without_hashing_it`,
`test_a_later_full_import_hashes_content_a_fast_scan_only_listed`,
`test_import_never_overwrites_capacity_in_simulator_mode`,
`test_measure_capacity_bytes_only_applies_on_real_hardware`).

## Open questions before implementing

(Resolved for items 1/2/5 above — `last_scanned_at` was the chosen shape,
and item 5 shipped independently of item 3. Still open for item 3/4:)

1. ~~Item 5's data-model gap — is `last_scanned_at` the right shape, or would
   you rather model "audit-worthiness" more explicitly (e.g. a computed
   status like `scanned` / `verified` / `stale`)?~~ Resolved: plain
   `last_scanned_at` column, shipped.
2. ~~Item 3 (concurrency) is the biggest lift — worth scoping as its own
   follow-up plan once the DB session fix lands, rather than bundling into
   this pass?~~ Resolved: yes, own follow-up plan when picked up.
3. ~~Priority order: is unblocking write/restore from long imports (item 3) or
   shipping the fast-scan/triage feature (item 5) the more urgent of the two
   bigger items?~~ Resolved: item 5 first, shipped. Item 3 (and whether
   item 4 is still worth doing on its own) remain open for whenever this is
   picked back up.

## New finding, post item-3: transient tape-changer arm errors — no retry, and cleanup failures are inconsistently visible

Found testing the write-path fix live, after items 1–5 above had already
landed: a fully successful write job (data written, checksummed, catalogued
— tape correctly flipped to `active` with the right `used_bytes`) still
ended with the post-write robot-arm unload (`mtx unload`, returning the tape
from the drive to a storage slot) failing once with a real but transient
SCSI error (`Illegal Request`, sense 53/03 — `MOVE MEDIUM` momentarily
rejected). Retrying the *exact same* `mtx unload` command by hand seconds
later succeeded immediately — confirmed transient, not a persistent
hardware fault or a code bug, and not something introduced by item 3's
arm-lock work.

Checked how each caller currently handles a failed cleanup unload. Item 3
already centralized the actual arm call in `library.py`'s
`load_tape`/`unload_tape`, and it always logs a `TapeEvent` with
`EventResult.error` before re-raising `HardwareError`, so every failure at
least lands in the audit trail — but what happens above that point still
varies by caller:

- `writer.py::_Drive.release()` and `verification.py`'s equivalent cleanup:
  `except HardwareError: pass` — silently swallowed, nothing on the job
  result. A job can report `completed` while a tape is still physically
  sitting in a drive, un-returned, with the only signal being a separate
  `TapeEvent` row an operator would have to think to go check.
- `restore.py`: same silent-swallow shape.
- `tape_import.py` / `batch_format.py`: already better — catch
  `HardwareError` and fold it into the job's own failure/error message
  (`"...; also failed to unload: {exc}"`), so at least the job itself shows
  something happened.

Proposed fix:
- Add a small bounded retry (e.g. 2 attempts, short delay between) around
  the actual `hw.load()`/`hw.unload()` SCSI call inside
  `library.py::load_tape`/`unload_tape` — one central place rather than
  patching every caller, and covers both directions since a transient
  `MOVE MEDIUM` failure isn't obviously load-only or unload-only.
- After retries are exhausted, keep raising `HardwareError` (unchanged
  contract) — but standardize what callers do with it: every cleanup site
  (`writer.py`, `verification.py`, `restore.py`, matching what
  `tape_import.py`/`batch_format.py` already do) should attach a warning to
  the job's own result/error when a post-work cleanup unload fails, not
  swallow it silently. The archival work itself succeeding should still let
  the job report success — this is about not hiding "a tape needs manual
  attention" behind a job that otherwise looks perfectly fine.

**Effort:** small-medium (retry is a small change in one place; auditing
4 call sites for consistent visibility is the rest of the work).
**Risk:** low. **Depends on:** nothing — orthogonal to items 1–5, safe to
pick up independently.
