"""A post-work cleanup unload (returning a tape to a storage slot once a
write/verify/restore is otherwise done) used to fail silently in these three
services — ``except HardwareError: pass``, unlike tape_import.py/
batch_format.py which already fold it into the job's own error message. A
tape could sit stuck loaded in a drive while its job reported a clean
`completed`. See PLAN_tape_import_optimization.md, "New finding, post
item-3: transient tape-changer arm errors".
"""
from __future__ import annotations

from app.hardware import HardwareError, get_hardware
from app.jobs import enqueue
from app.jobs.worker import run_pending_jobs_inline
from app.models import Job, JobStatus, JobType, RestoreStatus, Tape, TapeStatus
from app.services import library as lib
from app.services.catalog import SearchFilters, search_content
from app.services.restore import prepare_restore


def _scratch_barcode(db) -> str:
    from sqlalchemy import select

    return db.scalars(select(Tape.barcode).where(Tape.status == TapeStatus.scratch)).first()


def _always_fail_unload(slot, drive):
    raise HardwareError("Illegal Request (sense 53/03)")


def test_write_job_surfaces_a_failed_cleanup_unload_instead_of_hiding_it(
    seeded, make_source, monkeypatch
):
    hw = get_hardware()
    db = seeded
    barcode = _scratch_barcode(db)
    src = make_source("ProjA", frames=1, chunks=0, with_config=False)
    job = enqueue(db, JobType.write, {
        "source_path": str(src), "mode": "standard", "target_barcode": barcode, "drive": 0,
    })
    db.commit()

    monkeypatch.setattr(hw, "unload", _always_fail_unload)
    monkeypatch.setattr(lib.time, "sleep", lambda *_: None)

    run_pending_jobs_inline()

    db.expire_all()
    finished = db.get(Job, job.id)
    # the archival work itself still succeeded — a stuck-tape cleanup
    # failure shouldn't be reported as data loss
    assert finished.status == JobStatus.completed, finished.error
    assert finished.result["cleanup_warnings"], "a failed unload was silently swallowed"
    assert "failed to unload" in finished.result["cleanup_warnings"][0]
    # and it's a real, visible signal: the tape really is still loaded
    assert hw.library_status().drive(0).loaded_barcode == barcode


def test_verify_job_surfaces_a_failed_cleanup_unload(seeded, make_source, monkeypatch):
    hw = get_hardware()
    db = seeded
    barcode = _scratch_barcode(db)
    src = make_source("ProjA", frames=1, chunks=0, with_config=False)
    write_job = enqueue(db, JobType.write, {
        "source_path": str(src), "mode": "standard", "target_barcode": barcode, "drive": 0,
    })
    db.commit()
    run_pending_jobs_inline()
    db.expire_all()
    assert db.get(Job, write_job.id).status == JobStatus.completed

    monkeypatch.setattr(hw, "unload", _always_fail_unload)
    monkeypatch.setattr(lib.time, "sleep", lambda *_: None)

    verify_job = enqueue(db, JobType.verify, {"barcode": barcode, "full": True, "drive": 0})
    db.commit()
    run_pending_jobs_inline()

    db.expire_all()
    finished = db.get(Job, verify_job.id)
    assert finished.status == JobStatus.completed, finished.error
    assert finished.result["cleanup_warnings"], "a failed unload was silently swallowed"
    assert "failed to unload" in finished.result["cleanup_warnings"][0]


def test_restore_job_surfaces_a_failed_cleanup_unload_as_a_problem(
    seeded, make_source, monkeypatch
):
    hw = get_hardware()
    db = seeded
    src = make_source()
    write_job = enqueue(db, JobType.write, {"source_path": str(src), "mode": "standard"})
    db.commit()
    run_pending_jobs_inline()
    db.expire_all()
    assert db.get(Job, write_job.id).status == JobStatus.completed

    hits = search_content(db, SearchFilters(q="shot0100"))
    seq_hit = next(h for h in hits if h.kind == "sequence")
    req = prepare_restore(db, sequence_container_ids=[seq_hit.id], requested_by="tester")
    db.expire_all()

    monkeypatch.setattr(hw, "unload", _always_fail_unload)
    monkeypatch.setattr(lib.time, "sleep", lambda *_: None)

    restore_job = enqueue(db, JobType.restore, {"restore_id": req.id})
    db.commit()
    run_pending_jobs_inline()

    db.expire_all()
    finished = db.get(Job, restore_job.id)
    # a cleanup failure is exactly "a tape needs manual attention" — that
    # should flip the restore to failed, not hide behind a clean success
    assert finished.status == JobStatus.completed, finished.error
    assert any("failed to unload" in p for p in finished.result["problems"])
    updated_req = db.get(type(req), req.id)
    assert updated_req.status == RestoreStatus.failed
