# Tape Archive Control Application — Framework Document

**Status:** Design spec for development handoff
**Depends on:** `LTO-Archive-VM-Setup.md` (Linux VM with DDA-attached H355e HBA, `mtx`/`ltfs`/`mt` available)

---

## 1. Purpose

Replace manual `mtx`/`ltfs` command-line operation and the ML3 web interface with a single
application that:

1. Controls the library and drives (load/unload/status) reliably via direct SCSI, not the
   ML3's own browser UI.
2. Maintains a permanent, queryable **database of tapes** and **database of tape contents**.
3. Lets someone find "what tape is shot X on" or "what tape is video project Y on" without
   touching a command line.
4. Actively supports **recovery** — verification, integrity tracking, and guided restore —
   not just write-time cataloging.

This is an internal tool for a once-a-year job, but the catalog it builds is a
**permanent record** that must remain trustworthy and queryable for years, independent of
whether this specific app is still running. Design accordingly (see §8, Durability).

---

## 2. Architecture

### 2.1 Where it runs

The control logic (`mtx`, `mt`, `ltfs`, checksum tools) must run **on the Linux VM**, since
that's where the DDA-attached HBA lives. There are two front-end options:

| Option | Description | Verdict |
|---|---|---|
| **Browser app** | Backend API + web UI served from the Linux VM, accessed from any machine on the network via browser | **Recommended** |
| **Windows desktop app** | Native app on a Windows machine, talking to the VM over a network API | Only if there's a strong reason to avoid a browser (there usually isn't for an internal tool) |

**Recommendation: browser-based app, backend and frontend both served from the Linux VM.**
Reasons:
- No install/distribution problem — anyone who needs it opens a URL.
- Keeps all tape-control logic on the one machine that actually has hardware access; no
  need to build and maintain a network protocol between a Windows client and the VM.
- A Windows app would still need to talk to the VM over the network to actually do anything
  (it can't reach `/dev/sg*` remotely) — so it doesn't save you anything architecturally,
  it just adds a client to maintain.

### 2.2 Suggested stack

- **Backend:** Python, FastAPI. Rationale: first-class subprocess handling for wrapping
  `mtx`/`mt`/`ltfs`/`tar`, good SQLite/Postgres support, easy to keep a single person's
  head around a year later.
- **Database: Postgres**, not SQLite. At an archive size of ~500TB, the content catalog
  (see §3.3) is expected to run into the tens of millions of rows even with the two-tier
  approach below; Postgres handles that scale of indexed, searchable data far more
  comfortably than SQLite, and gives better concurrent write performance during large
  annual ingest runs. The trivial-backup argument for SQLite (§8) still applies to Postgres
  via `pg_dump` — it's just one extra command in the backup job, not a real cost.
- **Frontend:** Server-rendered HTML with HTMX (or a small React SPA if the developer
  prefers) — this is an internal ops tool, not a product; favor simplicity and long-term
  maintainability over framework sophistication.
- **Job execution:** Long-running operations (loading a tape, copying data, verifying)
  should run as background jobs with a persisted status/progress record, not synchronous
  HTTP requests — copies of large datasets can run for hours.

---

## 3. Core Feature Set

### 3.1 Library & Drive Control
- Live status dashboard: slot occupancy, barcode per slot (via `mtx status`), which drive
  has which tape loaded, drive read/write activity.
- Load / unload / inventory controls, each a logged action (who, when, what, result).
- Manual "refresh inventory" trigger (wraps `mtx inventory`) for when tapes are swapped
  outside the app.
- **Utility commands** (exposed as explicit, deliberate actions in the UI — not buried near
  routine controls, since these are destructive/maintenance operations):
  - **Format:** wraps `mkltfs` to prepare a new or ex-retired tape for LTFS use. Should
    require confirming the tape's current catalog status is `scratch` (or explicitly
    force-confirming for a tape being repurposed) before running, to guard against
    accidentally reformatting a tape that still has catalogued content on it.
  - **Clean:** triggers a drive cleaning cycle using a cleaning cartridge (load cleaning
    tape into the target drive, run the clean, unload) — wraps the same `mtx`/`mt` sequence
    an operator would run manually, logged the same as any other tape event. Track cleaning
    cartridge usage/remaining life if the cartridge type reports it, since cleaning tapes
    have a limited number of uses.
  - Consider also: **retire** (mark a tape `retired` in the catalog without touching the
    physical tape — for tapes pulled from rotation due to age or errors) and **eject/return
    to slot** as a simple explicit action distinct from unload-during-a-job.

### 3.2 Tape Database
One row per physical tape. Suggested fields:

