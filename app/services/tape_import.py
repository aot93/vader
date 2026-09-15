"""Import pre-existing LTFS tapes — written outside Vader (e.g. by a Windows
LTFS driver, before this app existed) — into the catalog.

Mounts each tape **read-only**, walks it with the same intake classifier a
write job uses, hashes every file, and builds the same SequenceContainer /
ContentItem / ContentTapeSpan rows a write job would — so imported content is
searchable and restorable identically to anything Vader wrote itself. Nothing
in this module ever writes to a tape; ``mount_ltfs(..., read_only=True)`` is
a hard guarantee enforced at the hardware layer, not just a convention here.

Content identity: normal write jobs key a unit's catalog identity off its
source-share path, which is stable regardless of which tape/drive it lands
on. Import has no such external path — the only "source" is the tape's own
mount point, and mount points are reused across drives/runs (``driveN``), so
two different tapes mounted on the same drive at different times would
otherwise produce identical paths for identically-named shot folders. To
keep identity scoped to the actual physical tape (and stable across re-runs
of the *same* tape, for idempotency), every unit's ``source_path`` is
rewritten to ``tape://<barcode>/<ltfs-relative-path>`` before cataloging.

Imported tapes never get mkltfs'd or offered to a future write job: a newly
seen barcode is registered straight to 'archived' status, never 'scratch' —
see ``_register_archived``. Each tape's content is recorded as self-contained
(part_index=0, part_count=1); if a shot was originally split across several
of these legacy tapes, re-importing each one catalogs them as separate spans
under the same unit_key rather than reconstructing the original split.

Like batch_format, the arm (load/unload) is serialised across drives —
lib.load_tape/lib.unload_tape share one lock internally (app.services.
library) — while each drive's mount/scan/catalog work runs concurrently —
that part is what's worth parallelising, since reading a whole LTO tape is
slow.

Two modes (``verify`` flag on :func:`run_tape_import`): the default full
import reads and SHA256-hashes every file, the same trust anchor as any
other content this app has never seen before. A "fast scan" (``verify=
False``) walks the tape and records what's there — size, path, count — without
reading file content at all, so it costs roughly what listing costs (seconds
to minutes) instead of what reading the whole tape costs (hours). Fast-scanned
entries get ``verified_at=None``; the existing idempotent-skip check already
only skips *verified* spans, so a later full import naturally re-processes
anything a fast scan only listed. ``Tape.last_scanned_at`` vs
``last_verified_at`` is how the catalog tells "listed" from "trusted" apart.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_scope
from app.hardware import HardwareError, get_hardware
from app.models import (
    BackupCategory,
    ContentTapeSpan,
    EventResult,
    Tape,
    TapeEventType,
    TapeStatus,
)
from app.services import library as lib
from app.services.audit import record_audit
from app.services.checksums import sha256_file
from app.services.intake import FileUnit, IntakeRules, SequenceUnit, scan_source
from app.services.manifests import (
    FrameChecksum,
    build_manifest_document,
    manifest_relpath,
    write_manifest,
)
from app.services.writer import _existing_span, _ltfs_relpath, _upsert_item, _upsert_sequence
from app.web import format_bytes

ProgressCb = Callable[[int, int, str], None]
CancelCb = Callable[[], bool]

# How often (seconds) to emit a progress update while scanning+hashing a
# single tape's units. Time-based rather than count-based so it stays
# responsive regardless of whether the units are many small files or a few
# huge sequences — the thing being answered is "is this stuck?", not a
# precise ETA.
_PROGRESS_INTERVAL_SECONDS = 5.0


class TapeImportError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _slot_of(barcode: str) -> int:
    state = get_hardware().library_status()
    slot = state.find_barcode_slot(barcode)
    if slot is None:
        raise HardwareError(f"tape {barcode} is not in a storage slot")
    return slot


def _free_drives() -> list[int]:
    state = get_hardware().library_status()
    return [d.number for d in state.drives if d.loaded_barcode is None]


def _register_archived(db: Session, barcode: str) -> int:
    """Bring a barcode into the catalog as 'archived' — never 'scratch' — so
    nothing can pick it up for a future write/format job, regardless of
    whether an inventory scan already auto-registered it as scratch. Returns
    the tape's id."""
    tape = db.scalar(select(Tape).where(Tape.barcode == barcode))
    if tape is None:
        tape = Tape(barcode=barcode, status=TapeStatus.archived)
        db.add(tape)
        db.flush()
    elif tape.status == TapeStatus.scratch:
        tape.status = TapeStatus.archived
    return tape.id


