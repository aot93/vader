"""Tiny helper around the app-level audit log (framework doc §3.7)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AuditLog


def record_audit(
    db: Session,
    *,
    actor: str = "operator",
    action: str,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    detail: dict | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        detail=detail or {},
    )
    db.add(entry)
    db.flush()
    return entry