| Field | Notes |
|---|---|
| Barcode | Primary identifier, read via `mtx status` |
| LTO generation | e.g. LTO-8, LTO-9 |
| Capacity (native/compressed) | |
| Status | `scratch` / `active` / `full` / `archived` / `retired` / `damaged` |
| First written date | |
| Last written date | |
| Last verified date | See §3.4 |
| Total write passes | LTO tapes have a rated head-pass/rewrite life — track it |
| Physical location | Slot number if in-library; shelf/box reference if removed for offsite storage |
| Offsite copy exists? | If you ever duplicate tapes, link to the sibling tape's barcode |
| Notes (free text) | e.g. "read errors observed 2027-03" |

### 3.3 Content Database

This is the part that turns tape barcodes into something actually searchable. Four content
types, with distinct handling — and one important design decision on granularity for
Type A given the archive's ~500TB scale.

**Type A — EXR image sequences (individually zip-compressed frames, not bundled into
per-sequence archives; a single shot folder can hold several thousand frame files)**

At this scale (an estimated 10–40 million individual frame files across a ~500TB archive,
assuming EXR sequences make up the bulk of it), tracking every single frame as its own row
in the primary content table is **impractical for browsing** (nobody wants to scroll
thousands of frame rows to find a shot) and **adds real overhead at write time** (hashing
and inserting tens of millions of rows during an already-large annual copy). It is *not*
impractical from a raw database-scale standpoint — Postgres handles that row count fine —
but it's the wrong primary unit of search.

**Use a two-tier model instead:**
- **Container record (primary, searchable):** one row per shot/sequence *folder* — project,
  sequence, shot, source directory path, frame range (first–last), frame count, any gaps in
  the sequence, total folder size (compressed), which tape + LTFS path. This is what search
  and restore operate on day-to-day.
- **Frame-level manifest (secondary, integrity-only):** a per-folder manifest listing every
  individual frame filename + its SHA256, generated at write time. Store this as a single
  file (CSV/JSON) referenced from the container record — **not** as one DB row per frame.
  Write the same manifest onto the tape alongside the frames, but in a **separate sidecar
  location** (e.g. a parallel `_manifests/` path on the LTFS volume, not inside the actual
  shot/sequence folder) so it is never mixed in with the real frame files. This matters
  because a restore must reproduce the original directory structure exactly for downstream
  systems — a stray manifest JSON landing inside a shot folder could confuse whatever
  pipeline or tool consumes those frames next. The restore workflow (§3.5) must explicitly
  exclude manifest files from what gets copied back; they're for integrity/database use
  only, never part of the restored deliverable.

**Type B — Video files (uncompressed, ~2GB chunks, single folder, logical naming)**
- Record: source video/project name, chunk index, chunk filename, chunk size, SHA256 of
  chunk, which tape + LTFS path. Critically, record **total chunk count for the parent
  video** and which chunks exist on which tape — a single video's chunks could
  theoretically span two tapes if it lands on a tape boundary; the DB needs to make that
  visible rather than hiding it.
- Chunk count per video is small enough (tens, not millions) that per-chunk rows in the
  primary table are fine here — no two-tier treatment needed for this type.

**Type C — Configuration data, settings, and logs**
- Small in volume relative to the rest of the archive; treat as a straightforward
  file-level catalog: source path, filename, size, SHA256, which system/application it came
  from (free-text or a simple tag), which tape + LTFS path.
- No OS images are included in scope — confirm this exclusion stays reflected in whatever
  intake process feeds the archive job, so it doesn't accidentally start pulling in full
  system images later.

**Type D — Audio and other media files**
- Similar treatment to Type C: file-level catalog (source path, filename, size, SHA256,
  project/context tag, tape + LTFS path). If audio files turn out to follow a chunked or
  sequence-like pattern similar to Type A or B, revisit whether they need the same
  container/manifest treatment — but treat as simple file-level records unless that need
  becomes apparent.

**Common to all four types:**
- Write timestamp (when it was archived).
- Checksum recorded **at write time, before the tape is ejected** — this is your only
  reliable guard against silent tape corruption discovered years later.
- Link every content record to exactly one tape barcode + path-on-tape.
- **Source machine (optional field):** the hostname/identifier of the machine the data was
  pulled from. Not all content will have this (general project archive pulls may be
  organized by project rather than by machine), so it must be nullable/optional — but when
  present, it should be filterable/searchable, since it's the key that ties a piece of
  content back to a specific whole-drive pull (see below).
- **Backup category:** a simple tag distinguishing `project_archive` (the general,
  organized archive) from `machine_drive_backup` (a full pull of an individual machine's
  data drive). These two together are what account for the ~500TB total — the machine
  drive pulls substantially overlap with the general archive's content, so this tag lets
  the catalog (and any future storage-reduction effort) distinguish "the organized copy"
  from "the raw drive image copy" rather than treating the whole archive as one
  undifferentiated mass. It also explains, when someone looks at the catalog later, why the
  same-looking content appears to exist twice.

