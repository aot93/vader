from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_db
from app.models import (
    ContentItem,
    ContentTapeSpan,
    ReadError,
    SequenceContainer,
    Tape,
    TapeEvent,
    TapeStatus,
)
from app.services.audit import record_audit
from app.web import templates

router = APIRouter(prefix="/tapes", dependencies=[Depends(require_auth)])


@router.get("")
def list_tapes(request: Request, db: Session = Depends(get_db), status: str | None = None):
    stmt = select(Tape).order_by(Tape.barcode)
    if status:
        stmt = stmt.where(Tape.status == status)
    tapes = db.scalars(stmt).all()
    span_counts = dict(
        db.execute(
            select(ContentTapeSpan.tape_id, func.count()).group_by(ContentTapeSpan.tape_id)
        ).all()
    )
    return templates.TemplateResponse(request, "tapes_list.html", {
        "tapes": tapes, "span_counts": span_counts,
        "statuses": [s.value for s in TapeStatus], "active_status": status,
    })


@router.get("/new")
def new_tape_form(request: Request):
    return templates.TemplateResponse(request, "tape_form.html", {
        "tape": None, "statuses": [s.value for s in TapeStatus],
    })


@router.post("/new")
def create_tape(
    request: Request,
    barcode: str = Form(...),
    lto_generation: str = Form(""),
    capacity_native_bytes: str = Form(""),
    status: str = Form("scratch"),
    physical_location: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    if db.scalar(select(Tape).where(Tape.barcode == barcode.strip())):
        raise HTTPException(status_code=409, detail="barcode already exists")
    tape = Tape(
        barcode=barcode.strip(),
        lto_generation=lto_generation.strip() or None,
        capacity_native_bytes=int(capacity_native_bytes) if capacity_native_bytes.strip() else None,
        status=TapeStatus(status),
        physical_location=physical_location.strip() or None,
        notes=notes.strip() or None,
    )
    db.add(tape)
    db.flush()
    record_audit(db, action="tape.create", entity_type="tape", entity_id=tape.barcode)
    db.commit()
    return RedirectResponse(f"/tapes/{tape.barcode}", status_code=303)


@router.get("/{barcode}")
def tape_detail(barcode: str, request: Request, db: Session = Depends(get_db)):
    tape = db.scalar(select(Tape).where(Tape.barcode == barcode))
    if not tape:
        raise HTTPException(status_code=404, detail="unknown tape")
    spans = db.scalars(
        select(ContentTapeSpan).where(ContentTapeSpan.tape_id == tape.id)
        .order_by(ContentTapeSpan.ltfs_path, ContentTapeSpan.part_index)
    ).all()
    rows = []
    for span in spans:
        if span.sequence_container_id:
            c = db.get(SequenceContainer, span.sequence_container_id)
            rows.append({"span": span, "kind": "sequence", "obj": c,
                         "title": f"{c.project_name} / {c.sequence_name or ''} / {c.shot_name}"})
        else:
            it = db.get(ContentItem, span.content_item_id)
            rows.append({"span": span, "kind": "item", "obj": it,
                         "title": f"{it.project_name or ''} / {it.filename}"})
    events = db.scalars(
        select(TapeEvent).where(TapeEvent.tape_id == tape.id)
        .order_by(TapeEvent.started_at.desc()).limit(50)
    ).all()
    read_errors = db.scalars(
        select(ReadError).where(ReadError.tape_id == tape.id)
        .order_by(ReadError.detected_at.desc()).limit(50)
    ).all()
    return templates.TemplateResponse(request, "tape_detail.html", {
        "tape": tape, "rows": rows, "events": events, "read_errors": read_errors,
        "statuses": [s.value for s in TapeStatus],
    })


@router.post("/{barcode}/edit")
def edit_tape(
    barcode: str,
    request: Request,
    lto_generation: str = Form(""),
    capacity_native_bytes: str = Form(""),
    status: str = Form(...),
    physical_location: str = Form(""),
    offsite_pair_barcode: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    tape = db.scalar(select(Tape).where(Tape.barcode == barcode))
    if not tape:
        raise HTTPException(status_code=404, detail="unknown tape")
    tape.lto_generation = lto_generation.strip() or None
    tape.capacity_native_bytes = (
        int(capacity_native_bytes) if capacity_native_bytes.strip() else tape.capacity_native_bytes
    )
    tape.status = TapeStatus(status)
    tape.physical_location = physical_location.strip() or None
    tape.notes = notes.strip() or None
    if offsite_pair_barcode.strip():
        pair = db.scalar(select(Tape).where(Tape.barcode == offsite_pair_barcode.strip()))
        tape.offsite_pair_tape_id = pair.id if pair else None
    record_audit(db, action="tape.edit", entity_type="tape", entity_id=barcode)
    db.commit()
    return RedirectResponse(f"/tapes/{barcode}", status_code=303)
