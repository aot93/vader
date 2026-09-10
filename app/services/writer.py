"""The write pipeline: scan -> allocate -> copy -> checksum -> read-back verify
-> catalog. Implements framework doc §3.3, §3.4 (write-time verification) and
§3.5a (automatic spanning + greedy mode).

Idempotency (§6): every unit is keyed by its source path. Re-running an
interrupted job skips any placement whose span row is already written (and
already read-back verified, when verification is on), so a network blip or VM
restart mid-run is recoverable by simply starting the job again.
"""
from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.hardware import HardwareError, get_hardware
from app.models import (
    BackupCategory,
    ContentItem,
    ContentTapeSpan,
    SequenceContainer,
    Tape,
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
    merge_part_manifests,
    write_manifest,
)
from app.services.spanning import (
    AllocationError,
    Placement,
    TapeAllocator,
    TapeView,
    unit_key,
)

ProgressCb = Callable[[int, int, str], None]
CancelCb = Callable[[], bool]


def _now() -> datetime:
    return datetime.now(UTC)


class WriteError(RuntimeError):
    pass


class _Drive:
    """Tracks what is physically in one drive during a job."""

    def __init__(self, db: Session, drive_number: int, job_id: int, actor: str) -> None:
        self.db = db
        self.n = drive_number
        self.job_id = job_id
        self.actor = actor
        self.hw = get_hardware()
        self.loaded_barcode: str | None = None
        self.mount: Path | None = None
        self._formatted: set[str] = set()

    def _slot_of(self, barcode: str) -> int:
        state = self.hw.library_status()
        slot = state.find_barcode_slot(barcode)
        if slot is None:
            raise HardwareError(f"tape {barcode} is not in a storage slot")
        return slot

    def ensure(self, barcode: str, *, needs_format: bool) -> Path:
        if self.loaded_barcode == barcode and self.mount is not None:
            return self.mount
        self.release()
        slot = self._slot_of(barcode)
        lib.load_tape(self.db, slot, self.n, initiated_by=self.actor, job_id=self.job_id)
        self.db.commit()
        self.loaded_barcode = barcode
        if needs_format and barcode not in self._formatted:
            self.hw.mkltfs(self.n, barcode)
            self._formatted.add(barcode)
        self.mount = self.hw.mount_ltfs(self.n)
        return self.mount

    def release(self) -> None:
        if self.loaded_barcode is None:
            return
        try:
            self.hw.unmount_ltfs(self.n)
        except HardwareError:
            pass
        # return the tape to the first free storage slot
        state = self.hw.library_status()
        free = next((s.number for s in state.slots if s.barcode is None), None)
        if free is not None:
            lib.unload_tape(self.db, free, self.n, initiated_by=self.actor, job_id=self.job_id)
            self.db.commit()
        self.loaded_barcode = None
        self.mount = None


def _tape_views(db: Session, *, greedy: bool, greedy_source: str | None) -> tuple[list[TapeView], list[TapeView]]:
    settings = get_settings()
    default_cap = settings.sim_tape_capacity_bytes
    writable: list[TapeView] = []
    scratch: list[TapeView] = []
    for tape in db.scalars(select(Tape)).all():
        if tape.status in {TapeStatus.retired, TapeStatus.damaged, TapeStatus.archived}:
            continue
        cap = tape.capacity_native_bytes or default_cap
        if tape.capacity_native_bytes is None:
            tape.capacity_native_bytes = cap
        view = TapeView(
            tape_id=tape.id, barcode=tape.barcode, capacity_bytes=cap,
            used_bytes=tape.used_bytes or 0, status=tape.status.value,
            dedicated_source_machine=tape.dedicated_source_machine,
        )
        if tape.status == TapeStatus.scratch:
            scratch.append(view)
        elif tape.status == TapeStatus.active:
            writable.append(view)
    db.flush()
    return writable, scratch


def _ltfs_relpath(source_root: Path, path: Path) -> str:
    try:
        rel = path.resolve().relative_to(source_root.resolve())
    except ValueError:
        rel = Path(path.name)
    return "/".join(rel.parts)


