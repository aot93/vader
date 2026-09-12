"""Batch tape optimize/format: cycle a set of user-selected tapes through
whichever drives are free, formatting each with ``mkltfs``.

Per IBM's LTFS documentation, ``mkltfs`` *is* the media-optimization step —
on LTO-9 it always runs a full optimize pass as part of format (regardless of
whether the tape was optimized before), so there is no separate "optimize"
verb to call. This module's only job is the batch cycling: load -> format ->
unload -> next tape, across as many drives as are free, so a run of many
tapes doesn't serialise through a single drive for hours.

The physical robotic arm can only move one tape at a time, so ``_arm_lock``
serialises calls to ``lib.load_tape`` / ``lib.unload_tape`` even though the
``mkltfs`` calls themselves (each already-loaded onto its own drive) run
concurrently — that's the part worth parallelising, since it's what can take
up to ~2h per tape on LTO-9.
"""
from __future__ import annotations

import queue
import threading
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import session_scope
from app.hardware import HardwareError, get_hardware
from app.models import EventResult, Tape, TapeEventType, TapeStatus
from app.services import library as lib
from app.services.audit import record_audit

ProgressCb = Callable[[int, int, str], None]
CancelCb = Callable[[], bool]

_arm_lock = threading.Lock()


class BatchFormatError(RuntimeError):
    pass


def _slot_of(barcode: str) -> int:
    state = get_hardware().library_status()
    slot = state.find_barcode_slot(barcode)
    if slot is None:
        raise HardwareError(f"tape {barcode} is not in a storage slot")
    return slot


def _first_free_slot() -> int:
    state = get_hardware().library_status()
    free = next((s.number for s in state.slots if s.barcode is None), None)
    if free is None:
        raise HardwareError("no free storage slot to return the tape to")
    return free


def _free_drives() -> list[int]:
    state = get_hardware().library_status()
    return [d.number for d in state.drives if d.loaded_barcode is None]


def run_batch_format(
    db: Session,
    *,
    job_id: int,
    barcodes: list[str],
    force: bool = False,
    initiated_by: str = "operator",
    progress: ProgressCb | None = None,
    is_cancelled: CancelCb | None = None,
) -> dict:
    progress = progress or (lambda *_a: None)
    is_cancelled = is_cancelled or (lambda: False)

    barcodes = list(dict.fromkeys(b.strip() for b in barcodes if b.strip()))
    if not barcodes:
        raise BatchFormatError("no tapes selected")

    # Pre-flight: refuse the whole batch up front if any selected tape isn't
    # 'scratch' (unless forced) — same guard as the single-tape Format action
    # (library.format_tape) — so a long batch doesn't die on tape #14 of #20.
    if not force:
        not_scratch = [
            f"{barcode} ({tape.status.value})"
            for barcode in barcodes
            if (tape := db.scalar(select(Tape).where(Tape.barcode == barcode)))
            and tape.status != TapeStatus.scratch
        ]
        if not_scratch:
            raise BatchFormatError(
                "refusing to format non-scratch tapes without force: " + ", ".join(not_scratch)
            )

    drives = _free_drives()[: len(barcodes)]
    if not drives:
        raise BatchFormatError("no free drives available")

    work: queue.Queue[str] = queue.Queue()
    for barcode in barcodes:
        work.put(barcode)

    total = len(barcodes)
    lock = threading.Lock()
    done = 0
    succeeded: list[str] = []
    failed: dict[str, str] = {}
    skipped: list[str] = []

    def worker(drive_number: int) -> None:
        nonlocal done
        while True:
            try:
                barcode = work.get_nowait()
            except queue.Empty:
                return
            if is_cancelled():
                with lock:
                    skipped.append(barcode)
                work.task_done()
                continue

            error: str | None = None
            try:
                progress(done, total, f"drive {drive_number}: loading {barcode}")
                with _arm_lock, session_scope() as s:
                    lib.load_tape(s, _slot_of(barcode), drive_number,
                                  initiated_by=initiated_by, job_id=job_id)
                try:
                    progress(done, total, f"drive {drive_number}: formatting/optimizing {barcode}")
                    with session_scope() as s:
                        tape = s.scalar(select(Tape).where(Tape.barcode == barcode))
                        event = lib.log_event(
                            s, event_type=TapeEventType.format, tape_id=tape.id if tape else None,
                            drive_number=drive_number, initiated_by=initiated_by, job_id=job_id,
                        )
                        try:
                            get_hardware().mkltfs(drive_number, barcode)
                        except HardwareError as exc:
                            lib.finish_event(s, event, EventResult.error, str(exc))
                            raise
                        lib.finish_event(s, event, EventResult.success)
                        if tape:
                            tape.status = TapeStatus.scratch
                            tape.used_bytes = 0
                            tape.write_pass_count = (tape.write_pass_count or 0) + 1
                            tape.dedicated_source_machine = None
                        record_audit(s, actor=initiated_by, action="tape.format",
                                     entity_type="tape", entity_id=barcode,
                                     detail={"force": force, "batch": True})
                except HardwareError as exc:
                    error = str(exc)
                finally:
                    # Always try to get the tape out of the drive, whether or
                    # not the format succeeded, so a bad tape doesn't sit
                    # loaded for the rest of the batch.
                    progress(done, total, f"drive {drive_number}: unloading {barcode}")
                    try:
                        with _arm_lock, session_scope() as s:
                            lib.unload_tape(s, _first_free_slot(), drive_number,
                                            initiated_by=initiated_by, job_id=job_id)
                    except HardwareError as exc:
                        note = f"failed to unload: {exc}"
                        error = f"{error}; also {note}" if error else note
            except HardwareError as exc:
                error = error or str(exc)

            with lock:
                done += 1
                if error:
                    failed[barcode] = error
                    progress(done, total, f"{barcode} failed: {error}")
                else:
                    succeeded.append(barcode)
                    progress(done, total, f"{barcode} done ({done}/{total})")
            work.task_done()

    threads = [
        threading.Thread(target=worker, args=(d,), name=f"batch-format-drive{d}")
        for d in drives
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if failed and not succeeded:
        raise BatchFormatError("all tapes failed: " + "; ".join(f"{b}: {e}" for b, e in failed.items()))

    return {"succeeded": succeeded, "failed": failed, "skipped": skipped, "total": total}
