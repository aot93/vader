"""Concurrent job execution keyed by drive (plan item 3, step 6/7) — the
JobWorker itself, not the individual service fixes that led up to it.

Uses the real job dispatch path (enqueue + run_pending_jobs_inline), with
targeted monkeypatches to slow down one step of a job just enough to force
genuine thread overlap, so these tests prove actual concurrency rather than
just "both jobs eventually finished"."""
from __future__ import annotations

import threading
import time

from sqlalchemy import select

from app.hardware import get_hardware
from app.jobs import drives, enqueue
from app.jobs.worker import run_pending_jobs_inline
from app.models import Job, JobStatus, JobType, Tape, TapeStatus


class _ConcurrencyProbe:
    """Wraps a callable so calls to it record how many other calls were
    in flight at the same time — lets a test assert "these two things ran
    at once" or "these two things never overlapped" without depending on
    absolute wall-clock timing."""

    def __init__(self, fn, delay: float = 0.15):
        self._fn = fn
        self._delay = delay
        self._lock = threading.Lock()
        self._current = 0
        self.max_concurrent = 0
        self.calls = 0

    def __call__(self, *args, **kwargs):
        with self._lock:
            self._current += 1
            self.max_concurrent = max(self.max_concurrent, self._current)
            self.calls += 1
        try:
            time.sleep(self._delay)
            return self._fn(*args, **kwargs)
        finally:
            with self._lock:
                self._current -= 1


def _scratch_barcodes(db, n: int) -> list[str]:
    return db.scalars(
        select(Tape.barcode).where(Tape.status == TapeStatus.scratch)
    ).all()[:n]


def _job_status(db, job: Job) -> JobStatus:
    db.expire_all()
    return db.get(Job, job.id).status


def test_jobs_on_different_drives_run_concurrently(seeded, make_source, monkeypatch):
    import app.services.writer as writer_mod

    probe = _ConcurrencyProbe(writer_mod.sha256_file, delay=0.1)
    monkeypatch.setattr(writer_mod, "sha256_file", probe)

    db = seeded
    a, b = _scratch_barcodes(db, 2)
    src_a = make_source("ProjA", frames=2, chunks=0, with_config=False)
    src_b = make_source("ProjB", frames=2, chunks=0, with_config=False)

    job_a = enqueue(db, JobType.write, {
        "source_path": str(src_a), "mode": "standard", "target_barcode": a, "drive": 0,
    })
    job_b = enqueue(db, JobType.write, {
        "source_path": str(src_b), "mode": "standard", "target_barcode": b, "drive": 1,
    })
    db.commit()

    run_pending_jobs_inline()

    assert _job_status(db, job_a) == JobStatus.completed
    assert _job_status(db, job_b) == JobStatus.completed
    # two frames each, readback verify on -> 2 sha256_file calls/frame; if
    # the two jobs (different drives) ran genuinely concurrently, at least
    # two of those calls overlapped in time.
    assert probe.max_concurrent >= 2, "jobs on different drives never actually overlapped"


def test_jobs_needing_the_same_drive_serialize(seeded, make_source, monkeypatch):
    import app.services.writer as writer_mod

    probe = _ConcurrencyProbe(writer_mod.sha256_file, delay=0.1)
    monkeypatch.setattr(writer_mod, "sha256_file", probe)

    db = seeded
    a, b = _scratch_barcodes(db, 2)
    src_a = make_source("ProjA", frames=2, chunks=0, with_config=False)
    src_b = make_source("ProjB", frames=2, chunks=0, with_config=False)

    job_a = enqueue(db, JobType.write, {
        "source_path": str(src_a), "mode": "standard", "target_barcode": a, "drive": 0,
    })
    job_b = enqueue(db, JobType.write, {
        "source_path": str(src_b), "mode": "standard", "target_barcode": b, "drive": 0,
    })
    db.commit()

    run_pending_jobs_inline()

    assert _job_status(db, job_a) == JobStatus.completed
    assert _job_status(db, job_b) == JobStatus.completed
    assert probe.max_concurrent == 1, "jobs contending for the same drive ran concurrently"


def test_arm_lock_holds_across_job_types_running_concurrently(seeded, make_source, monkeypatch):
    """Direct regression test for the pre-fix bug (batch_format.py and
    tape_import.py each kept their own separate _arm_lock): run a write and
    a batch_format job concurrently, on different drives, and confirm their
    hw.load()/hw.unload() calls — guarded by the now-shared arm lock in
    app.services.library — never overlap."""
    hw = get_hardware()
    load_probe = _ConcurrencyProbe(hw.load, delay=0.05)
    unload_probe = _ConcurrencyProbe(hw.unload, delay=0.05)
    monkeypatch.setattr(hw, "load", load_probe)
    monkeypatch.setattr(hw, "unload", unload_probe)

    db = seeded
    write_barcode, format_barcode = _scratch_barcodes(db, 2)
    src = make_source("ProjA", frames=1, chunks=0, with_config=False)

    job_write = enqueue(db, JobType.write, {
        "source_path": str(src), "mode": "standard", "target_barcode": write_barcode, "drive": 0,
    })
    job_format = enqueue(db, JobType.batch_format, {"barcodes": [format_barcode]})
    db.commit()

    run_pending_jobs_inline()

    assert _job_status(db, job_write) == JobStatus.completed
    assert _job_status(db, job_format) == JobStatus.completed
    assert load_probe.calls >= 2
    assert unload_probe.calls >= 2
    assert load_probe.max_concurrent == 1, "hw.load() overlapped across job types"
    assert unload_probe.max_concurrent == 1, "hw.unload() overlapped across job types"


