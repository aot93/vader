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
from app.services.tape_import import run_tape_import


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


def test_import_reports_intra_tape_scan_progress(seeded):
    """A long tape_import must not look frozen: some progress() call has to
    happen between mounting and unloading, not just before/after. Also
    guards against the DB session/lock bug this introduced (the callback
    opens its own session, which would deadlock against the still-open
    per-tape catalog transaction if that transaction isn't committed first —
    see the s.commit() in _import_one_tape)."""
    db = seeded
    hw = get_hardware()
    _seed_legacy_tape(hw, "TEST005L8", frames=6)

    job = enqueue(db, JobType.tape_import, {"barcodes": ["TEST005L8"]})
    db.commit()

    calls: list[tuple[int, int, str]] = []
    result = run_tape_import(
        db, job_id=job.id, barcodes=["TEST005L8"],
        progress=lambda current, total, message: calls.append((current, total, message)),
    )
    assert result["succeeded"]["TEST005L8"] >= 1

    scan_calls = [c for c in calls if "scanned" in c[2] and "TEST005L8" in c[2]]
    assert scan_calls, calls
    assert all(total == 1 for _current, total, _msg in scan_calls)


def test_fast_scan_lists_content_without_hashing_it(seeded):
    db = seeded
    hw = get_hardware()
    _seed_legacy_tape(hw, "TEST006L8")

    job = _run(db, JobType.tape_import, {"barcodes": ["TEST006L8"], "verify": False})
    assert job.status == JobStatus.completed, job.error
    assert job.result["succeeded"]["TEST006L8"] >= 1

    tape = db.scalar(select(Tape).where(Tape.barcode == "TEST006L8"))
    assert tape.status == TapeStatus.archived
    assert tape.used_bytes > 0  # size is known without reading file content
    assert tape.last_scanned_at is not None
    assert tape.last_verified_at is None  # never hashed, so not "verified"

    seq = db.scalar(select(SequenceContainer))
    assert seq.verified_at is None
    assert seq.manifest_ref is None

    span = db.scalar(select(ContentTapeSpan).where(ContentTapeSpan.tape_id == tape.id))
    assert span.verified_at is None
    assert span.sha256 is None
    assert span.size_bytes > 0  # listing still records size


def test_a_later_full_import_hashes_content_a_fast_scan_only_listed(seeded):
    db = seeded
    hw = get_hardware()
    _seed_legacy_tape(hw, "TEST007L8")

    _run(db, JobType.tape_import, {"barcodes": ["TEST007L8"], "verify": False})
    n_spans_after_scan = db.query(ContentTapeSpan).count()

    job2 = _run(db, JobType.tape_import, {"barcodes": ["TEST007L8"]})  # verify defaults to True
    assert job2.status == JobStatus.completed, job2.error
    # same span rows reused, not duplicated — the idempotent-skip check only
    # skips already-*verified* spans, so an unverified one is re-processed.
    assert db.query(ContentTapeSpan).count() == n_spans_after_scan

    tape = db.scalar(select(Tape).where(Tape.barcode == "TEST007L8"))
    assert tape.last_scanned_at is not None
    assert tape.last_verified_at is not None

    span = db.scalar(select(ContentTapeSpan).where(ContentTapeSpan.tape_id == tape.id))
    assert span.verified_at is not None
    assert span.sha256 is not None


def test_import_never_overwrites_capacity_in_simulator_mode(seeded):
    """_measure_capacity_bytes is real-hardware-only — statvfs() on the
    simulator's plain-directory "mount" would report the host disk's free
    space, not the (deliberately tiny, for fast tests) simulated tape
    capacity."""
    db = seeded
    hw = get_hardware()
    _seed_legacy_tape(hw, "TEST001L8")
    tape_before = db.scalar(select(Tape).where(Tape.barcode == "TEST001L8"))
    seeded_capacity = tape_before.capacity_native_bytes

    job = _run(db, JobType.tape_import, {"barcodes": ["TEST001L8"]})
    assert job.status == JobStatus.completed, job.error

    tape = db.scalar(select(Tape).where(Tape.barcode == "TEST001L8"))
    assert tape.capacity_native_bytes == seeded_capacity


def test_measure_capacity_bytes_only_applies_on_real_hardware(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import app.services.tape_import as ti

    monkeypatch.setattr(ti, "get_settings", lambda: SimpleNamespace(hardware_backend="real"))
    result = ti._measure_capacity_bytes(tmp_path)
    assert result is not None and result > 0

    monkeypatch.setattr(ti, "get_settings", lambda: SimpleNamespace(hardware_backend="simulator"))
    assert ti._measure_capacity_bytes(tmp_path) is None


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
