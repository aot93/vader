"""Connection Manager — turn an operator-entered Windows SMB share into a
mounted, auto-reconnecting root without any manual mount setup on the VM
(originally "Vader SMB Source Manager design spec v1.0", covering ingest
only; extended to also offer restore destinations).

A connection's ``purpose`` is fixed at creation and controls how the mount is
provisioned:

* ``ingest`` — read-only, offered as a write-job source root. The original,
  unchanged behaviour — same paths/unit names a pre-existing real deployment
  already has, so this rename+extension never orphans an existing mount.
* ``restore_destination`` — read-write, offered as a restore target. Always a
  distinct mount from any ingest connection for the same hostname (see
  ``app.mounts.base.unit_name_for`` / ``mount_dir_name``).

Provisioning itself is delegated to a :class:`~app.mounts.base.MountBackend`
(simulator by default, systemd on a real host — ``SMB_BACKEND``); this module
owns the database row, path/unit-name derivation, input sanitisation, and
audit logging.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Connection, ConnectionHealth, ConnectionPurpose
from app.mounts import ConnectionSpec, MountError, get_mount_backend
from app.mounts.base import (
    mount_dir_name,
    sanitize_hostname,
    sanitize_share,
    sanitize_simple,
    unit_name_for,
)
from app.services.audit import record_audit

_SMB_VERSIONS = {"1.0", "2.0", "2.1", "3.0", "3.02", "3.11"}


def _now() -> datetime:
    return datetime.now(UTC)


def _spec_for(connection: Connection) -> ConnectionSpec:
    return ConnectionSpec(
        hostname=connection.hostname, share=connection.share, mount_path=connection.mount_path,
        credentials_path=connection.credentials_path, unit_name=connection.unit_name,
        smb_version=connection.smb_version, domain=connection.domain, username=connection.username,
        read_only=connection.purpose == ConnectionPurpose.ingest,
    )


def create_connection(
    db: Session,
    *,
    hostname: str,
    share: str,
    username: str,
    password: str,
    domain: str = "",
    smb_version: str = "3.0",
    purpose: ConnectionPurpose | str = ConnectionPurpose.ingest,
    initiated_by: str = "operator",
) -> Connection:
    hostname = sanitize_hostname(hostname)
    share = sanitize_share(share)
    username = sanitize_simple(username, "username")
    if not username:
        raise ValueError("username is required")
    domain = sanitize_simple(domain, "domain")
    smb_version = smb_version.strip() or "3.0"
    if smb_version not in _SMB_VERSIONS:
        raise ValueError(f"unsupported SMB version: {smb_version}")
    if not password:
        raise ValueError("password is required")
    try:
        purpose = ConnectionPurpose(purpose)
    except ValueError as exc:
        raise ValueError(f"unknown connection purpose: {purpose!r}") from exc
    if db.scalar(
        select(Connection).where(Connection.hostname == hostname, Connection.purpose == purpose)
    ):
        raise ValueError(f"a {purpose.value} connection for {hostname} already exists")

    settings = get_settings()
    backend = get_mount_backend()
    connection = Connection(
        hostname=hostname,
        share=share,
        purpose=purpose,
        # Ask the backend where it actually mounts things — the simulator's
        # root is not smb_mount_base, and a Connection.mount_path that points
        # somewhere the backend never touches is exactly the "healthy but the
        # write-job source path doesn't exist" bug.
        mount_path=str(backend.mount_root() / mount_dir_name(hostname, purpose.value)),
        credentials_path=str(
            settings.data_dir / "smb_credentials"
            / f"{mount_dir_name(hostname, purpose.value)}.creds"
        ),
        unit_name=unit_name_for(hostname, purpose.value),
        smb_version=smb_version,
        domain=domain or None,
        username=username,
    )

    try:
        backend.provision(_spec_for(connection), password)
    except MountError as exc:
        raise MountError(f"could not provision {hostname}: {exc}") from exc

    db.add(connection)
    db.flush()
    record_audit(
        db, actor=initiated_by, action="connection.add", entity_type="connection",
        entity_id=connection.id,
        detail={
            "hostname": hostname, "share": share, "purpose": purpose.value,
            "mount_path": connection.mount_path, "backend": backend.backend,
        },
    )
    return connection


def delete_connection(db: Session, connection_id: int, *, initiated_by: str = "operator") -> None:
    connection = db.get(Connection, connection_id)
    if not connection:
        raise ValueError(f"unknown connection id {connection_id}")
    backend = get_mount_backend()
    try:
        backend.deprovision(_spec_for(connection))
    except MountError as exc:
        raise MountError(f"could not clean up {connection.hostname}: {exc}") from exc
    record_audit(
        db, actor=initiated_by, action="connection.delete", entity_type="connection",
        entity_id=connection.id,
        detail={"hostname": connection.hostname, "share": connection.share,
                "purpose": connection.purpose.value},
    )
    db.delete(connection)
    db.flush()


def check_health(db: Session, connection: Connection) -> Connection:
    backend = get_mount_backend()
    healthy, error = backend.check_health(_spec_for(connection))
    was = connection.last_health
    connection.last_health = ConnectionHealth.healthy if healthy else ConnectionHealth.unhealthy
    connection.last_checked_at = _now()
    connection.last_error = None if healthy else (error or "unknown error")
    db.flush()
    if was != ConnectionHealth.unknown and was != connection.last_health:
        record_audit(
            db, action="connection.health", entity_type="connection", entity_id=connection.id,
            detail={"hostname": connection.hostname, "healthy": healthy,
                    "error": connection.last_error},
        )
    return connection


def check_all_connections(db: Session) -> list[Connection]:
    """Sweep every configured connection. One bad connection (a backend
    raising unexpectedly) must not stop the rest from being checked."""
    connections = db.scalars(select(Connection)).all()
    for connection in connections:
        try:
            check_health(db, connection)
        except Exception as exc:  # noqa: BLE001
            connection.last_health = ConnectionHealth.unhealthy
            connection.last_checked_at = _now()
            connection.last_error = str(exc)
    db.flush()
    return connections


def list_healthy_connections(
    db: Session, *, purpose: ConnectionPurpose = ConnectionPurpose.ingest,
) -> list[Connection]:
    """Connections of the given purpose currently mounted and healthy —
    selectable ingest roots for a write job (design spec "Integration with
    Intake"), or restore targets for a restore request."""
    return list(
        db.scalars(
            select(Connection)
            .where(Connection.purpose == purpose, Connection.last_health == ConnectionHealth.healthy)
            .order_by(Connection.hostname)
        ).all()
    )
