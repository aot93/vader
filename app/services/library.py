"""Library / drive control — every hardware action goes through here so it is
logged as a ``tape_events`` row (who, when, what, result) per framework doc §3.1
and §3.7, and the cached ``library_slots`` snapshot stays current.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
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

# The physical robotic arm can only move one tape at a time, so every action
# that moves it (load/unload/clean) is serialized on this one shared lock —
# shared across every job type via load_tape/unload_tape/clean_drive below,
# rather than each caller managing its own (batch_format.py and
# tape_import.py used to each keep a private _arm_lock; two separate Lock
# objects never actually serialize each other, a latent bug that only
# mattered once jobs could run concurrently). format_tape() does not need
# this lock — mkltfs doesn't move the changer arm.
_arm_lock = threading.Lock()

# A live write job hit one MOVE MEDIUM SCSI error (Illegal Request, sense
# 53/03) on the post-write unload — retrying the identical `mtx unload` by
# hand seconds later succeeded immediately, confirming it was transient, not
# a persistent fault or a code bug. `_move_with_retry` wraps just the
# physical `hw.load()`/`hw.unload()` call so both directions get one bounded
# retry before the caller's existing `HardwareError` handling (event
# logging, job-result visibility) takes over — a general retry policy is
# deliberately not the goal here, just absorbing one flaky arm move.
_ARM_MOVE_ATTEMPTS = 2
_ARM_MOVE_RETRY_DELAY_SECONDS = 2.0


def _move_with_retry(move: Callable[[], None]) -> None:
    last_exc: HardwareError | None = None
    for attempt in range(1, _ARM_MOVE_ATTEMPTS + 1):
        try:
            move()
            return
        except HardwareError as exc:
            last_exc = exc
            if attempt < _ARM_MOVE_ATTEMPTS:
                time.sleep(_ARM_MOVE_RETRY_DELAY_SECONDS)
    raise last_exc


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
    with _arm_lock:
        hw = get_hardware()
        state = hw.library_status()
        slot_state = state.slot(slot)
        barcode = slot_state.barcode if slot_state else None
        tape = _tape_by_barcode(db, barcode) if barcode else None
        event = log_event(db, event_type=TapeEventType.load, tape_id=tape.id if tape else None,
                          slot_number=slot, drive_number=drive, initiated_by=initiated_by, job_id=job_id)
        try:
            _move_with_retry(lambda: hw.load(slot, drive))
            sync_snapshot(db, hw.library_status())
            if tape:
                tape.physical_location = f"drive {drive}"
            finish_event(db, event, EventResult.success)
        except HardwareError as exc:
            finish_event(db, event, EventResult.error, str(exc))
            raise


def unload_tape(db: Session, slot: int | None, drive: int, *, initiated_by: str = "operator",
                job_id: int | None = None) -> None:
    """``slot=None`` means "any free storage slot" — resolved here, under the
    arm lock, not by the caller beforehand. Every caller wants exactly that
    (nobody ever needs a *specific* return slot); resolving it outside the
    lock used to be a real race between two concurrently-unloading drives —
    both could see the same slot as free and both target it, one winning and
    the other failing with "slot occupied" (confirmed live: caught by
    tests/test_pipeline.py's batch_format test intermittently failing once
    two drives could genuinely unload at the same time)."""
    with _arm_lock:
        hw = get_hardware()
        state = hw.library_status()
        if slot is None:
            # Not `s.barcode is None`: a slot holding a tape whose barcode
            # label is blank/unreadable also reports no VolumeTag, which
            # looks identical to a genuinely empty slot if occupancy is
            # inferred from barcode alone — confirmed live via `mtx status`
            # reporting a target slot "Already Full" after this picked it as
            # "free". `occupied` comes straight from mtx's Empty/Full token,
            # independent of whether the barcode was readable.
            slot = next((s.number for s in state.slots if not s.occupied), None)
            if slot is None:
                raise HardwareError("no free storage slot to return the tape to")
        drive_state = state.drive(drive)
        barcode = drive_state.loaded_barcode if drive_state else None
        tape = _tape_by_barcode(db, barcode) if barcode else None
        event = log_event(db, event_type=TapeEventType.unload, tape_id=tape.id if tape else None,
                          slot_number=slot, drive_number=drive, initiated_by=initiated_by, job_id=job_id)
        try:
            _move_with_retry(lambda: hw.unload(slot, drive))
            sync_snapshot(db, hw.library_status())
            if tape:
                tape.physical_location = f"slot {slot}"
            finish_event(db, event, EventResult.success)
        except HardwareError as exc:
            finish_event(db, event, EventResult.error, str(exc))
            raise


def clean_drive(db: Session, drive: int, cleaning_slot: int, *, initiated_by: str = "operator") -> None:
    with _arm_lock:
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
                initiated_by: str = "operator", job_id: int | None = None) -> None:
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
                      drive_number=drive, initiated_by=initiated_by, job_id=job_id)
    try:
        # ``force`` here has already done its job as the catalog-status guard
        # above — separately, mkltfs itself always refuses on real hardware
        # when the medium already carries an LTFS filesystem, which is the
        # normal state of any real cartridge being (re)formatted, scratch or
        # not. That physical refusal must always be forced past here, or an
        # ordinary "format this scratch tape" action fails on real hardware.
        hw.mkltfs(drive, barcode, force=True)
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
        raise ValueError(f"unknown tape {barcode}")
    tape.status = TapeStatus.retired
    if reason:
        tape.notes = f"{(tape.notes + chr(10)) if tape.notes else ''}retired: {reason}"
    log_event(db, event_type=TapeEventType.retire, tape_id=tape.id, initiated_by=initiated_by)
    record_audit(db, actor=initiated_by, action="tape.retire", entity_type="tape",
                 entity_id=barcode, detail={"reason": reason})


def resolve_drives(claimed_drives: frozenset[int] | None, n: int) -> list[int]:
    """Which drives ``batch_format``/``tape_import`` should actually use for
    up to ``n`` tapes, fanning out across as many drives as they can get.
    Lives here (not in ``app.jobs.drives``, the claim registry itself)
    because both call sites already import this module, and importing
    ``app.jobs.drives`` from a service pulls in the whole ``app.jobs``
    package — which imports ``app.jobs.handlers``, which imports these same
    services back — a real circular import, not just a style concern.

    When run through the job worker, ``claimed_drives`` is exactly what the
    registry already reserved for the job and must be used as-is — physical
    state alone can't distinguish "free" from "reserved by a different
    concurrently-running job that hasn't loaded a tape onto it yet" (a
    concurrently-running write job claiming drive 0 leaves drive 0 looking
    physically free right up until it actually calls ``load_tape()``).
    Called directly with no worker (e.g. tests, or any future non-worker
    caller), ``claimed_drives`` is ``None`` and this discovers currently-free
    drives itself instead. Was duplicated verbatim (this exact branch, plus
    each module's own private ``_free_drives()``) in
    ``app.services.batch_format`` and ``app.services.tape_import``.
    """
    if claimed_drives is not None:
        return sorted(claimed_drives)[:n]
    state = get_hardware().library_status()
    return sorted(d.number for d in state.drives if d.loaded_barcode is None)[:n]
