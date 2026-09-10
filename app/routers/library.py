from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.db import get_db
from app.hardware import HardwareError, get_hardware
from app.models import Tape
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
    try:
        lib.format_tape(db, drive, barcode.strip(), force=force, initiated_by="operator")
        db.commit()
        return _render(request, db, notice=f"Formatted {barcode} for LTFS on drive {drive}.")
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
