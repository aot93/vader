# Session notes — connection/tape debugging (vm-wing)

Branch: `format-job-and-tape-targeting`
Last commit at time of writing: `d043c94`

This file is a handoff/resume point in case the session drops. Delete it once
the work is confirmed done and merged.

## Original problems reported

1. Connections are always valid despite the .env set to real
2. Valid connections do not actually resolve
3. Tape read operations do not work
4. Tape write operations do not work
5. (blank — user confirmed this was a typo, no content)

## Fixed and committed (pushed pending — see "Blocked" below)

### Commit `7cd53f4` — connection health check (issues 1 & 2)

`SystemdMountBackend.check_health()` in `app/mounts/systemd_backend.py` only
checked `os.listdir(mount_path)`. That directory is always created locally by
`provision()` regardless of whether the real CIFS mount ever connects, and
enabling the systemd `.automount` unit creates an autofs placeholder the
instant it's enabled, independent of the network connection. So health
checks always reported healthy. Fixed by cross-checking `/proc/mounts` for an
actual `cifs` filesystem at that exact path.

### Commit `d043c94` — real-hardware tape writes (issue 4)

Two independent bugs, both confirmed against this VM's actual job history and
DB state before fixing:

- **mkltfs never forced.** Real `mkltfs` refuses outright on a medium that
  already carries an LTFS filesystem — the normal state of any real "scratch"
  tape being reused — unless given `-f`/`--force`. Nothing in the app ever
  passed that through (`writer.py`, `batch_format.py`, `library.py` all called
  `hw.mkltfs()` bare). This reproduced exactly the "Medium is already
  formatted" failure already on record in this VM's job history (job #9,
  tape `AB26009L`). Force is now threaded through `mkltfs()` at the hardware
  layer in all three call sites. The simulator now mirrors the same refusal
  so this class of bug is covered by tests going forward
  (`tests/test_pipeline.py::test_write_reuses_a_scratch_tape_that_already_carries_an_ltfs_filesystem`).

- **Capacity fallback borrowed from the simulator.** Every real tape's
  `capacity_native_bytes` was `None` (confirmed on all 19 tapes in the DB),
  and the allocator fell back to `SIM_TAPE_CAPACITY_BYTES` (12 GB,
  deliberately scaled down 1000x for fast test spanning) as if it were the
  real capacity — so every real cartridge looked "full" almost immediately.
  Added `DEFAULT_TAPE_CAPACITY_BYTES` (`app/config.py`), used only when
  `HARDWARE_BACKEND=real`. Covered by `tests/test_tape_capacity.py`.

Both changes are fully covered by the test suite (73 tests passing at last
check: `.venv/bin/python -m pytest -q`).

## Fixed locally, NOT committed (`.env` is gitignored)

### Device path bug (issue 3 — tape reads)