### 3.4 Verification & Integrity (Recovery-Critical)

This is the section worth investing in, since it's what makes "recovery" actually work
rather than just "hope the tape reads fine in 5 years":

- **Write-time verification:** after copying to LTFS, read back and re-hash before
  considering the write complete. Flag mismatches immediately rather than discovering them
  during a later restore attempt.
- **Scheduled spot-verification:** since this is annual, the app should track "last
  verified date" per tape and surface a reminder ("Tape ABC123 hasn't been verified since
  2024 — pull it for a spot check") each year, even for tapes not being written to this
  cycle. Catching degradation early means you still have time to re-archive from a live
  source, which won't be true forever.
- **Verification just needs to re-read and re-hash a sample (or all) of a tape's LTFS
  content and compare against the stored SHA256 — no need to touch the source data again.**
- **Read error logging:** capture and store any SCSI/tape read errors surfaced by `mt`/`dd`
  during verification, tied to the specific tape record, so a pattern of degrading reads on
  one physical tape is visible over time rather than being a one-off surprise.

### 3.5 Search & Restore Workflow
- Search by project/shot name, video name, source machine, date range, backup category, or
  tape barcode.
- Search result shows: which tape(s), which slot (if in-library) or storage location (if
  offsite), and the exact LTFS path.
- **"Prepare restore" action:** given a selected set of content, generate the exact
  sequence of `mtx load` / `ltfs mount` / `cp` commands (or have the app run them directly)
  needed to pull that content back — this turns "recover shot X" from a manual archaeology
  exercise into a guided, auditable action.
- **Manifest files must never appear in restored output.** The restore process copies only
  the actual content files (frames, video chunks, config/audio files) back into the
  original directory structure — the sidecar frame-level manifests (§3.3) are excluded by
  default, unconditionally, from anything written back to a destination a downstream system
  will consume. If a manifest is ever needed for inspection (e.g. to manually verify a
  restored sequence), it should require a separate explicit action, not be a byproduct of a
  normal restore.
- For Type B (chunked video), and for any content that has been split automatically across
  tapes at write time (§3.5a), the restore workflow should identify every tape a given piece
  of content spans and sequence the loads accordingly, warning if any required tape is
  missing, offsite, or flagged as damaged before starting.

### 3.5a Multi-Tape Spanning and the "Greedy" Write Mode

