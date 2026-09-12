from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.config import get_settings
from app.db import get_db
from app.jobs import enqueue
from app.models import BackupCategory, Job, JobStatus, JobType, Tape, TapeStatus
from app.services import source_manager as sm
from app.services.catalog import distinct_source_machines
from app.web import paginate, templates

router = APIRouter(prefix="/jobs", dependencies=[Depends(require_auth)])


@router.get("")
def list_jobs(request: Request, db: Session = Depends(get_db), page: int = 1):
    total = db.scalar(select(func.count()).select_from(Job))
    pager = paginate(total, page, per_page=50)
    jobs = db.scalars(
        select(Job).order_by(Job.created_at.desc())
        .limit(pager["per_page"]).offset(pager["offset"])
    ).all()
    return templates.TemplateResponse(request, "jobs_list.html", {"jobs": jobs, "pager": pager, "qs": ""})


@router.get("/new/write")
def new_write_job(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "job_write_form.html", {
        "machines": distinct_source_machines(db),
        "categories": [c.value for c in BackupCategory],
        "sources": sm.list_healthy_sources(db),
    })


@router.post("/new/write")
def create_write_job(
    request: Request,
    db: Session = Depends(get_db),
    source_path: str = Form(...),
    mode: str = Form("standard"),
    greedy_source: str = Form(""),
    project_name: str = Form(""),
    source_machine: str = Form(""),
    backup_category: str = Form("project_archive"),
    drive: int = Form(0),
):
    src = Path(source_path.strip())
    if not src.is_dir():
        raise HTTPException(status_code=400, detail=f"source path not found: {src}")
    params = {
        "source_path": str(src),
        "mode": mode,
        "greedy_source": greedy_source.strip() or None,
        "project_name": project_name.strip() or None,
        "source_machine": source_machine.strip() or None,
        "backup_category": backup_category,
        "drive": drive,
    }
    job = enqueue(db, JobType.write, params)
    db.commit()
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.get("/new/verify")
def new_verify_job(request: Request, barcode: str = ""):
    return templates.TemplateResponse(request, "job_verify_form.html", {
        "barcode": barcode.strip(),
        "default_fraction": get_settings().default_verify_sample_fraction,
    })


@router.post("/new/verify")
def create_verify_job(
    request: Request,
    db: Session = Depends(get_db),
    barcode: str = Form(...),
    full: bool = Form(False),
    sample_fraction: str = Form(""),
    drive: int = Form(0),
):
    try:
        frac = float(sample_fraction) if sample_fraction.strip() else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="sample fraction must be a number") from exc
    if frac is not None and not 0 < frac <= 1:
        raise HTTPException(status_code=400, detail="sample fraction must be between 0 and 1")
    params = {
        "barcode": barcode.strip(),
        "full": full,
        "sample_fraction": frac,
        "drive": drive,
    }
    job = enqueue(db, JobType.verify, params)
    db.commit()
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.get("/new/batch_format")
def new_batch_format_job(request: Request, db: Session = Depends(get_db)):
    scratch = db.scalars(
        select(Tape.barcode).where(Tape.status == TapeStatus.scratch).order_by(Tape.barcode)
    ).all()
    return templates.TemplateResponse(request, "job_batch_format_form.html", {
        "scratch_barcodes": scratch,
    })


@router.post("/new/batch_format")
def create_batch_format_job(
    request: Request,
    db: Session = Depends(get_db),
    barcodes: str = Form(...),
    force: bool = Form(False),
):
    parsed = [b.strip() for b in barcodes.replace(",", "\n").splitlines() if b.strip()]
    if not parsed:
        raise HTTPException(status_code=400, detail="no tape barcodes given")
    params = {"barcodes": parsed, "force": force}
    job = enqueue(db, JobType.batch_format, params)
    db.commit()
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.post("/new/backup")
def create_backup_job(request: Request, db: Session = Depends(get_db)):
    job = enqueue(db, JobType.backup, {})
    db.commit()
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.get("/{job_id}")
def job_detail(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="unknown job")
    partial = request.query_params.get("partial") == "1"
    template = "job_progress_partial.html" if partial else "job_detail.html"
    return templates.TemplateResponse(request, template, {"job": job})


@router.post("/{job_id}/cancel")
def cancel_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="unknown job")
    if job.status in (JobStatus.queued, JobStatus.running):
        job.cancel_requested = True
        if job.status == JobStatus.queued:
            job.status = JobStatus.cancelled
    db.commit()
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/{job_id}/retry")
def retry_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="unknown job")
    clone = enqueue(db, job.job_type, job.params or {})
    db.commit()
    return RedirectResponse(f"/jobs/{clone.id}", status_code=303)
