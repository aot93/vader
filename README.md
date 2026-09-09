# Vader — Tape Archive Control Application

A single web application that replaces manual `mtx`/`ltfs` command-line operation
and the ML3 web interface for a once-a-year LTO archive run, and — more
importantly — maintains a **permanent, queryable catalog** of what is on which
tape, with verification and guided restore.

Built to the spec in [`docs/Tape-Archive-App-Framework.md`](docs/Tape-Archive-App-Framework.md),
which runs on the Linux VM described in [`docs/LTO-Archive-VM-Setup.md`](docs/LTO-Archive-VM-Setup.md).

> The catalog is the point. It must stay trustworthy and readable for years,
> independent of whether this app is still running — see **Durability** below and
> framework doc §8.

---

## What it does

| Area | Framework doc | Status |
|---|---|---|
| Live library & drive dashboard, load / unload / inventory, all logged | §3.1, §3.7 | ✅ |
| Utility actions kept separate: **Format** (mkltfs, scratch-guarded), **Clean**, **Retire**, **Eject** | §3.1 | ✅ |
| Tape database (barcode, gen, capacity, status, write passes, location, offsite pair, notes) | §3.2 | ✅ |
| Content catalog — Type A EXR sequences (two-tier: container row + sidecar frame manifest), Type B video chunks, Type C config/logs, Type D audio/media | §3.3 | ✅ |
| Write-time SHA256 + read-back verification | §3.4, §5.1 | ✅ |
| Scheduled re-verification reminders (per-tape "last verified") | §3.4, §5.5 | ✅ |
| Read-error history per tape | §3.4, §5.7 | ✅ |
| Search (project / shot / video / source machine / date / backup category / barcode) → **guided restore** with manifest exclusion enforced | §3.5, §5.2 | ✅ |
| Automatic cross-tape spanning (whole-frame / whole-chunk / byte boundary) | §3.5a, §5.3 | ✅ |
| Greedy write mode — isolate one source to its own tapes | §3.5a, §5.6 | ✅ |
| CSV export — per-tape (also written onto the tape) and full-catalog, with checksums | §3.6, §8 | ✅ |
| Audit log of every load/unload/write/verify/restore | §3.7 | ✅ |
| Background jobs with persisted progress; interrupted jobs detected on restart; idempotent re-run | §2, §6 | ✅ |
| Database backup + full-catalog CSV export for catalog durability | §8 | ✅ |

## Stack

- **Backend:** Python + FastAPI, synchronous SQLAlchemy 2.0 (tape ops are serial).
- **Database:** Postgres in production; SQLite for evaluation / dev. Alembic migrations.
- **Frontend:** server-rendered Jinja2 + a little HTMX for live job progress. No build step.
- **Jobs:** one background worker thread, one job at a time, state in the `jobs` table.
- **Hardware:** an abstraction layer with two backends —
  - `real` — subprocess wrappers around `mtx` / `mt` / `mkltfs` / `ltfs`, run on the archive VM.
  - `simulator` — in-memory library + directory-backed fake LTFS, so the whole app runs, demos and tests with **no tape hardware**.

## Quick start (simulator, SQLite)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt

# optional: populate a believable demo archive
HARDWARE_BACKEND=simulator .venv/bin/python scripts/dev_seed.py

.venv/bin/uvicorn app.main:app --reload --port 8000
# open http://localhost:8000/
```

With no `.env`, defaults are: SQLite at `./data/vader.sqlite3`, `HARDWARE_BACKEND=simulator`,
no auth. Copy `.env.example` to `.env` to change anything.

## Production (Postgres + real hardware)

See [`RUNBOOK.md`](RUNBOOK.md). In short:

```bash
cp .env.example .env      # set DATABASE_URL=postgresql+psycopg://…, HARDWARE_BACKEND=real,
                          # CHANGER_DEVICE, DRIVE_DEVICES, LTFS_MOUNT_BASE, SESSION_SECRET
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`docker compose up` brings up Postgres + the app with the simulator backend; the
`Dockerfile` header explains what to add for real device passthrough.

## Tests

```bash
.venv/bin/pytest
```

Covers intake classification, the spanning/greedy allocator, and a full
write → catalog → search → verify → restore round-trip on the simulator
(including idempotent re-run, tape spanning, greedy isolation, and corruption
detection).

## Durability of the catalog (§8)

- `scripts/backup.sh` (run from cron) writes a database dump **and** the
  full-catalog CSV to `BACKUP_DIR` — point that at the network share, not the VM.
- The full-catalog CSV includes every checksum, so it is a standalone integrity
  manifest readable in any spreadsheet with no app.
- Every write job also drops a per-tape CSV catalog onto the tape itself at
  `/_catalog/<barcode>.csv`, so a tape found without context still carries its
  own manifest.
- Frame-level manifests live in a parallel `_manifests/` path on the LTFS volume
  and in `DATA_DIR/manifests/` — **never inside a shot folder**, and always
  excluded from restore output.

## Repository layout

```
app/
  config.py            settings from env / .env
  db.py  models.py      SQLAlchemy engine + the permanent-record schema
  hardware/             base + real (mtx/ltfs) + simulator backends
  services/
    intake.py           classify a source tree into A/B/C/D write units
    spanning.py         tape allocation, cross-tape spanning, greedy mode
    writer.py           scan → allocate → copy → checksum → read-back → catalog
    verification.py     re-read + re-hash a tape, log read errors
    restore.py          prepare a guided restore plan, then execute it
    export_csv.py        per-tape and full-catalog CSV
    library.py           mtx/ltfs actions, each logged as a tape event
  jobs/                 persisted background worker
  routers/  templates/  static/    the web UI
migrations/             Alembic
scripts/               backup.sh, dev_seed.py
tests/
```
