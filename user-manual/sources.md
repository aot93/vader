# Sources

**What this covers:** the `/sources` screen — adding a Windows SMB share as an
auto-mounted ingest root, reading its health status, picking it on the write-job
form, and removing it. This replaces manually running `mount.cifs` (or editing
`/etc/fstab`) on the archive VM for every machine you pull from.

> No screenshot yet for this page — it shipped after the rest of this manual's
> screenshots were captured. The layout is a plain list + detail pair, the same
> shape as [Tapes](tapes.md).

---

## 1. What a source is

A **source** is one Windows machine's SMB share, registered once through
**Sources → + Add source**. Vader then:

1. writes a **credentials file** (username / password / domain) to
   `DATA_DIR/smb_credentials/<hostname>.creds`, mode `600` — never in the
   database, and not viewable again through the UI after you submit the form;
2. creates the **mount point** (`SMB_MOUNT_BASE/<hostname>`, default
   `/mnt/vader/<hostname>`);
3. generates a **systemd `.mount` + `.automount` unit pair** for it and starts
   the automount, so the share mounts on first access and **reconnects on its
   own** if the Windows machine reboots, and **survives a VM reboot** without
   you doing anything;
4. records a `source.add` [audit log](audit-log.md) entry.

One source per hostname — the mount path is derived from the hostname alone, so
a second share on the same machine needs a second hostname/IP entry (or a
different share name is not distinguished in the mount path).

This is a read-only ingest mount (`ro` in the mount options): Vader never writes
back to the source share.

---

## 2. Adding a source

**Sources → + Add source**, then:

| Field | Notes |
|---|---|
| Hostname or IP * | Sanitised strictly (letters, digits, dots, hyphens only) — this value is written into a systemd unit file, so anything else is rejected rather than risking it. |
| Share name * | The SMB share name on that machine. |
| Username * | The Windows account to authenticate as. |
| Password * | Written once into the mode-`600` credentials file, then discarded — Vader keeps no other copy. |
| Domain | Optional. |
| SMB version | `3.0` by default; drop to `2.1` or `1.0` only for an old NAS/share that needs it. |

**Add source** provisions immediately (credentials file → mount dir → systemd
units → `systemctl daemon-reload` && `enable --now`) and takes you to the new
source's detail page. A duplicate hostname, or a value that fails sanitisation,
is rejected with the reason shown on the form — nothing is created.

---

## 3. Health

Each source shows a health pill:

| Status | Meaning |
|---|---|
| `unknown` | Not checked yet (just added). |
| `healthy` | The mount directory is listable right now. |
| `unhealthy` | Listing the mount directory failed — the OS error is shown under the pill / on the detail page as **Last error**. |

A background sweep re-checks every source every `SMB_HEALTH_INTERVAL_SECONDS`
(default 300s / 5 minutes) — see [Configuration reference](configuration-reference.md).
A transition (healthy → unhealthy or back) is also written to the
[audit log](audit-log.md) as `source.health`, so a share that dropped overnight
is visible after the fact, not just at the moment you happen to look.

On the source's detail page, **Recheck now** runs the check immediately instead
of waiting for the next sweep.

---

## 4. Using a source in a write job

Only **healthy** sources appear as suggestions. On **[Jobs](jobs.md) → + Write
job**, the **Source path** field offers each healthy source's mount path (as
`hostname / share`) in its autocomplete list — pick one, or still type any path
on the host directly if you are not using a managed source. See
[Archiving](archiving.md) for the rest of the write-job form.

---

## 5. Removing a source

On the source's detail page, **Remove source**:

1. disables and stops the automount unit (and the mount unit, if separately
   active);
2. deletes both unit files and reloads systemd;
3. deletes the credentials file;
4. removes the mount directory, **only if it is empty**;
5. deletes the database row and records a `source.delete` audit entry.

This does not touch anything already catalogued from data ingested through the
share — those tape spans and catalog rows are permanent regardless of whether
the source that fed them still exists.

---

## 6. Simulator vs real

Like the tape library, the Source Manager has two backends
(`SMB_BACKEND`, default `simulator`):

- **`simulator`** — the same credentials-file / mount-dir / unit-file layout,
  but under `DATA_DIR/sim/` and with no `systemctl` calls, so you can add,
  health-check, and remove sources on a laptop with no root and no real Windows
  machines. This is what you get out of the box.
- **`real`** — actually writes systemd units under `SMB_SYSTEMD_DIR` (default
  `/etc/systemd/system`) and drives them with `systemctl`. **Requires the Vader
  process to be able to write there and run `systemctl`** — root, or a sudoers
  rule scoped to exactly those commands. Set this only on the archive VM itself,
  once it is provisioned for real SMB ingest.
