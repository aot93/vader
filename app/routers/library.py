from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_db
from app.hardware import HardwareError, get_hardware
from app.jobs import enqueue
from app.models import JobType, Tape
from app.services import library as lib
from app.web import templates

router = APIRouter(prefix="/library", dependencies=[Depends(require_auth)])


def _render(request: Request, db: Session, *, error: str | None = None, notice: str | None = None):
    hw = get_hardware()
    try:
        state = hw.library_status()
    except Exception as exc:  # noqa: BLE001
        state = None
        error = error or str(exc)
    tapes = {t.barcode: t for t in db.scalars(select(Tape)).all()}
    return templates.TemplateResponse(request, "library.html", {
        "state": state, "hw_backend": hw.backend, "error": error, "notice": notice,
        "tape_by_barcode": tapes,
    })


@router.get("")
def library_page(request: Request, db: Session = Depends(get_db)):
    return _render(request, db)


@router.post("/inventory")
def do_inventory(request: Request, db: Session = Depends(get_db)):
    try:
        lib.refresh_inventory(db, initiated_by="operator")
        db.commit()
        return _render(request, db, notice="Inventory refreshed from the changer.")
    except HardwareError as exc:
        db.rollback()
        return _render(request, db, error=str(exc))


@router.post("/load")
def do_load(request: Request, slot: int = Form(...), drive: int = Form(...),
            db: Session = Depends(get_db)):
    try:
        lib.load_tape(db, slot, drive, initiated_by="operator")
        db.commit()
        return _render(request, db, notice=f"Loaded slot {slot} → drive {drive}.")
    except HardwareError as exc:
        db.rollback()
        return _render(request, db, error=str(exc))


@router.post("/unload")
def do_unload(request: Request, slot: int = Form(...), drive: int = Form(...),
              db: Session = Depends(get_db)):
    try:
        lib.unload_tape(db, slot, drive, initiated_by="operator")
        db.commit()
        return _render(request, db, notice=f"Unloaded drive {drive} → slot {slot}.")
    except HardwareError as exc:
        db.rollback()
        return _render(request, db, error=str(exc))


# --- Utilities (deliberate, separated maintenance actions — §3.1) ------------


@router.post("/format")
def do_format(request: Request, drive: int = Form(...), barcode: str = Form(...),
              force: bool = Form(False), db: Session = Depends(get_db)):
    barcode = barcode.strip()
    # Checked here too, not just inside format_tape() once the job actually
    # runs — mkltfs isn't reached until the job worker picks this up, so
    # without this the operator submits the form, watches it get queued,
    # then only finds out several seconds later (on the job's page) that the
    # tape was never loaded. Failing the form immediately is strictly better.
    try:
        lib.require_drive_loaded(get_hardware().library_status(), drive, barcode)
    except HardwareError as exc:
        return _render(request, db, error=str(exc))
    # Runs as a background job, not inline — mkltfs's optimize pass can take
    # up to ~2h/tape on LTO-9, so blocking the request would leave the
    # operator staring at a hung page with no way to tell it's still working.
    job = enqueue(db, JobType.format, {"drive": drive, "barcode": barcode, "force": force})
    db.commit()
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.post("/unlock")
def do_unlock(request: Request, drive: int = Form(...), db: Session = Depends(get_db)):
    try:
        lib.unlock_drive(db, drive, initiated_by="operator")
        db.commit()
        return _render(request, db, notice=f"Sent ALLOW MEDIUM REMOVAL to drive {drive}.")
    except HardwareError as exc:
        db.rollback()
        return _render(request, db, error=str(exc))


@router.post("/clean")
def do_clean(request: Request, drive: int = Form(...), cleaning_slot: int = Form(...),
             db: Session = Depends(get_db)):
    try:
        lib.clean_drive(db, drive, cleaning_slot, initiated_by="operator")
        db.commit()
        return _render(request, db, notice=f"Cleaning cycle run on drive {drive}.")
    except HardwareError as exc:
        db.rollback()
        return _render(request, db, error=str(exc))


@router.post("/retire")
def do_retire(request: Request, barcode: str = Form(...), reason: str = Form(""),
              db: Session = Depends(get_db)):
    try:
        lib.retire_tape(db, barcode.strip(), reason=reason.strip(), initiated_by="operator")
        db.commit()
        return RedirectResponse(f"/tapes/{barcode.strip()}", status_code=303)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HardwareError as exc:
        db.rollback()
        return _render(request, db, error=str(exc))
