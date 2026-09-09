from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_db
from app.hardware import get_hardware
from app.models import Job, JobStatus, Tape, TapeEvent, TapeStatus
from app.services.catalog import catalog_totals
from app.services.verification import tapes_due_for_reverification
from app.web import templates

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    hw = get_hardware()
    try:
        state = hw.library_status()
        hw_error = None
    except Exception as exc:  # noqa: BLE001
        state = None
        hw_error = str(exc)

    status_counts = dict(
        db.execute(select(Tape.status, func.count()).group_by(Tape.status)).all()
    )
    counts = {s.value: status_counts.get(s, 0) for s in TapeStatus}

    active_jobs = db.scalars(
        select(Job).where(Job.status.in_([JobStatus.queued, JobStatus.running]))
        .order_by(Job.created_at)
    ).all()
    recent_jobs = db.scalars(select(Job).order_by(Job.created_at.desc()).limit(8)).all()
    recent_events = db.scalars(
        select(TapeEvent).order_by(TapeEvent.started_at.desc()).limit(12)
    ).all()
    due = tapes_due_for_reverification(db)

    return templates.TemplateResponse(request, "dashboard.html", {
        "state": state,
        "hw_error": hw_error,
        "hw_backend": hw.backend,
        "counts": counts,
        "totals": catalog_totals(db),
        "active_jobs": active_jobs,
        "recent_jobs": recent_jobs,
        "recent_events": recent_events,
        "reverify_due": due,
        "tape_by_barcode": {t.barcode: t for t in db.scalars(select(Tape)).all()},
    })


@router.get("/health")
def health():
    return {"status": "ok"}