def _copy_slice(src: Path, dst: Path, start: int, end: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    remaining = end - start
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        fi.seek(start)
        while remaining > 0:
            block = fi.read(min(4 * 1024 * 1024, remaining))
            if not block:
                break
            fo.write(block)
            remaining -= len(block)


def _existing_span(
    db: Session, *, seq_id: int | None, item_id: int | None, tape_id: int, part_index: int
) -> ContentTapeSpan | None:
    stmt = select(ContentTapeSpan).where(
        ContentTapeSpan.tape_id == tape_id,
        ContentTapeSpan.part_index == part_index,
    )
    if seq_id is not None:
        stmt = stmt.where(ContentTapeSpan.sequence_container_id == seq_id)
    else:
        stmt = stmt.where(ContentTapeSpan.content_item_id == item_id)
    return db.scalar(stmt)


def _upsert_sequence(db: Session, unit: SequenceUnit) -> SequenceContainer:
    row = db.scalar(select(SequenceContainer).where(SequenceContainer.source_path == unit.source_path))
    has_gaps, gap_detail = unit.gap_report()
    if row is None:
        row = SequenceContainer(source_path=unit.source_path)
        db.add(row)
    row.project_name = unit.project_name
    row.sequence_name = unit.sequence_name
    row.shot_name = unit.shot_name
    row.source_machine = unit.source_machine
    row.backup_category = unit.backup_category
    row.frame_start = unit.frame_start
    row.frame_end = unit.frame_end
    row.frame_count = unit.frame_count
    row.has_gaps = has_gaps
    row.gap_detail = gap_detail
    row.total_size_bytes = unit.total_size_bytes
    db.flush()
    return row


def _upsert_item(db: Session, unit: FileUnit) -> ContentItem:
    row = db.scalar(select(ContentItem).where(ContentItem.source_path == unit.source_path))
    if row is None:
        row = ContentItem(source_path=unit.source_path)
        db.add(row)
    row.content_type = unit.content_type
    row.project_name = unit.project_name
    row.video_name = unit.video_name
    row.chunk_index = unit.chunk_index
    row.total_chunks = unit.total_chunks
    row.filename = unit.filename
    row.source_machine = unit.source_machine
    row.source_system_tag = unit.source_system_tag
    row.backup_category = unit.backup_category
    row.file_size_bytes = unit.size
    db.flush()
    return row


def run_write(
    db: Session,
    *,
    job_id: int,
    source_path: str,
    drive: int = 0,
    mode: str = "standard",
    greedy_source: str | None = None,
    project_name: str | None = None,
    source_machine: str | None = None,
    backup_category: str = "project_archive",
    readback_verify: bool | None = None,
    actor: str = "operator",
    progress: ProgressCb | None = None,
    is_cancelled: CancelCb | None = None,
) -> dict:
    settings = get_settings()
    readback = settings.write_readback_verify if readback_verify is None else readback_verify
    greedy = mode == "greedy"
    if greedy and not greedy_source:
        raise WriteError("greedy mode requires 'greedy_source' (the machine/source name)")
    cat = BackupCategory(backup_category)
    if greedy:
        source_machine = source_machine or greedy_source
    progress = progress or (lambda *_: None)
    is_cancelled = is_cancelled or (lambda: False)

    root = Path(source_path)
    units = scan_source(
        root, rules=IntakeRules(), project_name=project_name,
        source_machine=source_machine, backup_category=cat,
    )
    if not units:
        raise WriteError(f"no archivable content found under {root}")

    writable, scratch = _tape_views(db, greedy=greedy, greedy_source=greedy_source)
    allocator = TapeAllocator(writable, scratch, greedy=greedy, greedy_source=greedy_source)
    try:
        alloc = allocator.allocate(units)
    except AllocationError as exc:
        raise WriteError(str(exc)) from exc

    unit_by_key = {unit_key(u): u for u in units}
    total_bytes = sum(p.size_bytes for p in alloc.placements)
    done_bytes = 0
    mismatches: list[str] = []
    mismatch_tapes: dict[int, int] = {}  # tape_id -> mismatch count
    tapes_written: set[int] = set()

    drv = _Drive(db, drive, job_id, actor)
    try:
        for placement in alloc.placements:
            if is_cancelled():
                raise WriteError("cancelled by operator")
            unit = unit_by_key[placement.unit_key]
            mount = drv.ensure(placement.barcode, needs_format=placement.needs_format)
            tape = db.scalar(select(Tape).where(Tape.id == placement.tape_id))

            before = len(mismatches)
            if isinstance(unit, SequenceUnit):
                span = _write_sequence_part(
                    db, unit, placement, mount, root, readback, mismatches, drv.n
                )
            else:
                span = _write_file_part(
                    db, unit, placement, mount, root, readback, mismatches, drv.n
                )
            if len(mismatches) > before:
                mismatch_tapes[placement.tape_id] = (
                    mismatch_tapes.get(placement.tape_id, 0) + len(mismatches) - before
                )
            span.tape_id = placement.tape_id
            done_bytes += placement.size_bytes
            tapes_written.add(placement.tape_id)
            if tape:
                tape.dedicated_source_machine = (
                    greedy_source if greedy else tape.dedicated_source_machine
                )
            db.commit()
            # progress AFTER commit so the write lock is released first
            progress(done_bytes, total_bytes,
                     f"{placement.barcode}: {placement.unit_key} part {placement.part_index + 1}/{placement.part_count}")

        # finalise tapes
        for tape_id in tapes_written:
            _finalise_tape(db, tape_id)
        # record any write-time read-back mismatch on the affected tape (§4.5) so
        # a follow-up verify is prompted even if nobody reads the job result.
        for tape_id, n in mismatch_tapes.items():
            t = db.scalar(select(Tape).where(Tape.id == tape_id))
            if t:
                t.notes = (
                    f"{(t.notes + chr(10)) if t.notes else ''}"
                    f"{_now().date()}: write-time read-back found {n} checksum "
                    f"mismatch(es) — re-verify this tape"
                )
        db.commit()

        # per-tape CSV written onto the tape itself (§8) — best effort
        from app.services.export_csv import tape_catalog_csv

        for tape_id in tapes_written:
            tape = db.scalar(select(Tape).where(Tape.id == tape_id))
            if not tape:
                continue
            try:
                mount = drv.ensure(tape.barcode, needs_format=False)
                dst = mount / "_catalog" / f"{tape.barcode}.csv"
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(tape_catalog_csv(db, tape.barcode))
            except HardwareError:
                pass
    finally:
        drv.release()
        db.commit()

    result = {
        "units": len(units),
        "placements": len(alloc.placements),
        "bytes": total_bytes,
        "tapes": sorted({p.barcode for p in alloc.placements}),
        "spanned_units": sorted(set(alloc.spanned_units)),
        "readback_mismatches": mismatches,
        "mode": mode,
    }
    record_audit(db, actor=actor, action="write.completed", detail=result)
    db.commit()
    return result


def _write_sequence_part(
    db: Session,
    unit: SequenceUnit,
    placement: Placement,
    mount: Path,
    root: Path,
    readback: bool,
    mismatches: list[str],
    drive_n: int,
) -> ContentTapeSpan:
    container = _upsert_sequence(db, unit)
    existing = _existing_span(
        db, seq_id=container.id, item_id=None,
        tape_id=placement.tape_id, part_index=placement.part_index,
    )
    if existing and existing.written_at and (existing.verified_at or not readback):
        return existing  # idempotent skip

    frames = placement.frames or []
    seq_dir_rel = _ltfs_relpath(root, Path(unit.source_path))
    checksums: list[FrameChecksum] = []
    for fr in frames:
        rel = _ltfs_relpath(root, fr.path)
        dst = mount / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(fr.path, dst)
        digest = sha256_file(dst)
        if readback:
            if sha256_file(fr.path) != digest:
                msg = f"{unit_key(unit)}::{fr.filename}"
                mismatches.append(msg)
        checksums.append(FrameChecksum(filename=fr.filename, frame=fr.frame, size=fr.size, sha256=digest))

    # sidecar manifest — parallel _manifests/ path, never inside the shot folder.
    # Each tape carries a manifest for the frames it holds (self-describing);
    # the app also keeps every part under one shared dir so the whole-folder
    # manifest can be merged once the last part lands.
    suffix = "" if placement.part_count == 1 else f".part{placement.part_index}"
    man_rel = manifest_relpath(unit.project_name, unit.sequence_name,
                               (unit.shot_name or "shot") + suffix)
    man_doc = build_manifest_document(
        project_name=unit.project_name, sequence_name=unit.sequence_name,
        shot_name=unit.shot_name, source_path=unit.source_path,
        ltfs_dir=seq_dir_rel, frames=checksums,
    )
    man_sha = write_manifest(mount / man_rel, man_doc)
    app_part_manifest = get_settings().data_dir / "manifests" / man_rel
    write_manifest(app_part_manifest, man_doc)

    if placement.part_count == 1:
        container.manifest_ref = str(app_part_manifest)
        container.manifest_sha256 = man_sha
    elif placement.part_index == placement.part_count - 1:
        # last part written — merge every part into one whole-folder manifest
        merged_rel = manifest_relpath(unit.project_name, unit.sequence_name, unit.shot_name or "shot")
        merged_path = get_settings().data_dir / "manifests" / merged_rel
        merged_doc, _ = merge_part_manifests(
            sorted(app_part_manifest.parent.glob(f"{Path(merged_rel).stem}.part*.json")),
            base=man_doc,
        )
        container.manifest_ref = str(merged_path)
        container.manifest_sha256 = write_manifest(merged_path, merged_doc)
    container.written_at = _now()
    if readback and not mismatches:
        container.verified_at = _now()

    span = existing or ContentTapeSpan(sequence_container_id=container.id)
    span.tape_id = placement.tape_id
    span.ltfs_path = seq_dir_rel
    span.part_index = placement.part_index
    span.part_count = placement.part_count
    span.size_bytes = placement.size_bytes
    span.sha256 = man_sha  # manifest hash is the part's integrity anchor
    span.written_at = _now()
    if readback:
        span.verified_at = _now()
    if span.id is None:
        db.add(span)
    db.flush()
    return span


def _write_file_part(
    db: Session,
    unit: FileUnit,
    placement: Placement,
    mount: Path,
    root: Path,
    readback: bool,
    mismatches: list[str],
    drive_n: int,
) -> ContentTapeSpan:
    item = _upsert_item(db, unit)
    existing = _existing_span(
        db, seq_id=None, item_id=item.id,
        tape_id=placement.tape_id, part_index=placement.part_index,
    )
    if existing and existing.written_at and (existing.verified_at or not readback):
        return existing

    src = unit.path or Path(unit.source_path)
    rel = _ltfs_relpath(root, src)
    if placement.whole_file:
        dst = mount / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        digest = sha256_file(dst)
        if item.sha256 is None or placement.part_count == 1:
            item.sha256 = digest
        part_sha = digest
        ltfs_path = rel
        if readback and sha256_file(src) != digest:
            mismatches.append(unit_key(unit))
    else:
        start = placement.byte_range_start or 0
        end = placement.byte_range_end or unit.size
        ltfs_path = f"{rel}.part{placement.part_index:03d}"
        dst = mount / ltfs_path
        _copy_slice(src, dst, start, end)
        part_sha = sha256_file(dst)
        if readback and sha256_file(src, offset=start, length=end - start) != part_sha:
            mismatches.append(f"{unit_key(unit)} part {placement.part_index}")

    item.written_at = _now()
    if readback and unit_key(unit) not in {m.split(" part")[0] for m in mismatches}:
        item.verified_at = _now()

    span = existing or ContentTapeSpan(content_item_id=item.id)
    span.tape_id = placement.tape_id
    span.ltfs_path = ltfs_path
    span.part_index = placement.part_index
    span.part_count = placement.part_count
    span.size_bytes = placement.size_bytes
    span.sha256 = part_sha
    if not placement.whole_file:
        span.byte_range_start = placement.byte_range_start
        span.byte_range_end = placement.byte_range_end
    span.written_at = _now()
    if readback:
        span.verified_at = _now()
    if span.id is None:
        db.add(span)
    db.flush()
    return span


def _finalise_tape(db: Session, tape_id: int) -> None:
    tape = db.scalar(select(Tape).where(Tape.id == tape_id))
    if not tape:
        return
    total = 0
    for span in db.scalars(select(ContentTapeSpan).where(ContentTapeSpan.tape_id == tape_id)).all():
        total += span.size_bytes or 0
    tape.used_bytes = total
    now = _now()
    if tape.first_written_at is None:
        tape.first_written_at = now
    tape.last_written_at = now
    tape.write_pass_count = (tape.write_pass_count or 0) + 1
    cap = tape.capacity_native_bytes or get_settings().sim_tape_capacity_bytes
    # "full" once under 2% headroom remains
    tape.status = TapeStatus.full if (cap - total) < cap * 0.02 else TapeStatus.active
    db.flush()
