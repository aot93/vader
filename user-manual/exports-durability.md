# Exports & durability

**What this covers:** the catalog CSV exports, the database backup job and
`scripts/backup.sh`, the sidecar files Vader leaves on every tape, and how to
recover the catalog itself if the database is lost.

The tape database is arguably worth more than any single tape — it is the only
thing that makes the archive searchable. It must survive independently of this
application. This page is how.

---

## 1. The three layers of catalog durability

| Layer | What it is | Where it lives | Rebuild value |
|---|---|---|---|
| **Database** | The live catalog (Postgres or SQLite). | The VM. | Everything — the working system. |
| **Full-catalog CSV** | One row per span, every checksum. | `DATA_DIR/exports/catalog-<timestamp>.csv` and copied to `BACKUP_DIR`. | A plain-text, app-free record of what is on which tape. |
| **On-tape sidecars** | Per-tape CSV + Type-A frame manifests, written onto the LTFS volume. | Each tape: `/_catalog/<barcode>.csv`, `/_manifests/...json`. | A tape found with no context still carries its own manifest. |

The design principle: if you lost the VM tomorrow, the CSV on the share plus the
sidecars on the tapes would still tell you — and any future tool — exactly what is
where, with checksums to prove integrity.

---

## 2. CSV exports

### Full catalog

- **Top bar → `catalog.csv`** (`GET /export/catalog.csv`). Every span across every
  tape, ordered by barcode then LTFS path.

### One tape

- **[Tape page](tapes.md) → Export tape CSV** (`GET /export/tape/<barcode>.csv`).
  Every span on that one tape. This is the same content written onto the tape at
  `/_catalog/<barcode>.csv` during the write job.

### Columns (both exports)

`tape_barcode`, `tape_status`, `content_kind`, `content_type`, `project_name`,
`sequence_name`, `shot_name`, `video_name`, `chunk_index`, `total_chunks`,
`backup_category`, `source_machine`, `source_path`, `ltfs_path`, `part_index`,
`part_count`, `byte_range_start`, `byte_range_end`, `size_bytes`, `sha256`,
`manifest_ref`, `written_at`, `verified_at`.

Because every row carries `sha256` (and `manifest_ref` points at the frame-level
manifest for Type A), the CSV **is itself an integrity manifest** — you can verify
a tape against it with nothing but a checksum tool.

---

## 3. The backup job

**[Jobs](jobs.md) → Run catalog backup + CSV export** enqueues a `backup` job that:

1. writes the full-catalog CSV to `DATA_DIR/exports/catalog-<timestamp>.csv`;
2. backs up the database to `BACKUP_DIR`:
   - **SQLite:** a timestamped file copy (`vader-<timestamp>.sqlite3`);
   - **Postgres:** a `pg_dump` to `vader-<timestamp>.sql`.

The job result reports both paths (`catalog_csv`, `db_backup`).

> **`BACKUP_DIR` must point at the network share (or wherever the organisation
> backs up critical files), not local disk on the VM.** A backup on the same disk
> as the database protects against nothing.

Run this job **at the end of every annual run**, and confirm the two files
actually landed on the share.

---

## 4. `scripts/backup.sh` (cron)

The same work as the backup job, runnable from cron so it happens without anyone
opening the UI. It calls `run_db_backup()` and `write_full_catalog_export()` and
prints the two paths.

```cron
# weekly at 03:00 during a run; monthly otherwise is fine when idle
0 3 * * 0  cd /opt/vader && ./scripts/backup.sh >> /var/log/vader-backup.log 2>&1
```

It reads the same `.env` as the app, so `DATABASE_URL` and `BACKUP_DIR` are
whatever the app uses.

---

## 5. On-tape sidecars

Written automatically by every write job, on the LTFS volume, in **parallel**
paths that are never inside a real content folder:

| Path on tape | Content |
|---|---|
| `/_catalog/<barcode>.csv` | This tape's full span listing with checksums — the per-tape CSV. |
| `/_manifests/<project>/[<sequence>/]<shot>.json` | Per-frame filename + frame number + size + SHA256 for each Type-A folder on the tape. A spanned shot has one `.partN.json` per tape. |

Both directory names are:

- **skipped by the intake scanner** — re-archiving a restored tree will not pull
  them back in;
- **excluded unconditionally from restore output** — see [Restore](restore.md) §7.

The app also keeps every frame manifest under `DATA_DIR/manifests/`, and for a
spanned shot merges the parts into one whole-folder manifest there;
`sequence_containers.manifest_ref` points at it, and the CSV exports carry that
reference.

---

## 6. Recovering the catalog itself

### Restore the database from a backup

- **Postgres:**
  ```bash
  createdb vader_restore_test
  psql vader_restore_test < /share/vader/backups/vader-YYYYMMDDTHHMMSSZ.sql
  ```
  Do this once as a drill so you know the dump is good, then point
  `DATABASE_URL` at the restored database.
- **SQLite:** copy the `vader-<timestamp>.sqlite3` file back to the
  `DATABASE_URL` path.

### If there is no usable database backup

The **full-catalog CSV** on the share is the fallback. It is not an automatic
re-import, but it is a complete, human- and spreadsheet-readable record of every
tape, every path, every checksum — enough to locate and validate any piece of
content by hand, and enough for a developer to bulk-load into a fresh database.

### If a tape turns up with no records at all

Mount it and read `/_catalog/<barcode>.csv` from the tape itself, and the
`/_manifests/` JSON files for frame-level checksums. That is exactly why they are
written there.
