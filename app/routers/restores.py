from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_db
from app.jobs import enqueue
from app.models import Connection, ContentItem, Job, JobType, RestoreRequest, SequenceContainer
from app.web import paginate, templates

router = APIRouter(prefix="/restores", dependencies=[Depends(require_auth)])


@router.get("")
def list_restores(request: Request, db: Session = Depends(get_db), page: int = 1):
    total = db.scalar(select(func.count()).select_from(RestoreRequest))
    pager = paginate(total, page, per_page=50)
    reqs = db.scalars(
        select(RestoreRequest).order_by(RestoreRequest.requested_at.desc())
        .limit(pager["per_page"]).offset(pager["offset"])
    ).all()
    return templates.TemplateResponse(request, "restores_list.html",
                                      {"reqs": reqs, "pager": pager, "qs": ""})


@router.get("/{restore_id}")
def restore_detail(restore_id: int, request: Request, db: Session = Depends(get_db)):
    req = db.get(RestoreRequest, restore_id)
    if not req:
        raise HTTPException(status_code=404, detail="unknown restore request")
    seqs = db.scalars(
        select(SequenceContainer).where(SequenceContainer.id.in_(req.sequence_container_ids or [0]))
    ).all()
    items = db.scalars(
        select(ContentItem).where(ContentItem.id.in_(req.content_item_ids or [0]))
    ).all()
    job = None
    if req.plan.get("job_id"):
        job = db.get(Job, req.plan["job_id"])
    blocked_warnings = [w for w in (req.warnings or []) if "not in the library" in w]
    destination_connection = (
        db.get(Connection, req.destination_connection_id) if req.destination_connection_id else None
    )
    return templates.TemplateResponse(request, "restore_detail.html", {
        "req": req, "seqs": seqs, "items": items, "job": job,
        "blocked_warnings": blocked_warnings, "destination_connection": destination_connection,
    })


@router.post("/{restore_id}/run")
def run_restore_job(restore_id: int, db: Session = Depends(get_db)):
    req = db.get(RestoreRequest, restore_id)
    if not req:
        raise HTTPException(status_code=404, detail="unknown restore request")
    job = enqueue(db, JobType.restore, {"restore_id": restore_id})
    plan = dict(req.plan)
    plan["job_id"] = job.id
    req.plan = plan
    db.commit()
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)
