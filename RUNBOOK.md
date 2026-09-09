# Vader — Operations Runbook

This tool sits untouched for ~11 months at a time. This file is the thing to
read when the annual run comes around again. Keep it next to the code.

---

## 0. Before the run — dry run on the VM

Follow `docs/LTO-Archive-VM-Setup.md` §6 first (scratch tape, both drives, changer
addressing). Only once `mtx status`, `mkltfs`, `ltfs` and `mt` all work by hand
should you point Vader at the real hardware.

## 1. Versions / dependencies

- Python 3.11+ (3.12 recommended; tested on 3.12–3.14).
- Install **`requirements.lock.txt`** (fully pinned), not `requirements.txt`:
  ```bash
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.lock.txt
  ```
- The tape toolchain on the VM: `sudo apt install -y mt-st mtx sg3-utils ltfs lsscsi`.
- Do **not** bump dependencies right before a run. If you must, re-run `pytest`
  and re-do the §0 dry run, then re-freeze: `.venv/bin/pip freeze > requirements.lock.txt`.

## 2. Configuration (`.env`)

Copy `.env.example` → `.env`. Key values for a real run:

| Variable | Value |
|---|---|
| `DATABASE_URL` | `postgresql+psycopg://vader:PASS@localhost:5432/vader` |
| `HARDWARE_BACKEND` | `real` |
| `CHANGER_DEVICE` | the changer `/dev/sg*` from `lsscsi -g` (type `mediumx`) |
| `DRIVE_DEVICES` | comma-separated `/dev/nst*` for the two drives, in bay order |
| `LTFS_MOUNT_BASE` | e.g. `/mnt/ltfs` (must be writable; the app makes `driveN` subdirs) |
| `DATA_DIR` | app working dir — manifests, exports, restores land here |
| `BACKUP_DIR` | **a path on the network share**, not local disk (see §7) |
| `SESSION_SECRET` | any long random string |
| `AUTH_TOKEN` | leave blank if the VM is network-isolated; else a shared token |
| `REVERIFY_MONTHS` | default 12 — how stale "last verified" may get before a reminder |

Device nodes can move across reboots — prefer `/dev/tape/by-id/...` where
populated, or add udev rules (VM-setup doc §6.1).

## 3. Start / stop

```bash
.venv/bin/alembic upgrade head                       # apply any schema changes
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Run it under `systemd` or `tmux` for the duration of the run. There is one
background worker thread inside the process; stopping uvicorn stops it cleanly.
Any job left `running` when the process dies is marked **interrupted** on the
next start — re-run it from the job page (write and verify are idempotent).

A `systemd` unit is fine and simple:

```ini
[Unit]
Description=Vader tape archive control
After=network-online.target

[Service]
WorkingDirectory=/opt/vader
EnvironmentFile=/opt/vader/.env
ExecStart=/opt/vader/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## 4. Typical annual run

1. **Library** page → **Refresh inventory**. Confirm the slot map matches reality.
   New barcodes are auto-registered as `scratch`.
2. Register/replenish **scratch** tapes if short — the write job needs enough
   blank tapes in the library to span onto. Format any that report a non-scratch
   status via **Library → Utilities → Format** (scratch-guarded; `force` to repurpose).
3. **Jobs → + Write job** for the general project archive:
   - Source path = the mounted share / directory to archive.
   - Mode **standard** (pack tapes fully, automatic spanning).
   - Set *backup category* = `project_archive`.
4. **Jobs → + Write job** per machine-drive pull:
   - Mode **greedy**, *greedy source* = the machine name, category = `machine_drive_backup`.
   - Greedy keeps that machine's data on its own tapes — a clean single-machine
     recovery unit — at the cost of some tape space.
5. Watch progress on the job page. The write pipeline does, per unit:
   scan → allocate tape(s) → copy to LTFS → SHA256 → **read back and re-hash** →
   write catalog rows. A read-back mismatch is flagged in the job result and the
   tape's notes immediately.
6. When done, run **Jobs → Run catalog backup + CSV export** (see §7).
7. Spot-check: **Search** for a couple of known shots, open one, **Prepare
   restore** to a scratch path, run it, eyeball the files.

## 5. Verifying tapes (recovery insurance)

- The **dashboard** surfaces tapes whose "last verified" is older than
  `REVERIFY_MONTHS` (or never). Pull those and queue a verify even if you are not
  writing to them this year.
- **Tape page → Queue verify**, or **Jobs → new verify**. `full=true` re-reads
  everything; otherwise it samples `DEFAULT_VERIFY_SAMPLE_FRACTION` (10%).
- Verify re-reads from LTFS and compares to the stored SHA256 — it never needs
  the original source. Checksum mismatches show in the job result; genuine
  SCSI/LTFS read errors are logged to the tape's **read-error history** so a
  degrading tape becomes a visible trend.
- A tape with mismatches is **not** auto-marked damaged — review it, then set the
  status manually on the tape page and, if you still have a live source,
  re-archive.

## 6. Restores

1. **Search** with any combination of project / shot / video / source machine /
   date / backup category / tape barcode.
2. Tick the rows, set a **destination path**, **Prepare restore**.
3. The restore page shows the tapes in load order, a per-file plan, and a
   command preview. If a required tape is **offsite / missing / damaged** you get
   a blocking warning — fetch it first.
4. **Run restore**. It loads each tape in turn, copies only real content back
   into the original directory structure, reassembles any byte-split files, and
   checksums the result. Sidecar manifests (`_manifests/`, `_catalog/`) are
   **never** written into restore output.

## 7. Catalog durability — do not skip

The catalog outlives this app. Protect it:

- `scripts/backup.sh` writes a DB dump **and** the full-catalog CSV to
  `BACKUP_DIR`. Put it on cron on the VM:
  ```
  0 3 * * 0  cd /opt/vader && ./scripts/backup.sh >> /var/log/vader-backup.log 2>&1
  ```
- Run it (or the **backup job** in the UI) at the **end of every annual run** and
  confirm the files landed on the share.
- The full-catalog CSV has every checksum — it is a usable integrity manifest on
  its own, no database or app required.
- Each written tape also carries `/_catalog/<barcode>.csv` on the tape itself.

Postgres restore test (do this once so you know it works):
```bash
pg_dump ... > /share/vader/backups/vader-YYYY....sql        # produced by backup.sh
createdb vader_restore_test && psql vader_restore_test < that_file
```

## 8. Troubleshooting

| Symptom | Check |
|---|---|
| Dashboard: "Library unreachable" | `HARDWARE_BACKEND`, `CHANGER_DEVICE`; run `sudo mtx -f $CHANGER_DEVICE status` by hand; device node moved? |
| `mkltfs` / `ltfs` "not found" | tape toolchain not installed in the app's `PATH` |
| Write job fails "out of scratch tapes" | load more blank tapes, Refresh inventory, re-run the job (idempotent) |
| Job stuck `running` after a crash | restart the app; it becomes `interrupted`; re-run it |
| "database is locked" (SQLite) | expected only under heavy concurrent use — move to Postgres for the real run |
| Restore blocked | a required tape is not in the library — the plan lists which barcode and its last known location |
| Verify shows read errors | tape page → read-error history; if recurring, retire the tape and re-archive from source while you still can |

## 9. Returning the HBA to the host

When the run is over and you want the HBA back on the Windows host, follow
`docs/LTO-Archive-VM-Setup.md` §4.6. Stop Vader and shut the VM down first.
