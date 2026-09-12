"""Source Manager — turn an operator-entered Windows SMB share into a mounted,
auto-reconnecting ingest root without any manual mount setup on the VM (Vader
SMB Source Manager design spec v1.0).

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
from app.models import Source, SourceHealth
from app.mounts import MountError, SourceSpec, get_mount_backend
from app.mounts.base import sanitize_hostname, sanitize_share, sanitize_simple, unit_name_for
from app.services.audit import record_audit

_SMB_VERSIONS = {"1.0", "2.0", "2.1", "3.0", "3.02", "3.11"}


def _now() -> datetime:
    return datetime.now(UTC)


def _spec_for(source: Source) -> SourceSpec:
    return SourceSpec(
        hostname=source.hostname, share=source.share, mount_path=source.mount_path,
        credentials_path=source.credentials_path, unit_name=source.unit_name,
        smb_version=source.smb_version, domain=source.domain, username=source.username,
    )


def create_source(
    db: Session,
    *,
    hostname: str,
    share: str,
    username: str,
    password: str,
    domain: str = "",
    smb_version: str = "3.0",
    initiated_by: str = "operator",
) -> Source:
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
    if db.scalar(select(Source).where(Source.hostname == hostname)):
        raise ValueError(f"a source for {hostname} already exists")

    settings = get_settings()
    backend = get_mount_backend()
    source = Source(
        hostname=hostname,
        share=share,
        # Ask the backend where it actually mounts things — the simulator's
        # root is not smb_mount_base, and a Source.mount_path that points
        # somewhere the backend never touches is exactly the "healthy but the
        # write-job source path doesn't exist" bug.
        mount_path=str(backend.mount_root() / hostname),
        credentials_path=str(settings.data_dir / "smb_credentials" / f"{hostname}.creds"),
        unit_name=unit_name_for(hostname),
        smb_version=smb_version,
        domain=domain or None,
        username=username,
    )

    try:
        backend.provision(_spec_for(source), password)
    except MountError as exc:
        raise MountError(f"could not provision {hostname}: {exc}") from exc

    db.add(source)
    db.flush()
    record_audit(
        db, actor=initiated_by, action="source.add", entity_type="source", entity_id=source.id,
        detail={
            "hostname": hostname, "share": share, "mount_path": source.mount_path,
            "backend": backend.backend,
        },
    )
    return source


def delete_source(db: Session, source_id: int, *, initiated_by: str = "operator") -> None:
    source = db.get(Source, source_id)
    if not source:
        raise ValueError(f"unknown source id {source_id}")
    backend = get_mount_backend()
    try:
        backend.deprovision(_spec_for(source))
    except MountError as exc:
        raise MountError(f"could not clean up {source.hostname}: {exc}") from exc
    record_audit(
        db, actor=initiated_by, action="source.delete", entity_type="source",
        entity_id=source.id, detail={"hostname": source.hostname, "share": source.share},
    )
    db.delete(source)
    db.flush()


def check_health(db: Session, source: Source) -> Source:
    backend = get_mount_backend()
    healthy, error = backend.check_health(_spec_for(source))
    was = source.last_health
    source.last_health = SourceHealth.healthy if healthy else SourceHealth.unhealthy
    source.last_checked_at = _now()
    source.last_error = None if healthy else (error or "unknown error")
    db.flush()
    if was != SourceHealth.unknown and was != source.last_health:
        record_audit(
            db, action="source.health", entity_type="source", entity_id=source.id,
            detail={"hostname": source.hostname, "healthy": healthy, "error": source.last_error},
        )
    return source


def check_all_sources(db: Session) -> list[Source]:
    """Sweep every configured source. One bad source (a backend raising
    unexpectedly) must not stop the rest from being checked."""
    sources = db.scalars(select(Source)).all()
    for source in sources:
        try:
            check_health(db, source)
        except Exception as exc:  # noqa: BLE001
            source.last_health = SourceHealth.unhealthy
            source.last_checked_at = _now()
            source.last_error = str(exc)
    db.flush()
    return sources


def list_healthy_sources(db: Session) -> list[Source]:
    """Sources currently mounted and healthy — selectable ingest roots for a
    write job (design spec "Integration with Intake")."""
    return list(
        db.scalars(
            select(Source).where(Source.last_health == SourceHealth.healthy)
            .order_by(Source.hostname)
        ).all()
    )
