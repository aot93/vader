# Connections

**What this covers:** the `/connections` screen — adding a Windows SMB share
as an auto-mounted **ingest source** (read-only, feeds the write job) or
**restore destination** (read-write, a restore's target), reading health
status, picking one on the write-job or restore-prep form, and removing it.
This replaces manually running `mount.cifs` (or editing `/etc/fstab`) on the
archive VM for every machine you read from or restore back onto.

> No screenshot yet for this page — it shipped (as "Sources") after the rest
> of this manual's screenshots were captured, then gained restore
> destinations later. The layout is a plain list + detail pair, the same
> shape as [Tapes](tapes.md), now split into two sections by purpose.

---

## 1. What a connection is

A **connection** is one Windows machine's SMB share, registered once through
**Connections → + Add ingest source** or **+ Add restore destination**. Vader
then:

1. writes a **credentials file** (username / password / domain) to
   `DATA_DIR/smb_credentials/`, mode `600` — never in the database, and not
   viewable again through the UI after you submit the form;
2. creates the **mount point** under `SMB_MOUNT_BASE` (default `/mnt/vader/`);
3. generates a **systemd `.mount` + `.automount` unit pair** for it and starts
   the automount, so the share mounts on first access and **reconnects on its
   own** if the Windows machine reboots, and **survives a VM reboot** without
   you doing anything;
4. records a `connection.add` [audit log](audit-log.md) entry.

**Purpose is fixed at creation and cannot be edited afterward** — it decides
how the mount is provisioned:

| Purpose | Mount mode | Offered on |
|---|---|---|
| **Ingest source** | read-only (`ro`) | [Archiving](archiving.md)'s write-job form, as a source path |
| **Restore destination** | read-write (`rw`) | restore prep (from [Search](search-catalog.md)), as a destination |

You can register **both an ingest source and a restore destination for the
same hostname** — they are provisioned as two entirely separate mounts (own
directory, own systemd unit, own credentials file), so adding a restore
destination for a machine never touches that machine's existing read-only
ingest mount. One connection per `(hostname, purpose)` pair — a second share
on the same machine with the same purpose needs a second hostname/IP entry.

---

## 2. Adding a connection

**Connections → + Add ingest source** or **+ Add restore destination**, then:

| Field | Notes |
|---|---|
| Hostname or IP * | Sanitised strictly (letters, digits, dots, hyphens only) — this value is written into a systemd unit file, so anything else is rejected rather than risking it. |
| Share name * | The SMB share name on that machine. |
| Username * | The Windows account to authenticate as. |
| Password * | Written once into the mode-`600` credentials file, then discarded — Vader keeps no other copy. |
| Domain | Optional. |
| SMB version | `3.0` by default; drop to `2.1` or `1.0` only for an old NAS/share that needs it. |

**Add …** provisions immediately (credentials file → mount dir → systemd
units → `systemctl daemon-reload` && `enable --now`) and takes you to the new
connection's detail page. A duplicate `(hostname, purpose)`, or a value that
fails sanitisation, is rejected with the reason shown on the form — nothing is
created.

---

## 3. Health

Each connection shows a health pill:

| Status | Meaning |
|---|---|
| `unknown` | Not checked yet (just added). |
| `healthy` | The mount directory is listable right now. |
| `unhealthy` | Listing the mount directory failed — the OS error is shown under the pill / on the detail page as **Last error**. |

A background sweep re-checks every connection every `SMB_HEALTH_INTERVAL_SECONDS`
(default 300s / 5 minutes) — see [Configuration reference](configuration-reference.md).
A transition (healthy → unhealthy or back) is also written to the
[audit log](audit-log.md) as `connection.health`, so a share that dropped
overnight is visible after the fact, not just at the moment you happen to look.

On the connection's detail page, **Recheck now** runs the check immediately
instead of waiting for the next sweep.

---

## 4. Using an ingest source in a write job

Only **healthy** ingest connections appear as suggestions. On
**[Jobs](jobs.md) → + Write job**, the **Source path** field offers each
healthy connection's mount path (as `hostname / share`) in its autocomplete
list — pick one, or still type any path on the host directly if you are not
using a managed connection. See [Archiving](archiving.md) for the rest of the
write-job form.

---

## 5. Restoring to a destination connection

On the [Search](search-catalog.md) page's restore-prep panel, **Restore
destination connection** offers every healthy `restore_destination` connection
(`hostname / share (mount path)`). Pick one and, optionally, a **subpath under
connection** (e.g. `RestoreJob-2027-03`) to namespace that particular restore
under its own folder rather than dumping straight into the share's root —
otherwise files land directly under the connection's mount path.

Selecting a connection takes priority over the free-text **custom path** field
below it; leave the connection dropdown on *"— custom path below —"* to use
that field (or leave both blank for the default `DATA_DIR/restores/…` on the
Vader VM itself, unchanged from before connections existed).

The resolved absolute path is stored on the restore request exactly as if you
had typed it — [Restore](restore.md) and the job that executes it need no
awareness that a connection was involved.

> **No connections yet?** The restore-prep panel says so and links straight
> to **+ Add restore destination**. Nothing stops you from using the custom
> path field in the meantime.

---

## 6. Removing a connection

On the connection's detail page, **Remove connection**:

1. disables and stops the automount unit (and the mount unit, if separately
   active);
2. deletes both unit files and reloads systemd;
3. deletes the credentials file;
4. removes the mount directory, **only if it is empty**;
5. deletes the database row and records a `connection.delete` audit entry.

This does not touch anything already catalogued from data ingested through an
ingest connection, or already restored through a restore destination — those
tape spans, catalog rows, and restored files are permanent regardless of
whether the connection that fed/received them still exists.

---

## 7. Simulator vs real

Like the tape library, the Connection Manager has two backends
(`SMB_BACKEND`, default `simulator`):

- **`simulator`** — the same credentials-file / mount-dir / unit-file layout,
  but under `DATA_DIR/sim/` and with no `systemctl` calls, so you can add,
  health-check, and remove connections on a laptop with no root and no real
  Windows machines. This is what you get out of the box.
- **`real`** — actually writes systemd units under `SMB_SYSTEMD_DIR` (default
  `/etc/systemd/system`) and drives them with `systemctl`. **Requires the Vader
  process to be able to write there and run `systemctl`** — root, or a sudoers
  rule scoped to exactly those commands. Set this only on the archive VM itself,
  once it is provisioned for real SMB ingest/restore.
