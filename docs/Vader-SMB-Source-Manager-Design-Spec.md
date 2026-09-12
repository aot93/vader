# Vader SMB Source Manager — Design Specification

**Version:** 1.0
**Author:** Anthony
**Target:** Claude Code (implementation)

> Implementation note: this is the design brief as given. The shipped feature
> follows Vader's existing module boundaries where they differ from the literal
> file paths named below (e.g. mount provisioning lives in `app/mounts/`
> alongside `app/hardware/`, not inline in `app/services/intake.py`) — see
> [Sources](../user-manual/sources.md) for the feature as built.

## Overview

Vader currently expects ingest sources to be available as mounted directories
on the Linux VM. Operators must manually configure SMB/CIFS mounts for Windows
machines, which is error-prone and unfriendly.

This feature adds a Source Manager to Vader:

- Operators can add Windows machines via the UI
- Vader creates mount points, credentials files, and systemd automount units
- Vader tests mount health and displays status
- Vader stores source configuration in the database
- Vader exposes mounted sources to the intake pipeline
- Vader logs all mount/unmount operations in the audit log

This makes ingest configuration safe, durable, and operator-friendly.

## Goals

- Allow operators to configure Windows SMB shares entirely through the Vader UI
- Automatically create:
  - credentials files
  - mount directories
  - systemd `.mount` and `.automount` units
- Automatically enable + start mounts
- Provide health checks and status indicators
- Integrate sources into the existing intake workflow
- Maintain audit logs for all mount operations
- Ensure mounts survive VM reboots
- Ensure mounts reconnect automatically if Windows machines reboot

## Non-Goals

- No full SMB browsing
- No write access (read-only ingest only)
- No storing plaintext passwords in the database
- No editing systemd units outside Vader's control

## Architecture

### Components

- Database model
- Credential file generator
- Mount directory manager
- Systemd unit generator
- Mount health checker
- FastAPI router
- Jinja2 templates
- Audit logging integration

### Database Model

File: `app/models.py`

```python
class Source(Base):
    __tablename__ = "sources"

    id = Column(Integer, primary_key=True)
    hostname = Column(String, nullable=False)
    share = Column(String, nullable=False)
    mount_path = Column(String, nullable=False)
    credentials_path = Column(String, nullable=False)
    smb_version = Column(String, default="3.0")
    domain = Column(String, nullable=True)
    username = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_health = Column(Boolean, default=None)
```

Passwords are not stored in the database.

### Credential File Format

Stored under:

```
DATA_DIR/smb_credentials/<hostname>.creds
```

Contents:

```
username=<username>
password=<password>
domain=<domain>
```

Permissions:

```
chmod 600
```

### Mount Directory

Created under:

```
/mnt/vader/<hostname>
```

Owned by the Vader user.

### Systemd Units

**Automount unit** — `/etc/systemd/system/mnt-vader-<hostname>.automount`

```ini
[Unit]
Description=Automount for SMB source <hostname>

[Automount]
Where=/mnt/vader/<hostname>

[Install]
WantedBy=multi-user.target
```

**Mount unit** — `/etc/systemd/system/mnt-vader-<hostname>.mount`

```ini
[Unit]
Description=Mount SMB source <hostname>

[Mount]
What=//<hostname>/<share>
Where=/mnt/vader/<hostname>
Type=cifs
Options=credentials=<credentials_path>,_netdev,nofail,vers=<smb_version>,ro

[Install]
WantedBy=multi-user.target
```

### Health Check Logic

File: `app/services/source_health.py`

```python
def check_source_health(source: Source) -> bool:
    try:
        entries = os.listdir(source.mount_path)
        return True
    except Exception:
        return False
```

A background job runs every 5 minutes:

- Updates `last_health`
- Logs failures
- Shows status in UI

### FastAPI Router

File: `app/routers/sources.py`

Endpoints:

- `GET /sources` — List all sources + health status.
- `GET /sources/new` — Form to add a new source.
- `POST /sources` — Create: credentials file, mount directory, systemd units,
  enable + start automount, DB entry, audit log entry.
- `GET /sources/{id}` — View source details.
- `POST /sources/{id}/delete` — Remove: systemd units, credentials file, mount
  directory, DB entry, audit log entry.

### UI Templates

Directory: `app/templates/sources/`

- **index.html** — Table of sources, health indicator (green/red), "Add Source" button.
- **new.html** — Form fields: Hostname/IP, Share name, Username, Password,
  Domain (optional), SMB version (default 3.0).
- **detail.html** — Shows mount path, health status, last health check, delete button.

### Audit Logging

Every mount/unmount/change is logged via:

```python
log_event("source.add", details)
log_event("source.delete", details)
log_event("source.health", details)
```

### Integration with Intake

Modify `services/intake.py`:

- Add a function to list all healthy sources
- Present them as selectable ingest roots

Example:

```python
def get_ingest_sources():
    return [
        s.mount_path
        for s in db.query(Source).filter(Source.last_health == True)
    ]
```

## Security Considerations

- Credentials stored only in filesystem with `chmod 600`
- Passwords never stored in DB
- Mounts are read-only (`ro`)
- Operators cannot view passwords after creation
- Systemd units validated before writing
- Hostnames sanitized

## Testing Plan

### Unit tests

- Credential file generator
- Systemd unit generator
- Health checker

### Integration tests

- Add → mount → health check
- Delete → unmount → cleanup

### Manual tests

- Windows machine reboot
- VM reboot
- Network outage
- Multiple sources

## Future Enhancements

- SMB browsing to auto-discover shares
- Per-source bandwidth throttling
- Per-source ingest quotas
- Offline warnings in the main dashboard

## Implementation Notes for Claude Code

- Use existing Vader patterns (services, routers, templates)
- Follow SQLAlchemy 2.0 style
- Use synchronous FastAPI (matching Vader's architecture)
- Use subprocess for systemctl calls
- Ensure idempotency (re-running creation should not break)
- Ensure cleanup removes all files + units