def _measure_capacity_bytes(mount: Path) -> int | None:
    """Best-effort measured capacity for this specific cartridge, read
    straight off the mounted LTFS filesystem. Real cartridges vary
    tape-to-tape (format overhead, partial writes prior to this app), so this
    is more accurate than the configured default — but only on real
    hardware: the simulator's "mount" is a plain directory on the host disk,
    so statvfs() on it would report the *host's* free space, not a tape's."""
    if get_settings().hardware_backend != "real":
        return None
    try:
        vfs = os.statvfs(mount)
    except OSError:
        return None
    return vfs.f_blocks * vfs.f_frsize


def _rewrite_identity(mount: Path, barcode: str, units: list[SequenceUnit | FileUnit]) -> dict[int, str]:
    """Scope each unit's catalog identity to this physical tape. Returns
    ``id(unit) -> ltfs-relative path`` (the real on-tape location) since the
    unit's own ``source_path`` gets overwritten to the tape:// identity."""
    ltfs_paths: dict[int, str] = {}
    for unit in units:
        rel = _ltfs_relpath(mount, Path(unit.source_path))
        ltfs_paths[id(unit)] = rel
        unit.source_path = f"tape://{barcode}/{rel}"
    return ltfs_paths


def _import_sequence(db: Session, unit: SequenceUnit, *, ltfs_path: str,
                     barcode: str, tape_id: int, verify: bool) -> ContentTapeSpan | None:
    container = _upsert_sequence(db, unit)
    existing = _existing_span(db, seq_id=container.id, item_id=None, tape_id=tape_id, part_index=0)
    if existing and existing.verified_at:
        return None  # idempotent skip — already imported (and verified) from this tape

    man_sha: str | None = None
    verified_at: datetime | None = None
    if verify:
        checksums = [
            FrameChecksum(filename=fr.filename, frame=fr.frame, size=fr.size, sha256=sha256_file(fr.path))
            for fr in unit.sorted_frames
        ]
        man_rel = manifest_relpath(unit.project_name, unit.sequence_name, unit.shot_name or "shot")
        man_doc = build_manifest_document(
            project_name=unit.project_name, sequence_name=unit.sequence_name,
            shot_name=unit.shot_name, source_path=unit.source_path,
            ltfs_dir=ltfs_path, frames=checksums,
        )
        man_path = get_settings().data_dir / "manifests" / "imported" / barcode / man_rel
        man_sha = write_manifest(man_path, man_doc)

        container.manifest_ref = str(man_path)
        container.manifest_sha256 = man_sha
        verified_at = _now()
        container.verified_at = verified_at  # we just read + hashed every frame
    # else: fast scan — leave container.manifest_ref/verified_at as they were
    # (unset, or whatever an earlier full import already recorded).

    span = existing or ContentTapeSpan(sequence_container_id=container.id)
    span.tape_id = tape_id
    span.ltfs_path = ltfs_path
    span.part_index = 0
    span.part_count = 1
    span.size_bytes = unit.total_size_bytes
    span.sha256 = man_sha
    span.verified_at = verified_at
    if span.id is None:
        db.add(span)
    db.flush()
    return span


