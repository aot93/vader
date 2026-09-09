"""Library / drive control — every hardware action goes through here so it is
logged as a ``tape_events`` row (who, when, what, result) per framework doc §3.1
and §3.7, and the cached ``library_slots`` snapshot stays current.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hardware import HardwareError, LibraryState, get_hardware
from app.models import (
    EventResult,
    LibrarySlot,
    Tape,
    TapeEvent,
    TapeEventType,
    TapeStatus,
)
from app.services.audit import record_audit


def _now() -> datetime:
    return datetime.now(UTC)


def log_event(
    db: Session,
    *,
    event_type: TapeEventType,
    tape_id: int | None = None,
    slot_number: int | None = None,
    drive_number: int | None = None,
    initiated_by: str = "operator",
    job_id: int | None = None,
) -> TapeEvent:
    event = TapeEvent(
        tape_id=tape_id,
        event_type=event_type,
        slot_number=slot_number,
        drive_number=drive_number,
        initiated_by=initiated_by,
        job_id=job_id,
        started_at=_now(),
        result=EventResult.pending,
    )
    db.add(event)
    db.flush()
    return event


def finish_event(db: Session, event: TapeEvent, result: EventResult, error: str | None = None) -> None:
    event.finished_at = _now()
    event.result = result
    event.error_detail = error
    db.flush()


def sync_snapshot(db: Session, state: LibraryState) -> None:
    """Replace the cached library_slots rows from a fresh hardware state."""
    db.query(LibrarySlot).delete()
    for s in state.slots:
        db.add(LibrarySlot(
            slot_kind=s.kind if s.kind in {"storage", "import_export"} else "storage",
            slot_number=s.number, barcode=s.barcode,
            is_cleaning_tape=s.is_cleaning_tape,
        ))
    for d in state.drives:
        db.add(LibrarySlot(
            slot_kind="drive", slot_number=d.number, barcode=d.loaded_barcode,
            drive_loaded_from=d.loaded_from_slot,
        ))
    # Auto-register tapes we have never seen before as scratch.
    known = {t.barcode for t in db.scalars(select(Tape)).all()}
    for s in state.slots:
        if s.barcode and s.barcode not in known and not s.is_cleaning_tape:
            db.add(Tape(barcode=s.barcode, status=TapeStatus.scratch,
                        physical_location=f"slot {s.number}"))
            known.add(s.barcode)
    db.flush()


def refresh_inventory(db: Session, *, initiated_by: str = "operator") -> LibraryState:
    hw = get_hardware()
    event = log_event(db, event_type=TapeEventType.inventory, initiated_by=initiated_by)
    try:
        state = hw.inventory()
        sync_snapshot(db, state)
        finish_event(db, event, EventResult.success)
        record_audit(db, actor=initiated_by, action="library.inventory",
                     detail={"slots": len(state.slots), "drives": len(state.drives)})
        return state
    except HardwareError as exc:
        finish_event(db, event, EventResult.error, str(exc))
        raise


def get_state(db: Session) -> LibraryState:
    return get_hardware().library_status()


def _tape_by_barcode(db: Session, barcode: str) -> Tape | None:
    return db.scalar(select(Tape).where(Tape.barcode == barcode))


def load_tape(db: Session, slot: int, drive: int, *, initiated_by: str = "operator",
              job_id: int | None = None) -> None:
    hw = get_hardware()
    state = hw.library_status()
    slot_state = state.slot(slot)
    barcode = slot_state.barcode if slot_state else None
    tape = _tape_by_barcode(db, barcode) if barcode else None
    event = log_event(db, event_type=TapeEventType.load, tape_id=tape.id if tape else None,
                      slot_number=slot, drive_number=drive, initiated_by=initiated_by, job_id=job_id)
    try:
        hw.load(slot, drive)
        sync_snapshot(db, hw.library_status())
        if tape:
            tape.physical_location = f"drive {drive}"
        finish_event(db, event, EventResult.success)
    except HardwareError as exc:
        finish_event(db, event, EventResult.error, str(exc))
        raise


def unload_tape(db: Session, slot: int, drive: int, *, initiated_by: str = "operator",
                job_id: int | None = None) -> None:
    hw = get_hardware()
    state = hw.library_status()
    drive_state = state.drive(drive)
    barcode = drive_state.loaded_barcode if drive_state else None
    tape = _tape_by_barcode(db, barcode) if barcode else None
    event = log_event(db, event_type=TapeEventType.unload, tape_id=tape.id if tape else None,
                      slot_number=slot, drive_number=drive, initiated_by=initiated_by, job_id=job_id)
    try:
        hw.unload(slot, drive)
        sync_snapshot(db, hw.library_status())
        if tape:
            tape.physical_location = f"slot {slot}"
        finish_event(db, event, EventResult.success)
    except HardwareError as exc:
        finish_event(db, event, EventResult.error, str(exc))
        raise


def clean_drive(db: Session, drive: int, cleaning_slot: int, *, initiated_by: str = "operator") -> None:
    hw = get_hardware()
    event = log_event(db, event_type=TapeEventType.clean, slot_number=cleaning_slot,
                      drive_number=drive, initiated_by=initiated_by)
    try:
        hw.clean_drive(drive, cleaning_slot)
        sync_snapshot(db, hw.library_status())
        finish_event(db, event, EventResult.success)
        record_audit(db, actor=initiated_by, action="drive.clean",
                     detail={"drive": drive, "cleaning_slot": cleaning_slot})
    except HardwareError as exc:
        finish_event(db, event, EventResult.error, str(exc))
        raise


def format_tape(db: Session, drive: int, barcode: str, *, force: bool = False,
                initiated_by: str = "operator") -> None:
    """Wraps mkltfs. Refuses unless the tape's catalog status is 'scratch'
    (or ``force`` for a deliberate repurpose) — guards against wiping a tape
    that still has catalogued content (§3.1)."""
    tape = _tape_by_barcode(db, barcode)
    if tape and tape.status != TapeStatus.scratch and not force:
        raise HardwareError(
            f"tape {barcode} status is '{tape.status.value}', not 'scratch' — "
            f"refusing to format without force"
        )
    hw = get_hardware()
    event = log_event(db, event_type=TapeEventType.format, tape_id=tape.id if tape else None,
                      drive_number=drive, initiated_by=initiated_by)
    try:
        hw.mkltfs(drive, barcode)
        if tape:
            tape.status = TapeStatus.scratch
            tape.used_bytes = 0
            tape.write_pass_count = (tape.write_pass_count or 0) + 1
            tape.dedicated_source_machine = None
        finish_event(db, event, EventResult.success)
        record_audit(db, actor=initiated_by, action="tape.format",
                     entity_type="tape", entity_id=barcode, detail={"force": force})
    except HardwareError as exc:
        finish_event(db, event, EventResult.error, str(exc))
        raise


def retire_tape(db: Session, barcode: str, *, reason: str = "", initiated_by: str = "operator") -> None:
    tape = _tape_by_barcode(db, barcode)
    if not tape:
        raise HardwareError(f"unknown tape {barcode}")
    tape.status = TapeStatus.retired
    if reason:
        tape.notes = f"{(tape.notes + chr(10)) if tape.notes else ''}retired: {reason}"
    log_event(db, event_type=TapeEventType.retire, tape_id=tape.id, initiated_by=initiated_by)
    record_audit(db, actor=initiated_by, action="tape.retire", entity_type="tape",
                 entity_id=barcode, detail={"reason": reason})
