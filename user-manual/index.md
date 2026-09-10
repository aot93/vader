# Vader — Tape Archive Control: User Manual

**What this covers:** an operator's guide to running Vader — the web application
that controls the LTO tape library, records what is written to every tape, and
guides verification and restore. If you are setting the machine up for the first
time, start with [Getting started](getting-started.md); if the archive VM is
already built, jump to *Daily operation at a glance* below.

Vader replaces hand-run `mtx` / `ltfs` commands and the ML3 changer web page with
one screen per task, and — more importantly — it builds a **permanent catalog**
of what is on which tape. That catalog is meant to stay readable for years, with
or without this application (see [Exports & durability](exports-durability.md)).

This manual describes the application **as it actually behaves in the code**. Where
the original framework design document and the running software differ, the manual
follows the software and notes the difference.

The manual is also available inside the app: the **Help** link in the top bar
renders these same Markdown files live, so what you read in-app always matches the
version deployed.

---

## Who this manual is for

The archive operator — the person who, once a year, loads tapes, starts the write
run, and later needs to find and restore specific shots or files. No programming
knowledge is assumed. Command-line steps appear only for the initial VM setup,
which is covered in the project `RUNBOOK.md` and only summarised here.

---

## Every page

| Page | What it gives you |
|---|---|
| [Getting started](getting-started.md) | Install, environment variables, first run, login. Brief — links to `RUNBOOK.md` for the VM build. |
| [Dashboard](dashboard.md) | The landing screen: tape counts, catalog totals, library map, jobs in flight, re-verification reminders. |
| [Library & drive control](library.md) | Refresh inventory, load / unload, and the Utilities: Format (scratch), Clean drive, Retire tape. |
| [Tapes](tapes.md) | The tape list and tape detail page: statuses, capacity, location, offsite pairing, "dedicated to" (greedy), notes, per-tape contents, event and read-error history. |
| [Archiving](archiving.md) | **Primary chapter.** Preparing a source tree, content classification (Types A–D), starting a write job, standard vs greedy mode, tape spanning, write-time checksums and read-back verify, idempotent re-runs, monitoring, on-tape catalog and manifests. |
| [Verification](verification.md) | Running a verify job, sample vs full, the re-verification-due list, reading read-error history, responding to a mismatch. |
| [Restore](restore.md) | **Primary chapter.** Searching, selecting content, the prepare-restore plan (load order, command preview, blocking warnings), running the restore, multi-tape and byte-split reassembly, manifest exclusion. |
| [Search the catalog](search-catalog.md) | Every search filter, how results map to tapes and slots, exporting results. |
| [Jobs](jobs.md) | The background job system: types, the one-at-a-time worker, progress and heartbeat, cancel, retry, interrupted-on-restart behaviour. |
| [Exports & durability](exports-durability.md) | Catalog CSV export, the database backup job, on-tape `/_catalog/` and `/_manifests/`, recovering the catalog itself. |
| [Audit log](audit-log.md) | What is recorded, and how to read the two logs. |
| [Configuration reference](configuration-reference.md) | Every environment variable / setting, with its default and meaning. |
| [Troubleshooting](troubleshooting.md) | Out of scratch tapes, drive needs cleaning, tape not in library, job stuck or interrupted, database locked, verification mismatch. |

---

## Daily operation at a glance

This is the short path through a normal annual run. Each step links to the page
with the detail.

1. **Open the [Dashboard](dashboard.md).** Confirm the hardware backend is the one
   you expect (`real` on the archive VM) and that the library map shows your
   drives and slots. If it says *Library unreachable*, stop and fix that first
   ([Troubleshooting](troubleshooting.md)).
2. **[Library](library.md) → Refresh inventory.** Check the slot map matches the
   physical library. Any barcode Vader has never seen is auto-registered as a
   `scratch` tape.
3. **Make sure you have enough scratch tapes.** The write job spans onto blank
   tapes automatically, but only if they are in the library and marked `scratch`.
   Format any that still report a non-scratch status via
   **[Library](library.md) → Utilities → Format**.
4. **[Jobs](jobs.md) → + Write job** for the general project archive: source path,
   **standard** mode, backup category `project_archive`. See [Archiving](archiving.md).
5. **[Jobs](jobs.md) → + Write job** for each machine-drive pull: **greedy** mode,
   greedy source = the machine name, backup category `machine_drive_backup`.
6. **Watch progress** on the job page. Per unit the pipeline runs
   scan → allocate tape(s) → copy to LTFS → SHA256 → read back and re-hash →
   write catalog rows. Read-back mismatches show in the job result.
7. **When the run is done:** [Jobs](jobs.md) → **Run catalog backup + CSV export**,
   and confirm the files landed on the network share
   ([Exports & durability](exports-durability.md)).
8. **Spot-check:** [Search](search-catalog.md) for a couple of known shots, open one,
   **Prepare restore** to a scratch path, run it, eyeball the files
   ([Restore](restore.md)).
9. **Each year, also verify tapes you did not write to.** The Dashboard lists
   tapes overdue for a spot re-verification — pull those and queue a verify
   ([Verification](verification.md)).

---

## A note on terminology

| Term | Meaning in Vader |
|---|---|
| **Tape** | One physical LTO cartridge, identified by its barcode. |
| **Span** (`content_tape_span`) | One record tying a piece of content to one tape at one LTFS path. A single shot or file split across tapes has one span per tape. |
| **Sequence container** | The catalog row for one Type-A EXR shot/sequence *folder* (not per frame). |
| **Content item** | The catalog row for one Type-B video chunk, Type-C config file, or Type-D audio/media file. |
| **Unit** / **write unit** | What the intake scanner produces from the source tree before allocation: one per sequence folder, one per video chunk, one per loose file. |
| **Manifest** | A JSON sidecar listing every frame filename + SHA256 for one Type-A folder. Stored under `_manifests/`, never inside the shot folder, never restored into deliverable output. |
| **Job** | A background task (write, verify, restore, backup) run one at a time by the worker. |