**Automatic spanning (default behavior):** when a piece of content (a shot folder, a video's
chunk set, a machine's drive backup) is larger than the space remaining on the current tape,
the write job should automatically split it at a safe boundary (e.g. between whole frames or
whole chunks, never mid-file), continue the write on the next available tape, and record the
resulting multi-tape span accurately in the catalog (each affected `sequence_containers` /
`content_items` row should be able to reference more than one tape, or be represented as
multiple linked rows if the schema makes that simpler). This should require no manual
intervention beyond having enough scratch tapes available in the library.

**Greedy mode (optional, per-job):** when enabled and given a specific source machine (or
project) to write, the job restricts itself to content from that source only — it will not
begin filling a tape with the next source's data, even if the current tape has space left
over, and it will not span that source's content onto a tape shared with a different
source's data. The tape is left partially full rather than mixed. This trades tape
efficiency for a clean, isolated recovery unit: a tape (or a known, contiguous set of tapes)
containing exactly one machine's backup, nothing else, making a single-machine restore
simpler and reducing the number of tapes that need to be handled/loaded for that recovery.

Both modes should be selectable per write job (e.g. "archive everything, pack tapes fully"
vs. "archive Machine-07's drive, greedy") rather than being a global setting, since a given
annual run will likely use both — greedy for individual machine drive pulls, standard
spanning for the general project archive.

### 3.6 CSV Export
- Per-tape export: full content listing for a single tape (useful for a physical label /
  insert, or handing a specific tape to someone).
- Full-catalog export: every content row across every tape, for search-in-a-spreadsheet
  use, and as an offline backup of the catalog itself (see §8).
- Export should include checksums, so the CSV itself can double as an integrity manifest
  independent of the app/database. For Type A (EXR sequences), the per-tape export should
  reference/include the frame-level manifests (§3.3) alongside the container-level rows, so
  the export is a complete integrity record, not just the folder-level summary.

### 3.7 Audit Log
- Every load, unload, write, verify, and restore action recorded with timestamp and
  outcome. Given this is a once-a-year touch, a clear log of "what actually happened during
  the 2027 run" is valuable when someone's trying to reconstruct what happened during next
  year's run.

---

## 4. Suggested Data Model (Simplified)

```
tapes
  id, barcode, lto_generation, status, first_written_at, last_written_at,
  last_verified_at, write_pass_count, physical_location, offsite_pair_tape_id, notes

sequence_containers     -- Type A primary unit: one row per shot/sequence folder
  id, project_name, sequence_name, shot_name, source_path,
  source_machine,        -- optional; nullable
  backup_category ('project_archive' | 'machine_drive_backup'),
  frame_start, frame_end, frame_count, has_gaps, total_size_bytes,
  manifest_ref,          -- pointer/path to the per-frame checksum manifest file
  written_at, verified_at

content_items           -- Type B (video chunks), Type C (config/logs), Type D (audio/media)
  id, content_type ('video_chunk' | 'config' | 'audio_media'),
  project_name, video_name, chunk_index, total_chunks,   -- used for video_chunk
  source_path, source_machine,        -- optional; nullable
  backup_category ('project_archive' | 'machine_drive_backup'),
  file_size_bytes, sha256, written_at, verified_at

content_tape_spans      -- join table: which tape(s) hold which piece of content
  id, tape_id, ltfs_path,
  sequence_container_id, content_item_id,   -- exactly one of these two set per row
  byte_range_start, byte_range_end          -- used only when a single item is split across tapes

tape_events
  id, tape_id, event_type ('load'|'unload'|'write'|'verify'|'inventory'|'format'|'clean'),
  slot_number, drive_number, initiated_by, started_at, finished_at,
  result, error_detail

restore_requests
  id, requested_by, requested_at, sequence_container_ids (list),
  content_item_ids (list), status, fulfilled_at, notes
```

Note: `content_tape_spans` replaces a single `tape_id` column on the content tables so that
automatically-split content (§3.5a) can reference more than one tape cleanly, rather than
forcing a piece of content to pick one "primary" tape and hide the rest.
Note: `sequence_containers` is kept as a separate table from `content_items` deliberately —
it's the two-tier design from §3.3 in schema form. Frame-level checksums for Type A live in
the external manifest referenced by `manifest_ref`, not as rows in either table.

---

## 5. Recovery-Oriented Features (Summary — Priority List)

Given recovery is a named goal, prioritize these in this order if scope needs trimming:

1. **Write-time checksum + verify** — non-negotiable; this is the actual insurance policy.
2. **Content search → restore workflow, with manifest exclusion enforced** — the main
   day-to-day value of the system, and restoring a manifest file into a downstream pipeline
   by mistake is the kind of bug that's easy to miss until it causes real damage.
3. **Automatic cross-tape spanning** — required given the archive size; without it, large
   sequences/machine backups simply can't be written reliably.
4. **CSV export with checksums** — cheap to build, and the fallback if the DB itself is ever
   lost (see §8).
5. **Scheduled re-verification reminders** — cheap to build, catches degrading tapes early.
6. **Greedy write mode** — valuable for machine-drive-backup isolation, but can follow after
   automatic spanning is solid, since greedy is really a constraint on top of the same
   spanning logic.
7. **Read-error history per tape** — nice-to-have trend visibility, lower urgency.

---

## 6. Non-Functional Requirements

- **Long idle tolerance:** the app will sit untouched for ~11 months at a time. Avoid
  dependencies that require frequent updates to keep working (pin versions; document exact
  setup steps in a runbook alongside the code).
- **Single-operator auth is fine** — this doesn't need enterprise SSO. A single shared
  login or even no auth (if the VM is already network-isolated to trusted machines) is
  proportionate. Don't over-build this.
- **Backups of the app's own database** are essential — see §8.
- **Idempotent operations** — if a copy or verify job is interrupted (network blip, VM
  restart), re-running it should be safe, not create duplicate content records.

---

## 7. Suggested Build Phases

1. **Phase 1 — Core control:** library/drive status dashboard, load/unload, tape DB (no
   content DB yet). Gets you off the ML3 web UI immediately.
2. **Phase 2 — Content cataloging:** write-time content recording for both EXR and video
   types, search UI, CSV export.
3. **Phase 3 — Verification & restore:** checksum verification, scheduled re-verify
   reminders, guided restore workflow.
4. **Phase 4 — Polish:** read-error history, offsite-pair tracking, audit log UI.

---

## 8. Durability of the Catalog (Important)

The tape database is arguably more valuable than any single tape — it's the only thing
that makes the archive searchable at all. It must survive independently of this
application:

- Automated **database backup** (even just a scheduled file copy of the SQLite file, or a
  `pg_dump` if Postgres) to a location separate from the VM itself — e.g. the same network
  share the source data lives on, or wherever the organization already backs up critical
  files.
- The **full-catalog CSV export (§3.6)** should be generated automatically at the end of
  every annual run and stored alongside the database backup — if the database is ever lost
  or corrupted, the CSV is a plain-text fallback that still tells you what's on which tape,
  readable by anyone with a spreadsheet, no app required.
- Consider writing a copy of the relevant CSV export (per-tape content listing) **onto the
  tape itself** as a small text file, so a tape found without any surrounding context still
  carries its own manifest.
