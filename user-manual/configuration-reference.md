# Configuration reference

**What this covers:** every setting Vader reads, its default, and what it does.
All configuration comes from environment variables, optionally loaded from a
`.env` file. There is no settings screen — configuration is fixed at startup, so
changing any value means restarting the application.

---

## How settings are loaded

1. On startup Vader reads a `.env` file from the working directory (or the path in
   `VADER_ENV_FILE`). Values already set in the real environment win over `.env`.
2. Settings are then frozen for the life of the process.
3. On startup Vader also creates, if missing: `DATA_DIR`, `BACKUP_DIR`,
   `DATA_DIR/manifests`, `DATA_DIR/exports`, `DATA_DIR/sim`,
   `DATA_DIR/smb_credentials` (mode 700).

Boolean values accept `1`, `true`, `yes`, `on` (case-insensitive) for true;
anything else is false.

---

## Database

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/vader.sqlite3` | SQLAlchemy URL. Production: `postgresql+psycopg://user:pass@host:5432/vader`. A `sqlite:` URL enables WAL mode, a 15 s busy timeout and `foreign_keys=ON` automatically. |

---

## Hardware backend

| Variable | Default | Meaning |
|---|---|---|
| `HARDWARE_BACKEND` | `simulator` | `simulator` = in-memory library + directory-backed fake LTFS, no hardware needed. `real` = drive `mtx` / `mt` / `mkltfs` / `ltfs` via subprocess on this host. |
| `CHANGER_DEVICE` | `/dev/sg3` | The changer's `/dev/sg*` node (type `mediumx` in `lsscsi -g`). **`real` only.** |
| `DRIVE_DEVICES` | `/dev/nst0,/dev/nst1` | Comma-separated tape-drive nodes, **in physical bay order**. **`real` only.** |
| `LTFS_MOUNT_BASE` | `/mnt/ltfs` | Base mount directory; Vader mounts each drive at `<base>/driveN`. Must be writable. **`real` only.** |

> Device nodes can move across reboots — prefer stable `/dev/tape/by-id/...`
> paths, or add udev rules (see `docs/LTO-Archive-VM-Setup.md` §6).

---

## Simulator tuning (only used when `HARDWARE_BACKEND=simulator`)

| Variable | Default | Meaning |
|---|---|---|
| `SIM_STORAGE_SLOTS` | `24` | Number of storage slots in the fake library. |
| `SIM_DRIVES` | `2` | Number of fake drives. |
| `SIM_TAPE_CAPACITY_BYTES` | `12000000000` (12 GB) | Capacity of each simulated tape. Deliberately small so spanning is easy to exercise. Also used as the **fallback capacity** for any real tape whose `capacity_native_bytes` is unset when the write allocator runs. |
| `SIM_SEED_TAPES` | `true` | Seed the fake library with barcoded cartridges (`TEST001L8`…) plus one cleaning cartridge (`CLN001L1`). |

---

## SMB Connection Manager (only used when `SMB_BACKEND=real`)

| Variable | Default | Meaning |
|---|---|---|
| `SMB_BACKEND` | `simulator` | `simulator` = same credentials file / mount dir / unit layout under `DATA_DIR/sim/`, no `systemctl` calls, no root needed. `real` = write systemd units under `SMB_SYSTEMD_DIR` and drive them with `systemctl` on this host. |
| `SMB_MOUNT_BASE` | `/mnt/vader` | Ingest connections are mounted at `<base>/<hostname>`; restore destinations at `<base>/<hostname>-restore` (so the two never collide for the same machine). **`real` only** — the simulator mounts under `DATA_DIR/sim/mounts/` instead. |
| `SMB_SYSTEMD_DIR` | `/etc/systemd/system` | Where the generated `.mount` / `.automount` unit pair is written. Requires the process to be able to write here and run `systemctl` — root, or a sudoers rule scoped to this app. **`real` only.** |
| `SMB_HEALTH_INTERVAL_SECONDS` | `300` | How often the background health-check sweep re-checks every configured connection (floor of 30s). See [Connections](connections.md). |

---

## Paths

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `./data` (resolved to absolute) | Working directory. Holds `manifests/`, `exports/`, `sim/`, `smb_credentials/`, and default `restores/` output. |
| `BACKUP_DIR` | `./data/backups` (resolved) | Where the backup job and `scripts/backup.sh` write the DB dump and full-catalog CSV. **Point at the network share for a real run.** |
| `MANUAL_DIR` | `<repo>/user-manual` (resolved) | Directory of Markdown files rendered live at **Help** (`/help`). Change only if you deploy the manual separately from the app. |

---

## Behaviour

| Variable | Default | Meaning |
|---|---|---|
| `REVERIFY_MONTHS` | `12` | A tape is flagged on the Dashboard when `last verified` is empty or older than this many months (calculated as `months × 30` days). See [Verification](verification.md). |
| `DEFAULT_VERIFY_SAMPLE_FRACTION` | `0.1` | Fraction of a tape's checks a **sample** verify runs (0.1 = 10%, at least 1). A full verify ignores this. |
| `WRITE_READBACK_VERIFY` | `true` | After copying each file to LTFS, re-hash the source and compare to the written copy; record any mismatch in the write job result. Strongly recommended on for the real archive. See [Archiving](archiving.md) §6. |

---

## Auth

| Variable | Default | Meaning |
|---|---|---|
| `AUTH_TOKEN` | *(empty)* | Empty = **auth disabled**, every page open (intended for a network-isolated VM). Non-empty = every page requires a session established at `/login` with this shared token. One shared token, no user accounts. |
| `SESSION_SECRET` | `change-me-please` | Secret that signs the session cookie. Set to a long random string in production even if auth is disabled. |

---

## Other

| Variable | Default | Meaning |
|---|---|---|
| `VADER_ENV_FILE` | `.env` | Path to the env file to load at startup. |

---

## Not configurable via environment

These are fixed in code; listed here so you know the numbers:

| Behaviour | Value | Where |
|---|---|---|
| Worker poll interval | ~1.5 s | job worker |
| Job page auto-refresh | 2 s | job detail template |
| "Tape is full" threshold | < 2% of capacity free after a write | write finalisation |
| "Avoid fragmentation" rule | a unit that fits whole on another writable tape is placed there rather than split | spanning allocator |
| Search result cap | 200 rows | catalog search |
| List page size | 50 rows (tapes, jobs, restores); 100 rows (audit actions, tape events) | paginated list views, `?page=` / `?epage=` |
| Recent events on Dashboard | latest 12 | dashboard |
| Copy / hash block size | 4 MiB | writer / checksums |
