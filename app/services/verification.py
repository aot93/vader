"""Verification & integrity (framework doc §3.4) — the part that makes
"recovery" mean something.

A verify run re-reads content from a tape's LTFS volume, re-hashes it and
compares against the SHA256 recorded at write time. It never touches the
original source. Read errors surfaced by the OS/LTFS layer are captured against
the tape record so a pattern of degradation on one physical tape becomes
visible over time rather than being a one-off surprise.
"""
from __future__ import annotations

import random
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.hardware import HardwareError, TapeReadError, get_hardware
from app.models import (
    ContentItem,
    ContentTapeSpan,
    ReadError,
    SequenceContainer,
    Tape,
)
from app.services import library as lib
from app.services.audit import record_audit
from app.services.checksums import sha256_file
from app.services.manifests import load_manifest

ProgressCb = Callable[[int, int, str], None]
CancelCb = Callable[[], bool]


def _now() -> datetime:
    return datetime.now(UTC)


class _Check:
    __slots__ = ("kind", "path", "expected", "label", "span_id", "ltfs_path")

    def __init__(self, kind, path, expected, label, span_id, ltfs_path):
        self.kind = kind
        self.path = path
        self.expected = expected
        self.label = label
        self.span_id = span_id
        self.ltfs_path = ltfs_path


def _load_and_mount(db: Session, tape: Tape, drive: int, actor: str, job_id: int) -> Path:
    hw = get_hardware()
    state = hw.library_status()
    slot = state.find_barcode_slot(tape.barcode)
    if slot is None:
        loaded = next((d for d in state.drives if d.loaded_barcode == tape.barcode), None)
        if loaded is None:
            raise HardwareError(f"tape {tape.barcode} is not in the library")
        drive = loaded.number
    else:
        lib.load_tape(db, slot, drive, initiated_by=actor, job_id=job_id)
        db.commit()
    return hw.mount_ltfs(drive)


def _unload(db: Session, drive: int, actor: str, job_id: int) -> None:
    hw = get_hardware()
    try:
        hw.unmount_ltfs(drive)
    except HardwareError:
        pass
    state = hw.library_status()
    free = next((s.number for s in state.slots if s.barcode is None), None)
    if free is not None:
        lib.unload_tape(db, free, drive, initiated_by=actor, job_id=job_id)
        db.commit()


def _build_checks(db: Session, tape: Tape, mount: Path) -> list[_Check]:
    checks: list[_Check] = []
    spans = db.scalars(select(ContentTapeSpan).where(ContentTapeSpan.tape_id == tape.id)).all()
    for span in spans:
        if span.sequence_container_id:
            c = db.get(SequenceContainer, span.sequence_container_id)
            # verify the on-tape manifest, then every frame it lists
            suffix = "" if span.part_count == 1 else f".part{span.part_index}"
            man_rel = _manifest_rel(c, suffix)
            man_path = mount / man_rel
            checks.append(_Check("manifest", man_path, span.sha256,
                                 f"{c.project_name}/{c.shot_name} manifest", span.id, man_rel))
            if man_path.is_file():
                try:
                    doc = load_manifest(man_path)
                except Exception:  # noqa: BLE001
                    doc = {"frames": []}
                for fr in doc.get("frames", []):
                    fp = mount / span.ltfs_path / fr["filename"]
                    checks.append(_Check("frame", fp, fr["sha256"],
                                         f"{c.shot_name}/{fr['filename']}", span.id,
                                         f"{span.ltfs_path}/{fr['filename']}"))
        else:
            it = db.get(ContentItem, span.content_item_id)
            checks.append(_Check("file", mount / span.ltfs_path, span.sha256,
                                 f"{it.project_name or ''}/{it.filename}", span.id, span.ltfs_path))
    return checks


def _manifest_rel(c: SequenceContainer, suffix: str) -> str:
    from app.services.manifests import manifest_relpath

    return manifest_relpath(c.project_name, c.sequence_name, (c.shot_name or "shot") + suffix)