def _import_file(db: Session, unit: FileUnit, *, ltfs_path: str, tape_id: int, verify: bool) -> ContentTapeSpan | None:
    item = _upsert_item(db, unit)
    existing = _existing_span(db, seq_id=None, item_id=item.id, tape_id=tape_id, part_index=0)
    if existing and existing.verified_at:
        return None

    digest: str | None = None
    verified_at: datetime | None = None
    if verify:
        src = unit.path or Path(unit.source_path)
        digest = sha256_file(src)
        item.sha256 = digest
        verified_at = _now()
        item.verified_at = verified_at

    span = existing or ContentTapeSpan(content_item_id=item.id)
    span.tape_id = tape_id
    span.ltfs_path = ltfs_path
    span.part_index = 0
    span.part_count = 1
    span.size_bytes = unit.size
    span.sha256 = digest
    span.verified_at = verified_at
    if span.id is None:
        db.add(span)
    db.flush()
    return span


def _import_one_tape(
    hw, barcode: str, drive_number: int, *, tape_id: int, job_id: int, initiated_by: str,
    project_name: str | None, source_machine: str | None, cat: BackupCategory,
    progress: ProgressCb, done: int, total: int, verify: bool = True,
) -> int:
    """Mount ``barcode`` (already loaded in ``drive_number``) read-only, scan
    and catalog it, then unmount. Returns the number of units imported.

    ``progress``/``done``/``total`` are the job-level (tape-count) progress
    callback and its current tape-level counters — ``done``/``total`` don't
    change while this tape is being processed, only the message does, so a
    long single-tape scan+hash no longer looks frozen in the UI.

    ``verify=False`` is the fast-scan mode: list what's on the tape without
    reading/hashing any file content.
    """
    mount = hw.mount_ltfs(drive_number, read_only=True)
    try:
        try:
            units = scan_source(mount, rules=IntakeRules(), project_name=project_name,
                                source_machine=source_machine, backup_category=cat)
        except FileNotFoundError:
            units = []
        ltfs_paths = _rewrite_identity(mount, barcode, units)
        measured_capacity_bytes = _measure_capacity_bytes(mount)

        n_units = 0
        total_bytes = 0
        n_total_units = len(units)
        last_progress = time.monotonic()
        verb = "scanned" if verify else "scanned (fast, no hash)"
        with session_scope() as s:
            event = lib.log_event(
                s, event_type=TapeEventType.import_tape, tape_id=tape_id,
                drive_number=drive_number, initiated_by=initiated_by, job_id=job_id,
            )
            try:
                for i, unit in enumerate(units, start=1):
                    path = ltfs_paths[id(unit)]
                    if isinstance(unit, SequenceUnit):
                        span = _import_sequence(s, unit, ltfs_path=path, barcode=barcode,
                                                tape_id=tape_id, verify=verify)
                    else:
                        span = _import_file(s, unit, ltfs_path=path, tape_id=tape_id, verify=verify)
                    if span:
                        n_units += 1
                        total_bytes += span.size_bytes or 0

                    now = time.monotonic()
                    if now - last_progress >= _PROGRESS_INTERVAL_SECONDS or i == n_total_units:
                        # Commit first: `progress()` opens its own DB session
                        # to update the Job row, which needs SQLite's single
                        # write lock — still held by this still-open catalog
                        # transaction otherwise, so the callback would just
                        # block until busy_timeout and fail with "database is
                        # locked". expire_on_commit=False (app.db.SessionLocal)
                        # means `s` and everything loaded through it (e.g.
                        # `event` below) stay usable after this commit.
                        s.commit()
                        progress(done, total,
                                 f"drive {drive_number}: {barcode} — {verb} {i}/{n_total_units} "
                                 f"units ({format_bytes(total_bytes)})")
                        last_progress = now

                tape = s.get(Tape, tape_id)
                tape.used_bytes = sum(
                    sp.size_bytes or 0 for sp in
                    s.scalars(select(ContentTapeSpan).where(ContentTapeSpan.tape_id == tape_id)).all()
                )
                if measured_capacity_bytes:
                    tape.capacity_native_bytes = measured_capacity_bytes
                tape.last_scanned_at = _now()
                if verify:
                    tape.last_verified_at = _now()
                tape.notes = (
                    f"{(tape.notes + chr(10)) if tape.notes else ''}"
                    f"{_now().date()}: {'imported' if verify else 'fast-scanned'} "
                    f"{n_units} pre-existing unit(s) ({total_bytes} B)"
                    f"{'' if verify else ' — not hash-verified, audit before trusting'}"
                    f" — original write date unknown"
                )
                lib.finish_event(s, event, EventResult.success)
                record_audit(s, actor=initiated_by, action="tape.import", entity_type="tape",
                            entity_id=barcode, detail={"units": n_units, "bytes": total_bytes, "verify": verify})
            except Exception as exc:  # noqa: BLE001 - record, then still unmount/unload
                lib.finish_event(s, event, EventResult.error, str(exc))
                raise
        return n_units
    finally:
        hw.unmount_ltfs(drive_number)


