"""Importing pre-existing LTFS tapes: cataloging, safety (never scratch, never
written to), and idempotency."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.hardware import get_hardware
from app.jobs import enqueue
from app.jobs.worker import run_pending_jobs_inline
from app.models import (
    ContentTapeSpan,
    Job,
    JobStatus,
    JobType,
    SequenceContainer,
    Tape,
    TapeStatus,
)
from app.services.catalog import SearchFilters, search_content


def _run(db, job_type, params):
    job = enqueue(db, job_type, params)
    db.commit()
    jid = job.id
    run_pending_jobs_inline()
    db.expire_all()
    return db.get(Job, jid)


def _seed_legacy_tape(hw, barcode: str, *, frames: int = 4, frame_size: int = 500) -> None:
    """Write a shot-folder tree straight into a tape's volume dir, mimicking
    content an external (non-Vader) LTFS writer already put there."""
    vol = hw.volumes / barcode
    shot = vol / "seq010" / "shot0100"
    shot.mkdir(parents=True)
    for i in range(1, frames + 1):
        (shot / f"shot0100.{i:04d}.exr").write_bytes(bytes([i % 256]) * frame_size)
    (vol / ".ltfs_label").write_text(f"barcode={barcode}\n")


def test_import_catalogs_a_pre_existing_tape_and_flips_it_to_archived(seeded):
    db = seeded
    hw = get_hardware()
    # TEST001L8 was auto-registered as 'scratch' by the seeded fixture (as a
    # real inventory refresh would) even though it actually holds real content
    # — exactly the dangerous state import must correct.
    _seed_legacy_tape(hw, "TEST001L8")

    job = _run(db, JobType.tape_import, {"barcodes": ["TEST001L8"]})
    assert job.status == JobStatus.completed, job.error
    assert job.result["succeeded"]["TEST001L8"] >= 1

    tape = db.scalar(select(Tape).where(Tape.barcode == "TEST001L8"))
    assert tape.status == TapeStatus.archived
    assert tape.used_bytes > 0
    assert tape.last_verified_at is not None
    assert tape.write_pass_count == 0  # import never counts as a write pass

    seq = db.scalar(select(SequenceContainer))
    assert seq is not None
    assert seq.source_path.startswith("tape://TEST001L8/")
    assert seq.verified_at is not None
    assert seq.written_at is None  # original write date is genuinely unknown

    span = db.scalar(select(ContentTapeSpan).where(ContentTapeSpan.tape_id == tape.id))
    assert span.part_index == 0 and span.part_count == 1
    assert span.verified_at is not None
    assert span.written_at is None

    hits = search_content(db, SearchFilters())
    assert hits


def test_import_is_idempotent(seeded):
    db = seeded
    hw = get_hardware()
    _seed_legacy_tape(hw, "TEST002L8")

    _run(db, JobType.tape_import, {"barcodes": ["TEST002L8"]})
    n_spans = db.query(ContentTapeSpan).count()
    n_seqs = db.query(SequenceContainer).count()

    job2 = _run(db, JobType.tape_import, {"barcodes": ["TEST002L8"]})
    assert job2.status == JobStatus.completed, job2.error
    assert db.query(ContentTapeSpan).count() == n_spans
    assert db.query(SequenceContainer).count() == n_seqs


def test_import_refuses_damaged_or_retired_tapes_up_front(seeded):
    db = seeded
    tape = db.scalar(select(Tape).where(Tape.barcode == "TEST003L8"))
    tape.status = TapeStatus.damaged
    db.commit()

    job = _run(db, JobType.tape_import, {"barcodes": ["TEST003L8"]})
    assert job.status == JobStatus.failed
    assert "damaged" in job.error


def test_import_fails_clearly_if_tape_not_physically_present_but_still_registers_it_safely(seeded):
    db = seeded
    job = _run(db, JobType.tape_import, {"barcodes": ["GHOST999L8"]})
    assert job.status == JobStatus.failed
    assert "not in a storage slot" in job.error

    # registration happens before the load attempt, so even a tape that turns
    # out to be physically missing never sits around mislabeled as scratch.
    tape = db.scalar(select(Tape).where(Tape.barcode == "GHOST999L8"))
    assert tape is not None
    assert tape.status == TapeStatus.archived


def test_read_only_mount_actually_blocks_writes(seeded):
    hw = get_hardware()
    _seed_legacy_tape(hw, "TEST004L8")
    hw.load(4, 0)
    mp = hw.mount_ltfs(0, read_only=True)
    try:
        with pytest.raises(PermissionError):
            (mp / "should_not_be_writable.txt").write_text("nope")
    finally:
        hw.unmount_ltfs(0)
        hw.unload(4, 0)