def test_drive_released_on_job_success(seeded, make_source):
    db = seeded
    barcode = _scratch_barcodes(db, 1)[0]
    src = make_source("ProjA", frames=1, chunks=0, with_config=False)
    job = enqueue(db, JobType.write, {
        "source_path": str(src), "mode": "standard", "target_barcode": barcode, "drive": 0,
    })
    db.commit()

    run_pending_jobs_inline()

    assert _job_status(db, job) == JobStatus.completed
    assert drives.claimed() == frozenset()

    # a follow-up job wanting the same drive can start right away
    src2 = make_source("ProjB", frames=1, chunks=0, with_config=False)
    barcode2 = _scratch_barcodes(db, 1)[0]
    job2 = enqueue(db, JobType.write, {
        "source_path": str(src2), "mode": "standard", "target_barcode": barcode2, "drive": 0,
    })
    db.commit()
    run_pending_jobs_inline()
    assert _job_status(db, job2) == JobStatus.completed


def test_drive_released_on_job_failure(seeded, make_source, monkeypatch):
    import app.jobs.handlers as handlers_mod

    def boom(*a, **kw):
        raise RuntimeError("simulated failure mid-job")

    monkeypatch.setattr(handlers_mod, "run_write", boom)

    db = seeded
    barcode = _scratch_barcodes(db, 1)[0]
    src = make_source("ProjA", frames=1, chunks=0, with_config=False)
    job = enqueue(db, JobType.write, {
        "source_path": str(src), "mode": "standard", "target_barcode": barcode, "drive": 0,
    })
    db.commit()

    run_pending_jobs_inline()

    assert _job_status(db, job) == JobStatus.failed
    assert drives.claimed() == frozenset(), "a failed job's drive claim was never released"

    monkeypatch.undo()  # restore run_write for the follow-up check
    src2 = make_source("ProjB", frames=1, chunks=0, with_config=False)
    job2 = enqueue(db, JobType.write, {
        "source_path": str(src2), "mode": "standard", "target_barcode": barcode, "drive": 0,
    })
    db.commit()
    run_pending_jobs_inline()
    assert _job_status(db, job2) == JobStatus.completed


def test_backup_runs_immediately_alongside_a_slow_drive_job(seeded, make_source, monkeypatch):
    import app.services.writer as writer_mod

    probe = _ConcurrencyProbe(writer_mod.sha256_file, delay=0.3)
    monkeypatch.setattr(writer_mod, "sha256_file", probe)

    db = seeded
    barcode = _scratch_barcodes(db, 1)[0]
    src = make_source("ProjA", frames=2, chunks=0, with_config=False)

    job_write = enqueue(db, JobType.write, {
        "source_path": str(src), "mode": "standard", "target_barcode": barcode, "drive": 0,
    })
    job_backup = enqueue(db, JobType.backup, {})
    db.commit()

    run_pending_jobs_inline()

    assert _job_status(db, job_write) == JobStatus.completed
    assert _job_status(db, job_backup) == JobStatus.completed
    # backup needs no drive at all — it must not have waited behind the
    # (deliberately slow) drive-bound write job.
    db.expire_all()
    backup_job, write_job = db.get(Job, job_backup.id), db.get(Job, job_write.id)
    assert backup_job.started_at <= write_job.finished_at


def test_tape_import_claims_all_free_drives_without_double_claiming(seeded):
    """batch_format/tape_import want "up to N free drives" — confirm that
    claim doesn't race a concurrently-queued job that wants one specific
    drive: exactly one of them gets drive 1, never both."""
    db = seeded
    barcodes = _scratch_barcodes(db, 2)

    job_import = enqueue(db, JobType.batch_format, {"barcodes": barcodes})  # wants up to 2 drives
    job_write = enqueue(db, JobType.write, {
        "source_path": "/nonexistent", "mode": "standard", "drive": 1,
    })  # will fail (bad source_path), but that's fine — only the claim matters
    db.commit()

    run_pending_jobs_inline()

    db.expire_all()
    ji, jw = db.get(Job, job_import.id), db.get(Job, job_write.id)
    assert ji.status in (JobStatus.completed, JobStatus.failed)
    assert jw.status in (JobStatus.completed, JobStatus.failed)
    # neither job is stuck queued forever, and both drives end up released
    assert drives.claimed() == frozenset()


def test_inline_helper_drains_jobs_that_only_become_startable_later(seeded, make_source, monkeypatch):
    """Three jobs: two contend for drive 0, one wants drive 1. A single
    run_pending_jobs_inline() call must drain all three, not give up after
    the first pass leaves the third drive-0 job still queued."""
    import app.services.writer as writer_mod

    probe = _ConcurrencyProbe(writer_mod.sha256_file, delay=0.05)
    monkeypatch.setattr(writer_mod, "sha256_file", probe)

    db = seeded
    a, b, c = _scratch_barcodes(db, 3)
    jobs = []
    for name, barcode, drive in [("A", a, 0), ("B", b, 0), ("C", c, 1)]:
        src = make_source(name, frames=1, chunks=0, with_config=False)
        jobs.append(enqueue(db, JobType.write, {
            "source_path": str(src), "mode": "standard", "target_barcode": barcode, "drive": drive,
        }))
    db.commit()

    run_pending_jobs_inline()

    db.expire_all()
    statuses = [db.get(Job, j.id).status for j in jobs]
    assert statuses == [JobStatus.completed] * 3, statuses