def run_tape_import(
    db: Session,
    *,
    job_id: int,
    barcodes: list[str],
    project_name: str | None = None,
    source_machine: str | None = None,
    backup_category: str = "project_archive",
    initiated_by: str = "operator",
    verify: bool = True,
    progress: ProgressCb | None = None,
    is_cancelled: CancelCb | None = None,
) -> dict:
    progress = progress or (lambda *_a: None)
    is_cancelled = is_cancelled or (lambda: False)

    barcodes = list(dict.fromkeys(b.strip() for b in barcodes if b.strip()))
    if not barcodes:
        raise TapeImportError("no tape barcodes given")
    cat = BackupCategory(backup_category)

    # Pre-flight, same spirit as batch_format's scratch-status guard: refuse
    # up front rather than partway through a long batch.
    known = {t.barcode: t.status for t in db.scalars(
        select(Tape).where(Tape.barcode.in_(barcodes))).all()}
    blocked = [f"{b} ({known[b].value})" for b in barcodes
              if known.get(b) in {TapeStatus.damaged, TapeStatus.retired}]
    if blocked:
        raise TapeImportError("refusing to import onto damaged/retired tapes: " + ", ".join(blocked))

    drives = _free_drives()[: len(barcodes)]
    if not drives:
        raise TapeImportError("no free drives available")

    work: queue.Queue[str] = queue.Queue()
    for barcode in barcodes:
        work.put(barcode)

    total = len(barcodes)
    lock = threading.Lock()
    done = 0
    succeeded: dict[str, int] = {}  # barcode -> units imported
    failed: dict[str, str] = {}
    skipped: list[str] = []

    def worker(drive_number: int) -> None:
        nonlocal done
        hw = get_hardware()
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
            n_units = 0
            try:
                with session_scope() as s:
                    tape_id = _register_archived(s, barcode)
                progress(done, total, f"drive {drive_number}: loading {barcode}")
                with session_scope() as s:
                    lib.load_tape(s, _slot_of(barcode), drive_number,
                                  initiated_by=initiated_by, job_id=job_id)
                try:
                    progress(done, total, f"drive {drive_number}: mounting {barcode} read-only")
                    n_units = _import_one_tape(
                        hw, barcode, drive_number, tape_id=tape_id, job_id=job_id,
                        initiated_by=initiated_by, project_name=project_name,
                        source_machine=source_machine, cat=cat,
                        progress=progress, done=done, total=total, verify=verify,
                    )
                except HardwareError as exc:
                    error = str(exc)
                except Exception as exc:  # noqa: BLE001 - surface any scan/catalog failure
                    error = str(exc)
                finally:
                    progress(done, total, f"drive {drive_number}: unloading {barcode}")
                    try:
                        with session_scope() as s:
                            lib.unload_tape(s, None, drive_number,
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
                    succeeded[barcode] = n_units
                    progress(done, total, f"{barcode}: {n_units} units imported ({done}/{total})")
            work.task_done()

    threads = [
        threading.Thread(target=worker, args=(d,), name=f"tape-import-drive{d}")
        for d in drives
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if failed and not succeeded:
        raise TapeImportError("all tapes failed: " + "; ".join(f"{b}: {e}" for b, e in failed.items()))

    return {"succeeded": succeeded, "failed": failed, "skipped": skipped, "total": total}
