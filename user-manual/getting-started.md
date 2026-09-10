# Getting started

**What this covers:** the minimum needed to get Vader running and to log in. This
is deliberately short. The full archive-VM build (Hyper-V DDA passthrough, the
tape toolchain, networking, the pre-run dry run) lives in the project
`RUNBOOK.md` and `docs/LTO-Archive-VM-Setup.md` — this page does not repeat it.

---

## 1. What Vader needs to run

| Requirement | Notes |
|---|---|
| Python 3.11+ | 3.12 recommended; tested on 3.12–3.14. |
| The pinned dependencies | Install `requirements.lock.txt` (fully pinned), not `requirements.txt`. |
| A database | SQLite works out of the box for evaluation. Use PostgreSQL for a real run. |
| Tape toolchain (real hardware only) | `mt-st`, `mtx`, `sg3-utils`, `ltfs`, `lsscsi` on the VM `PATH`. |

The application itself is one process: a FastAPI web server with a single
background worker thread inside it. There is no separate queue service.

---

## 2. First run (simulator, SQLite) — for evaluation and training

The **simulator** backend is a complete in-memory library with a directory-backed
fake LTFS volume. The whole application runs, and every screen works, with no tape
hardware attached. Use it to learn the tool.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt

# optional: populate a believable demo archive (two projects, EXR + video + config + audio)
HARDWARE_BACKEND=simulator .venv/bin/python scripts/dev_seed.py

.venv/bin/uvicorn app.main:app --port 8000
# open http://localhost:8000/
```

With no `.env` file the defaults are: SQLite at `./data/vader.sqlite3`,
`HARDWARE_BACKEND=simulator`, and **no login required**. On first start with the
simulator, Vader registers the simulated cartridges as `scratch` tapes so a write
job has somewhere to go.

---

## 3. Configuration

All settings come from the environment, optionally via a `.env` file in the
working directory. Copy the template and edit:

```bash
cp .env.example .env
```

The settings you are most likely to change:

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/vader.sqlite3` | Use `postgresql+psycopg://user:pass@host:5432/vader` for production. |
| `HARDWARE_BACKEND` | `simulator` | `simulator` = fake library (no hardware). `real` = drive `mtx`/`mt`/`mkltfs`/`ltfs` on this host. |
| `CHANGER_DEVICE` | `/dev/sg3` | The changer `/dev/sg*` node (from `lsscsi -g`), used only when `HARDWARE_BACKEND=real`. |
| `DRIVE_DEVICES` | `/dev/nst0,/dev/nst1` | Comma-separated drive nodes, in physical bay order. |
| `LTFS_MOUNT_BASE` | `/mnt/ltfs` | Vader mounts each drive under `driveN` here; the directory must be writable. |
| `DATA_DIR` | `./data` | Working directory — manifests, CSV exports and restore output land here. |
| `BACKUP_DIR` | `./data/backups` | Where the backup job writes. **Point this at the network share**, not local disk. |
| `AUTH_TOKEN` | *(empty)* | Empty = auth disabled (fine on an isolated VM). Set it to require a shared token at `/login`. |
| `SESSION_SECRET` | `change-me-please` | Any long random string; signs the login session cookie. |
| `REVERIFY_MONTHS` | `12` | How stale a tape's "last verified" date may get before the Dashboard flags it. |

The full list, including simulator tuning and read-back behaviour, is in the
[Configuration reference](configuration-reference.md).

> **Note — device nodes move.** `/dev/sg*` and `/dev/nst*` numbers can change
> across a reboot. Prefer stable `/dev/tape/by-id/...` paths where the VM
> populates them, or add udev rules (VM-setup doc §6).

---

## 4. Production start

On the archive VM, after `.env` is set for Postgres and `HARDWARE_BACKEND=real`:

```bash
.venv/bin/alembic upgrade head                        # apply schema
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Run it under `systemd` or `tmux` for the duration of the run — a sample
`systemd` unit is in `RUNBOOK.md` §3. Stopping uvicorn stops the worker cleanly.
Any job still `running` when the process dies is marked **interrupted** on the
next start and can be safely re-run ([Jobs](jobs.md)).

`docker compose up` brings up PostgreSQL plus the app on the simulator backend,
for a production-shaped local test.

---

## 5. Logging in

- **If `AUTH_TOKEN` is empty:** there is no login. Every page is open. This is the
  intended setup when the VM is already network-isolated to trusted machines.
- **If `AUTH_TOKEN` is set:** every page redirects to `/login` until you enter the
  shared token. There is one shared token, no user accounts. A **log out** link
  appears in the top bar once you are in. The session is a signed cookie; it lasts
  until the browser clears it or you log out.

There is no "forgot token" flow — the token is whatever `AUTH_TOKEN` is set to in
the environment. To change it, change the variable and restart.

---

## 6. First things to check after starting

1. **Dashboard** loads and shows `Hardware backend: real` (for a production run).
2. No red *Library unreachable* banner. If there is one, the changer device is
   wrong or the toolchain is not on `PATH` — see [Troubleshooting](troubleshooting.md).
3. **Library → Refresh inventory** returns a slot map that matches the physical
   library.

Once those three are true, you are ready to archive — continue to
[Archiving](archiving.md).