def run_verify(
    db: Session,
    *,
    job_id: int,
    barcode: str,
    drive: int = 0,
    full: bool = False,
    sample_fraction: float | None = None,
    actor: str = "operator",
    progress: ProgressCb | None = None,
    is_cancelled: CancelCb | None = None,
) -> dict:
    settings = get_settings()
    frac = 1.0 if full else (sample_fraction or settings.default_verify_sample_fraction)
    frac = max(0.0, min(frac, 1.0))
    progress = progress or (lambda *_: None)
    is_cancelled = is_cancelled or (lambda: False)

    tape = db.scalar(select(Tape).where(Tape.barcode == barcode))
    if not tape:
        raise ValueError(f"unknown tape {barcode}")

    mount = _load_and_mount(db, tape, drive, actor, job_id)
    checked = 0
    mismatches: list[str] = []
    read_errors: list[str] = []
    ok_span_ids: set[int] = set()
    bad_span_ids: set[int] = set()
    try:
        all_checks = _build_checks(db, tape, mount)
        rng = random.Random(f"{barcode}:{job_id}")
        if frac >= 1.0:
            selected = all_checks
        else:
            k = max(1, int(len(all_checks) * frac)) if all_checks else 0
            selected = rng.sample(all_checks, k) if k else []
        total = len(selected)

        for i, chk in enumerate(selected, 1):
            if is_cancelled():
                break
            try:
                if not chk.path.is_file():
                    raise TapeReadError(f"missing on tape: {chk.ltfs_path}")
                actual = sha256_file(chk.path)
            except (OSError, TapeReadError) as exc:
                read_errors.append(f"{chk.ltfs_path}: {exc}")
                db.add(ReadError(tape_id=tape.id, job_id=job_id, operation="verify",
                                 ltfs_path=chk.ltfs_path, error_text=str(exc)))
                if chk.span_id:
                    bad_span_ids.add(chk.span_id)
                checked += 1
                progress(i, total, f"read error: {chk.label}")
                continue

            if chk.expected and actual != chk.expected:
                mismatches.append(f"{chk.label} (expected {chk.expected[:12]}…, got {actual[:12]}…)")
                if chk.span_id:
                    bad_span_ids.add(chk.span_id)
            elif chk.span_id:
                ok_span_ids.add(chk.span_id)
            checked += 1
            progress(i, total, chk.label)

        # mark verified: spans with at least one OK check and no failures
        now = _now()
        verified_spans = ok_span_ids - bad_span_ids
        for span_id in verified_spans:
            span = db.get(ContentTapeSpan, span_id)
            span.verified_at = now
            if span.sequence_container_id:
                db.get(SequenceContainer, span.sequence_container_id).verified_at = now
            elif span.content_item_id:
                db.get(ContentItem, span.content_item_id).verified_at = now

        if not mismatches and not read_errors:
            tape.last_verified_at = now
        if read_errors and not mismatches:
            pass  # tape not marked damaged automatically; operator reviews read-error history
        if mismatches:
            tape.notes = (
                f"{(tape.notes + chr(10)) if tape.notes else ''}"
                f"{now.date()}: verify found {len(mismatches)} checksum mismatch(es)"
            )
        db.commit()
    finally:
        _unload(db, drive, actor, job_id)
        db.commit()

    result = {
        "barcode": barcode,
        "checks_run": checked,
        "sample_fraction": frac,
        "mismatches": mismatches,
        "read_errors": read_errors,
        "verified": not mismatches and not read_errors,
    }
    record_audit(db, actor=actor, action="verify.completed", entity_type="tape",
                 entity_id=barcode, detail=result)
    db.commit()
    return result


def tapes_due_for_reverification(db: Session):
    """Framework doc §3.4 / §5.5 — surface tapes not verified within the window."""
    from datetime import timedelta

    settings = get_settings()
    cutoff = _now() - timedelta(days=30 * settings.reverify_months)
    out = []
    for tape in db.scalars(select(Tape)).all():
        if tape.status.value in {"scratch", "retired"}:
            continue
        if tape.last_written_at is None:
            continue
        if tape.last_verified_at is None or tape.last_verified_at < cutoff:
            out.append(tape)
    return out
