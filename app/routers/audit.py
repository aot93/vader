from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_db
from app.models import AuditLog, TapeEvent
from app.web import paginate, templates

router = APIRouter(prefix="/audit", dependencies=[Depends(require_auth)])


@router.get("")
def audit_page(request: Request, db: Session = Depends(get_db), page: int = 1, epage: int = 1):
    n_entries = db.scalar(select(func.count()).select_from(AuditLog))
    n_events = db.scalar(select(func.count()).select_from(TapeEvent))
    entry_pager = paginate(n_entries, page, per_page=100)
    event_pager = paginate(n_events, epage, per_page=100)
    entries = db.scalars(
        select(AuditLog).order_by(AuditLog.ts.desc())
        .limit(entry_pager["per_page"]).offset(entry_pager["offset"])
    ).all()
    events = db.scalars(
        select(TapeEvent).order_by(TapeEvent.started_at.desc())
        .limit(event_pager["per_page"]).offset(event_pager["offset"])
    ).all()
    return templates.TemplateResponse(request, "audit.html", {
        "entries": entries, "events": events,
        "entry_pager": entry_pager, "event_pager": event_pager,
    })
