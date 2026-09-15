# Plan — tape_import / job-worker performance & fast-scan

Written during the `format-job-and-tape-targeting` debugging session; see
`SESSION_NOTES_tape_and_connections.md` for the connection/tape bug-fix
history this grew out of (job #45, a full-tape import of `AB26001L`, ~17TB /
375,423 files, is what surfaced all of this).

**Status:** items 1, 2, and 5 are implemented (see item sections below).
Items 3 and 4 are still just planned — item 3 needs its own follow-up plan
(the DB session lifetime fix has to land first); item 4 was left aside since
item 5 covers the same integrity-vs-speed tradeoff more explicitly.

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

- **Related, already deferred**: `_run_job()` holds one DB session/transaction
  open for the entire job (including long blocking hardware/filesystem
  work), confirmed via `pg_stat_activity` showing a connection "idle in
  transaction" for the full duration of job #45. Tracked separately, not
  part of this doc, but relevant to item 3 below — going concurrent without
  fixing this first makes it worse (multiple long-lived idle transactions
  stacking up against a small connection pool).

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

### 3. Concurrent job execution keyed by drive

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
