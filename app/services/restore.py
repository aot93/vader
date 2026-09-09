"""Search-driven, guided restore (framework doc §3.5).

``prepare_restore`` turns "recover shot X" into an explicit, auditable plan: the
exact tapes involved, in load order, with a warning up front if any required
tape is offsite, missing or flagged damaged. ``run_restore`` executes that plan.

Manifest exclusion is unconditional: the sidecar ``_manifests/`` and ``_catalog/``
paths are never written into restore output a downstream pipeline will consume.
``include_manifests`` (a deliberate, separate opt-in) only adds them to a review
copy, never to the primary deliverable path.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.hardware import HardwareError, get_hardware
from app.models import (
    ContentItem,
    ContentTapeSpan,
    RestoreRequest,
    RestoreStatus,
    SequenceContainer,
    Tape,
    TapeStatus,
)
from app.services import library as lib
from app.services.audit import record_audit
from app.services.checksums import sha256_file

ProgressCb = Callable[[int, int, str], None]
CancelCb = Callable[[], bool]

_EXCLUDED_PREFIXES = ("_manifests/", "_manifests", "_catalog/", "_catalog")


def _now() -> datetime:
    return datetime.now(UTC)


def _collect_spans(db: Session, seq_ids: list[int], item_ids: list[int]) -> list[ContentTapeSpan]:
    spans: list[ContentTapeSpan] = []
    if seq_ids:
        spans += db.scalars(
            select(ContentTapeSpan).where(ContentTapeSpan.sequence_container_id.in_(seq_ids))
        ).all()
    if item_ids:
        spans += db.scalars(
            select(ContentTapeSpan).where(ContentTapeSpan.content_item_id.in_(item_ids))
        ).all()
    return spans


def prepare_restore(
    db: Session,
    *,
    sequence_container_ids: list[int] | None = None,
    content_item_ids: list[int] | None = None,
    destination_path: str | None = None,
    include_manifests: bool = False,
    requested_by: str = "operator",
) -> RestoreRequest:
    seq_ids = sorted(set(sequence_container_ids or []))
    item_ids = sorted(set(content_item_ids or []))
    if not seq_ids and not item_ids:
        raise ValueError("select at least one sequence or content item to restore")

    spans = _collect_spans(db, seq_ids, item_ids)
    if not spans:
        raise ValueError("selected content has no tape spans recorded")

    state = get_hardware().library_status()
    by_tape: dict[int, list[ContentTapeSpan]] = {}
    for span in spans:
        by_tape.setdefault(span.tape_id, []).append(span)

    warnings: list[str] = []
    plan_tapes = []
    for tape_id, tspans in by_tape.items():
        tape = db.get(Tape, tape_id)
        slot = state.find_barcode_slot(tape.barcode)
        in_drive = next((d for d in state.drives if d.loaded_barcode == tape.barcode), None)
        in_library = slot is not None or in_drive is not None
        if tape.status == TapeStatus.damaged:
            warnings.append(f"tape {tape.barcode} is flagged DAMAGED — restore may fail")
        if not in_library:
            warnings.append(
                f"tape {tape.barcode} is not in the library "
                f"(location: {tape.physical_location or 'unknown'}) — fetch it before running"
            )
        files = []
        for span in sorted(tspans, key=lambda s: (s.ltfs_path, s.part_index)):
            if any(span.ltfs_path.startswith(p) for p in _EXCLUDED_PREFIXES):
                continue
            if span.sequence_container_id:
                c = db.get(SequenceContainer, span.sequence_container_id)
                files.append({
                    "kind": "sequence_dir",
                    "ltfs_path": span.ltfs_path,
                    "dest_rel": span.ltfs_path,
                    "label": f"{c.project_name}/{c.sequence_name or ''}/{c.shot_name}",
                    "part_index": span.part_index,
                    "part_count": span.part_count,
                    "manifest_ref": c.manifest_ref,
                })
            else:
                it = db.get(ContentItem, span.content_item_id)
                files.append({
                    "kind": "file",
                    "ltfs_path": span.ltfs_path,
                    "dest_rel": _split_dest_rel(span),
                    "label": f"{it.project_name or ''}/{it.filename}",
                    "sha256": span.sha256,
                    "whole_file": span.byte_range_start is None,
                    "byte_range_start": span.byte_range_start,
                    "byte_range_end": span.byte_range_end,
                    "part_index": span.part_index,
                    "part_count": span.part_count,
                })
        plan_tapes.append({
            "barcode": tape.barcode,
            "tape_id": tape.id,
            "in_library": in_library,
            "slot": slot,
            "status": tape.status.value,
            "location": tape.physical_location or "unknown",
            "files": files,
        })

    dest = destination_path or str(get_settings().data_dir / "restores")
    plan = {
        "destination_path": dest,
        "include_manifests": include_manifests,
        "tapes": plan_tapes,
        "commands_preview": _commands_preview(plan_tapes, dest),
    }

    req = RestoreRequest(
        requested_by=requested_by,
        sequence_container_ids=seq_ids,
        content_item_ids=item_ids,
        destination_path=dest,
        include_manifests=include_manifests,
        status=RestoreStatus.ready if not warnings else RestoreStatus.pending,
        plan=plan,
        warnings=warnings,
    )
    db.add(req)
    db.flush()
    record_audit(db, actor=requested_by, action="restore.prepared", entity_type="restore",
                 entity_id=req.id, detail={"tapes": [t["barcode"] for t in plan_tapes],
                                           "warnings": warnings})
    db.commit()
    return req


def _split_dest_rel(span: ContentTapeSpan) -> str:
    path = span.ltfs_path
    if span.byte_range_start is not None and f".part{span.part_index:03d}" in path:
        return path.rsplit(f".part{span.part_index:03d}", 1)[0]
    return path


def _commands_preview(plan_tapes: list[dict], dest: str) -> list[str]:
    out: list[str] = []
    for t in plan_tapes:
        slot = t["slot"] if t["slot"] is not None else "<slot>"
        out.append(f"mtx -f $CHANGER load {slot} 0")
        out.append("ltfs -o devname=$DRIVE0 /mnt/ltfs/drive0")
        for f in t["files"]:
            src = f"/mnt/ltfs/drive0/{f['ltfs_path']}"
            if f["kind"] == "sequence_dir":
                out.append(f"cp -r --no-clobber {src}/. {dest}/{f['dest_rel']}/")
            elif f.get("whole_file", True):
                out.append(f"cp {src} {dest}/{f['dest_rel']}")
            else:
                out.append(f"cat {src} >> {dest}/{f['dest_rel']}   # part {f['part_index'] + 1}/{f['part_count']}")
        out.append("umount /mnt/ltfs/drive0")
        out.append(f"mtx -f $CHANGER unload {slot} 0")
    return out


def run_restore(
    db: Session,
    *,
    job_id: int,
    restore_id: int,
    drive: int = 0,
    actor: str = "operator",
    progress: ProgressCb | None = None,
    is_cancelled: CancelCb | None = None,
) -> dict:
    progress = progress or (lambda *_: None)
    is_cancelled = is_cancelled or (lambda: False)
    req = db.get(RestoreRequest, restore_id)
    if not req:
        raise ValueError(f"unknown restore request {restore_id}")
    blocking = [w for w in req.warnings if "not in the library" in w or "DAMAGED" in w]
    if blocking:
        raise RuntimeError("restore blocked: " + "; ".join(blocking))

    req.status = RestoreStatus.in_progress
    db.commit()

    hw = get_hardware()
    dest_root = Path(req.destination_path or (get_settings().data_dir / "restores"))
    if dest_root == get_settings().data_dir / "restores":
        dest_root = dest_root / f"restore-{restore_id}"
    dest_root.mkdir(parents=True, exist_ok=True)

    plan = req.plan
    tapes = plan["tapes"]
    total_files = sum(len(t["files"]) for t in tapes) or 1
    done = 0
    restored: list[str] = []
    problems: list[str] = []

    try:
        for t in tapes:
            if is_cancelled():
                break
            state = hw.library_status()
            slot = state.find_barcode_slot(t["barcode"])
            if slot is not None:
                lib.load_tape(db, slot, drive, initiated_by=actor, job_id=job_id)
                db.commit()
            mount = hw.mount_ltfs(drive)
            try:
                for f in t["files"]:
                    if is_cancelled():
                        break
                    done += 1
                    try:
                        _restore_one(mount, dest_root, f, restored, problems, req.include_manifests)
                    except (OSError, HardwareError) as exc:
                        problems.append(f"{f['label']}: {exc}")
                    progress(done, total_files, f"{t['barcode']}: {f['label']}")
            finally:
                try:
                    hw.unmount_ltfs(drive)
                except HardwareError:
                    pass
                state = hw.library_status()
                free = next((s.number for s in state.slots if s.barcode is None), None)
                if free is not None:
                    lib.unload_tape(db, free, drive, initiated_by=actor, job_id=job_id)
                    db.commit()

        req.status = RestoreStatus.completed if not problems else RestoreStatus.failed
        req.fulfilled_at = _now()
        db.commit()
    except Exception:
        req.status = RestoreStatus.failed
        db.commit()
        raise

    result = {
        "restore_id": restore_id,
        "destination": str(dest_root),
        "files_restored": len(restored),
        "problems": problems,
    }
    record_audit(db, actor=actor, action="restore.completed", entity_type="restore",
                 entity_id=restore_id, detail=result)
    db.commit()
    return result


def _restore_one(mount: Path, dest_root: Path, f: dict, restored: list, problems: list,
                 include_manifests: bool) -> None:
    ltfs_path = f["ltfs_path"]
    if any(ltfs_path.startswith(p) for p in _EXCLUDED_PREFIXES) and not include_manifests:
        return
    if f["kind"] == "sequence_dir":
        src_dir = mount / ltfs_path
        dst_dir = dest_root / f["dest_rel"]
        dst_dir.mkdir(parents=True, exist_ok=True)
        if not src_dir.is_dir():
            problems.append(f"{f['label']}: sequence dir missing on tape ({ltfs_path})")
            return
        for item in sorted(src_dir.rglob("*")):
            if item.is_dir() or item.is_symlink():
                continue
            rel = item.relative_to(src_dir)
            target = dst_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                _copyfile(item, target)
            restored.append(str(target))
        return

    src = mount / ltfs_path
    dst = dest_root / f["dest_rel"]
    dst.parent.mkdir(parents=True, exist_ok=True)
    if f.get("whole_file", True):
        if not src.is_file():
            problems.append(f"{f['label']}: missing on tape ({ltfs_path})")
            return
        _copyfile(src, dst)
        if f.get("sha256") and sha256_file(dst) != f["sha256"]:
            problems.append(f"{f['label']}: checksum mismatch after restore")
    else:
        # byte-range split part: append in order
        if not src.is_file():
            problems.append(f"{f['label']}: part missing on tape ({ltfs_path})")
            return
        mode = "wb" if f["part_index"] == 0 else "ab"
        with open(src, "rb") as fi, open(dst, mode) as fo:
            while True:
                block = fi.read(4 * 1024 * 1024)
                if not block:
                    break
                fo.write(block)
    restored.append(str(dst))


def _copyfile(src: Path, dst: Path) -> None:
    import shutil

    shutil.copyfile(src, dst)
