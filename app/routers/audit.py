from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_db
from app.models import AuditLog, TapeEvent
from app.web import templates

router = APIRouter(prefix="/audit", dependencies=[Depends(require_auth)])


@router.get("")
def audit_page(request: Request, db: Session = Depends(get_db)):
    entries = db.scalars(select(AuditLog).order_by(AuditLog.ts.desc()).limit(300)).all()
    events = db.scalars(select(TapeEvent).order_by(TapeEvent.started_at.desc()).limit(300)).all()
    return templates.TemplateResponse(request, "audit.html", {"entries": entries, "events": events})
