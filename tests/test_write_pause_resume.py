"""Pause/resume control for running write jobs (plan doc: "New feature
request: pause/resume control for running write jobs").

Covers the bug the design write-up surfaced along the way — a cancelled
write job used to be reported `failed` instead of `cancelled`, because
writer.py's checkpoint raised instead of breaking, like every other job
type's checkpoint already did — plus the new pause/resume mechanism itself,
which reuses that same fixed checkpoint.
"""
from __future__ import annotations

from sqlalchemy import select

from app.db import session_scope
from app.hardware import get_hardware
from app.jobs import drives, enqueue
from app.jobs.worker import run_pending_jobs_inline
from app.models import ContentTapeSpan, Job, JobStatus, JobType, Tape, TapeStatus


def _scratch_barcode(db) -> str:
    return db.scalars(select(Tape.barcode).where(Tape.status == TapeStatus.scratch)).first()


def _written_span_count(db) -> int:
    db.expire_all()
    return sum(1 for s in db.scalars(select(ContentTapeSpan)).all() if s.written_at is not None)


def _flip_after_first_sha256(writer_mod, job_id: int, field: str):
    """Patches writer_mod.sha256_file so the first call flips `field` (either
    "cancel_requested" or "pause_requested") on the job — takes effect at the
    *next* placement's checkpoint, same as an operator clicking the button
    mid-run would."""
    real_sha256 = writer_mod.sha256_file
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            with session_scope() as s:
                setattr(s.get(Job, job_id), field, True)
        return real_sha256(*args, **kwargs)

    return flaky


def test_cancelling_a_running_write_job_reports_cancelled_not_failed(
    seeded, make_source, monkeypatch
):
    """Direct regression test for the raise-vs-break bug: before the fix,
    this asserted `JobStatus.failed` with "cancelled by operator" as the
    error text."""
    import app.services.writer as writer_mod

    db = seeded
    barcode = _scratch_barcode(db)
    src = make_source("ProjA")  # multiple content units -> multiple placements
    job = enqueue(db, JobType.write, {
        "source_path": str(src), "mode": "standard", "target_barcode": barcode, "drive": 0,
    })
    db.commit()
    job_id = job.id

    monkeypatch.setattr(
        writer_mod, "sha256_file", _flip_after_first_sha256(writer_mod, job_id, "cancel_requested")
    )

    run_pending_jobs_inline()

    db.expire_all()
    finished = db.get(Job, job_id)
    assert finished.status == JobStatus.cancelled, finished.error
    # cancelled jobs don't record a result (same as every other job type) —
    # what matters here is that this *reached* the cancelled branch at all,
    # rather than raising into _run_job's except-Exception failure handler,
    # which is what actually wrote the partial spans below.
    written = _written_span_count(db)
    assert 0 < written < 6, f"expected a partial write (some but not all of 6), got {written}"
    assert drives.claimed() == frozenset(), "cancelling a write job must still release its drive"


def test_pausing_a_running_write_job_reports_paused_and_releases_the_drive(
    seeded, make_source, monkeypatch
):
    import app.services.writer as writer_mod

    db = seeded
    barcode = _scratch_barcode(db)
    src = make_source("ProjA")
    job = enqueue(db, JobType.write, {
        "source_path": str(src), "mode": "standard", "target_barcode": barcode, "drive": 0,
    })
    db.commit()
    job_id = job.id

    monkeypatch.setattr(
        writer_mod, "sha256_file", _flip_after_first_sha256(writer_mod, job_id, "pause_requested")
    )

    run_pending_jobs_inline()

    db.expire_all()
    finished = db.get(Job, job_id)
    assert finished.status == JobStatus.paused, finished.error
    written = _written_span_count(db)
    assert 0 < written < 6, f"expected a partial write (some but not all of 6), got {written}"
    assert drives.claimed() == frozenset(), "a paused job must not sit there holding a drive"
    assert get_hardware().library_status().drive(0).loaded_barcode is None, (
        "a paused job must not leave the tape loaded"
    )


def test_resuming_a_paused_write_job_completes_the_remaining_placements(
    seeded, make_source, monkeypatch
):
    import app.services.writer as writer_mod

    db = seeded
    barcode = _scratch_barcode(db)
    src = make_source("ProjA")
    job = enqueue(db, JobType.write, {
        "source_path": str(src), "mode": "standard", "target_barcode": barcode, "drive": 0,
    })
    db.commit()
    job_id = job.id

    monkeypatch.setattr(
        writer_mod, "sha256_file", _flip_after_first_sha256(writer_mod, job_id, "pause_requested")
    )
    run_pending_jobs_inline()
    monkeypatch.undo()

    db.expire_all()
    paused = db.get(Job, job_id)
    assert paused.status == JobStatus.paused
    written_after_pause = _written_span_count(db)
    assert 0 < written_after_pause < 6

    # "Resume" is just re-enqueuing a clone of the same params (what POST
    # /jobs/{id}/resume does) — the write pipeline's existing placement-level
    # idempotent skip does the rest.
    resumed = enqueue(db, paused.job_type, paused.params or {})
    db.commit()
    run_pending_jobs_inline()

    db.expire_all()
    finished = db.get(Job, resumed.id)
    assert finished.status == JobStatus.completed, finished.error
    assert _written_span_count(db) == 6, "resuming must pick up every unit not already written"