`DRIVE_DEVICES` in `.env` was `/dev/nst0,/dev/nst1` (the Linux `st`-driver
tape nodes). The `ltfs`/`mkltfs` binaries on this box (a local build of the
open-source LinearTapeFileSystem project, at `/usr/local/bin/ltfs` /
`/usr/local/bin/mkltfs`, source checked out at `/home/pml/ltfs`) issue raw
`SG_IO` ioctls for actual data reads/writes. Linux's `st` driver does **not**
support `SG_IO` — only the `sg` driver (`/dev/sg*`) does. This is exactly the
`Failed to execute SG_IO ioctl, opcode = 08 (22)` error recorded in job
history (jobs #44, #11) — the kernel rejecting the ioctl because of the wrong
device class, not a media or LTO-9-command-set problem. Confirmed the correct
mapping with `lsscsi -g`:

```
[1:0:0:0]    tape    IBM      ULT3580-HH9      R3G9  /dev/st0   /dev/sg1   <- drive 0
[1:0:0:1]    mediumx IBM      3573-TL          1621  /dev/sch0  /dev/sg2   <- changer
[1:0:1:0]    tape    IBM      ULT3580-HH9      R3G9  /dev/st1   /dev/sg3   <- drive 1
```

`.env` now has:
```
DRIVE_DEVICES=/dev/sg1,/dev/sg3
```

Also updated (and this part IS committed, in `d043c94`) `app/config.py` and
`.env.example` to document that `CHANGER_DEVICE`/`DRIVE_DEVICES` must be
`/dev/sg*` nodes, not `/dev/nst*`/`/dev/st*`.

### LTO-9 capacity (per user correction)

This library is entirely LTO-9 media (18 TB native), not LTO-8 (12 TB). Set
in `.env`:
```
DEFAULT_TAPE_CAPACITY_BYTES=18000000000000
```
The tracked default in `app/config.py`/`.env.example` is left at the LTO-8
figure (12 TB) as a generic example — only this VM's `.env` needed the
LTO-9-specific override.

## Verification in progress (real hardware, live service)

User ran `sudo systemctl restart vader.service` (00:25:55 UTC) to pick up the
fixed code + `.env`. Service runs as **root** (confirmed via `ps aux`) — note
the unit file `/etc/systemd/system/vader.service` has a commented-out
`#User=pml`/`#Group=pml` with a stale comment ("Run as your normal user, not
root") that no longer matches reality. Not changed — just flagged in case it
matters later (if someone ever uncomments it, real LTFS mounts would fail
with a FUSE `fusermount: user has no write access to mountpoint` error unless
`/mnt/ltfs/drive0` and `/mnt/ltfs/drive1` ownership is also changed, since
those dirs are currently `root:root`).

**Job #45 — `tape_import`, barcode `AB26001L` (slot 1), drive 1.** Submitted
via the live app's own HTTP API (`POST /jobs/new/tape_import`) so it runs
under the restarted root process, not a standalone script (a standalone
script run as `pml` hit the FUSE ownership issue above — not a real bug, just
an artifact of testing outside the app).

Result so far: **read is conclusively confirmed working.** The LTFS mount
succeeded cleanly (`LTFS11031I Volume mounted successfully`, no SG_IO error),
and the real archived content is directly browsable at `/mnt/ltfs/drive1` —
confirmed 375,423 real files (~17 TB, per user) under a legacy pre-Vader
backup structure (`PML1 Content Backup`, `PML2 Originals`, client-code
subfolders). The `tape_import` job's `scan_source()` walk (stat + regex match
per file, no incremental progress reporting) is still grinding through all
375K files as of session pause — this is expected to take a long time given
the scale, not a hang (confirmed both the `ltfs` FUSE process and the
`uvicorn` app process were actively burning CPU, not blocked).

**Job #46 — `write`, target tape `AB26008L`, drive 0.** Submitted via
`POST /jobs/new/write` with a tiny synthetic test sequence at
`/tmp/vader-write-test/seq010/shot0100/shot0100.{1001,1002,1003}.exr` (this
is under `/tmp`, will not survive a reboot — recreate if needed, see below).
Currently **queued behind #45** — `app/jobs/worker.py`'s `JobWorker` is a
single background thread, jobs run strictly serially regardless of which
drive they need, so #46 will not start until #45 finishes (success, failure,
or cancellation).

## To resume

1. Check job status:
   ```
   cd /home/pml/projects/vader
   .venv/bin/python3 -c "
   from app.db import SessionLocal
   from app.models import Job
   db = SessionLocal()
   for jid in (45, 46):
       j = db.get(Job, jid)
       print(jid, j.status.value, j.progress_message, j.error, j.result)
   "
   ```
   Or just check `http://<vm>:8000/jobs/45` and `/jobs/46` in a browser.

2. If #45 is still running and you don't want to wait longer, it can be
   cancelled via `POST /jobs/45/cancel` (note: cancellation is checked at
   loop boundaries, not inside the current blocking `scan_source()` call, so
   it may not take effect until that finishes on its own regardless).

3. Once #46 (write) completes, confirm:
   - `job.status == "completed"`
   - Tape `AB26008L`'s `used_bytes`/`status`/`capacity_native_bytes` in the
     Tapes page reflect the write and the corrected 18 TB capacity.
   - The written content is catalogued (Search page) and matches what's in
     `/tmp/vader-write-test`.

4. If the test source dir is gone (e.g. reboot), recreate it:
   ```
   mkdir -p /tmp/vader-write-test/seq010/shot0100
   for i in 1001 1002 1003; do
     dd if=/dev/urandom of=/tmp/vader-write-test/seq010/shot0100/shot0100.$i.exr bs=1024 count=50
   done
   ```

5. Once both jobs are confirmed good, push the two local commits — **this VM
   has no git credentials configured for `github.com/aot93/vader`** (no SSH
   key, no `gh` CLI, no credential helper). Either set one up here, or push
   from a machine that already has access:
   ```
   git push origin format-job-and-tape-targeting
   ```

## Loose ends / not investigated further

- The `vader.service` `User=`/`Group=` inconsistency noted above (currently
  running as root; unit file suggests it was meant to run as `pml`). Not a
  bug per se today, but if that's ever "fixed" to actually run as `pml`,
  `/mnt/ltfs/drive0` and `/mnt/ltfs/drive1` ownership will need to change too
  or every real LTFS mount will fail on the FUSE ownership check.
- Haven't double-checked whether `tape_import`'s full catalog of 375K files
  produces sane `ContentItem`/`SequenceContainer` rows at this scale
  (performance/memory of one very large `scan_source()` call over FUSE was
  not something this session profiled — if #45 fails or looks wrong when you
  resume, that's the first place to look).
