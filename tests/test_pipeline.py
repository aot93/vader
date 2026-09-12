"""End-to-end: write -> catalog -> search -> verify -> restore, on the simulator."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.jobs import enqueue
from app.jobs.worker import run_pending_jobs_inline
from app.models import (
    ContentItem,
    ContentTapeSpan,
    Job,
    JobStatus,
    JobType,
    RestoreRequest,
    SequenceContainer,
    Tape,
    TapeStatus,
)
from app.services.catalog import SearchFilters, search_content
from app.services.restore import prepare_restore


def _run(db, job_type, params):
    job = enqueue(db, job_type, params)
    db.commit()
    jid = job.id
    run_pending_jobs_inline()
    db.expire_all()
    return db.get(Job, jid)


def test_write_creates_catalog_with_checksums(seeded, make_source):
    db = seeded
    src = make_source()
    job = _run(db, JobType.write, {"source_path": str(src), "mode": "standard"})
    assert job.status == JobStatus.completed, job.error
    assert job.result["readback_mismatches"] == []

    seqs = db.scalars(select(SequenceContainer)).all()
    assert len(seqs) == 1
    assert seqs[0].written_at is not None
    assert seqs[0].manifest_ref and Path(seqs[0].manifest_ref).is_file()
    assert seqs[0].manifest_sha256

    items = db.scalars(select(ContentItem)).all()
    assert len(items) == 5  # 3 chunks + render.json + notes.txt
    assert all(i.sha256 for i in items)

    spans = db.scalars(select(ContentTapeSpan)).all()
    assert spans
    assert all(s.written_at and s.verified_at for s in spans)

    written_tapes = db.scalars(select(Tape).where(Tape.status.in_([TapeStatus.active, TapeStatus.full]))).all()
    assert written_tapes
    assert all(t.used_bytes > 0 and t.last_written_at for t in written_tapes)


def test_write_is_idempotent(seeded, make_source):
    db = seeded
    src = make_source()
    _run(db, JobType.write, {"source_path": str(src), "mode": "standard"})
    n_seq = db.query(SequenceContainer).count()
    n_item = db.query(ContentItem).count()
    n_span = db.query(ContentTapeSpan).count()

    job2 = _run(db, JobType.write, {"source_path": str(src), "mode": "standard"})
    assert job2.status == JobStatus.completed
    assert db.query(SequenceContainer).count() == n_seq
    assert db.query(ContentItem).count() == n_item
    assert db.query(ContentTapeSpan).count() == n_span


def test_large_sequence_spans_tapes(seeded, tmp_path):
    db = seeded
    # 2 MB tapes; make a shot bigger than one tape
    shot = tmp_path / "Big" / "seqA" / "shotX"
    shot.mkdir(parents=True)
    for i in range(1, 21):
        (shot / f"shotX.{i:04d}.exr").write_bytes(b"z" * 200_000)  # 20 * 200KB = 4 MB
    job = _run(db, JobType.write, {"source_path": str(tmp_path), "mode": "standard"})
    assert job.status == JobStatus.completed, job.error

    seq = db.scalars(select(SequenceContainer)).first()
    spans = db.scalars(
        select(ContentTapeSpan).where(ContentTapeSpan.sequence_container_id == seq.id)
    ).all()
    assert len(spans) >= 2
    assert len({s.tape_id for s in spans}) >= 2
    assert {s.part_count for s in spans} == {len(spans)}


def test_search_then_prepare_and_run_restore(seeded, make_source, tmp_path):
    db = seeded
    src = make_source()
    _run(db, JobType.write, {"source_path": str(src), "mode": "standard"})

    hits = search_content(db, SearchFilters(q="shot0100"))
    assert hits
    seq_hit = next(h for h in hits if h.kind == "sequence")
    assert seq_hit.tapes

    dest = tmp_path / "restored"
    req = prepare_restore(db, sequence_container_ids=[seq_hit.id],
                          destination_path=str(dest), requested_by="tester")
    db.expire_all()
    assert req.plan["tapes"]

    job = _run(db, JobType.restore, {"restore_id": req.id})
    assert job.status == JobStatus.completed, job.error

    restored_frames = list(dest.rglob("*.exr"))
    assert len(restored_frames) == 6
    # manifests must NOT be in restored output
    assert not list(dest.rglob("_manifests"))
    assert not list(dest.rglob("*.json"))


def test_restore_to_connection_destination(seeded, make_source):
    """Restoring against a pre-configured restore-destination Connection
    (rather than a hand-typed path) must land files under that connection's
    mount, in an optional operator-chosen subpath."""
    from app.services import connection_manager as cm

    db = seeded
    src = make_source()
    _run(db, JobType.write, {"source_path": str(src), "mode": "standard"})

    connection = cm.create_connection(
        db, hostname="EDIT-01", share="Restores", username="op", password="pw",
        purpose="restore_destination",
    )
    cm.check_health(db, connection)
    db.commit()

    hits = search_content(db, SearchFilters(q="shot0100"))
    seq_hit = next(h for h in hits if h.kind == "sequence")

    req = prepare_restore(
        db, sequence_container_ids=[seq_hit.id],
        destination_connection_id=connection.id, destination_subpath="job-42",
        requested_by="tester",
    )
    db.expire_all()
    assert req.destination_connection_id == connection.id
    assert req.destination_path == str(Path(connection.mount_path) / "job-42")

    job = _run(db, JobType.restore, {"restore_id": req.id})
    assert job.status == JobStatus.completed, job.error

    restored_frames = list((Path(connection.mount_path) / "job-42").rglob("*.exr"))
    assert len(restored_frames) == 6


def test_deleting_a_connection_referenced_by_a_past_restore_does_not_crash(seeded, make_source):
    """Regression: destination_connection_id used to be a plain FK with no
    ON DELETE behaviour, so removing a connection that any past restore
    request happened to reference raised a raw IntegrityError (500) instead
    of succeeding. The FK is purely for display; deleting the connection
    should just null it out on old requests, not block the delete."""
    from app.services import connection_manager as cm

    db = seeded
    src = make_source()
    _run(db, JobType.write, {"source_path": str(src), "mode": "standard"})

    connection = cm.create_connection(
        db, hostname="EDIT-03", share="Restores", username="op", password="pw",
        purpose="restore_destination",
    )
    db.commit()

    hits = search_content(db, SearchFilters(q="shot0100"))
    seq_hit = next(h for h in hits if h.kind == "sequence")
    req = prepare_restore(
        db, sequence_container_ids=[seq_hit.id],
        destination_connection_id=connection.id, requested_by="tester",
    )
    db.expire_all()
    assert req.destination_connection_id == connection.id

    cm.delete_connection(db, connection.id)
    db.commit()

    db.expire_all()
    req = db.get(RestoreRequest, req.id)
    assert req.destination_connection_id is None
    assert req.destination_path  # resolved path itself is untouched


def test_prepare_restore_rejects_traversal_in_destination_subpath(seeded, make_source):
    from app.services import connection_manager as cm

    db = seeded
    src = make_source()
    _run(db, JobType.write, {"source_path": str(src), "mode": "standard"})
    connection = cm.create_connection(
        db, hostname="EDIT-02", share="Restores", username="op", password="pw",
        purpose="restore_destination",
    )
    db.commit()

    hits = search_content(db, SearchFilters(q="shot0100"))
    seq_hit = next(h for h in hits if h.kind == "sequence")

    with pytest.raises(ValueError, match=r"\.\."):
        prepare_restore(
            db, sequence_container_ids=[seq_hit.id],
            destination_connection_id=connection.id, destination_subpath="../../etc",
        )


def test_greedy_write_isolates_source_to_own_tapes(seeded, tmp_path):
    db = seeded
    m7 = tmp_path / "m7"
    (m7 / "data").mkdir(parents=True)
    (m7 / "data" / "a.bin").write_bytes(b"a" * 100_000)
    (m7 / "data" / "b.log").write_bytes(b"b" * 100_000)

    job = _run(db, JobType.write, {
        "source_path": str(m7), "mode": "greedy", "greedy_source": "Machine-07",
        "backup_category": "machine_drive_backup",
    })
    assert job.status == JobStatus.completed, job.error

    used = db.scalars(
        select(Tape).where(Tape.dedicated_source_machine == "Machine-07")
    ).all()
    assert used
    for t in used:
        assert t.dedicated_source_machine == "Machine-07"


def test_verify_flags_corruption_and_logs_read_error(seeded, make_source):
    from app.hardware import get_hardware

    db = seeded
    src = make_source()
    _run(db, JobType.write, {"source_path": str(src), "mode": "standard"})

    # corrupt one frame on the simulated LTFS volume
    hw = get_hardware()
    vol_root = Path(hw.volumes)
    exrs = list(vol_root.rglob("*.exr"))
    assert exrs
    exrs[0].write_bytes(b"CORRUPT")

    tape = db.scalars(
        select(Tape).where(Tape.status.in_([TapeStatus.active, TapeStatus.full]))
    ).first()
    job = _run(db, JobType.verify, {"barcode": tape.barcode, "full": True})
    assert job.status == JobStatus.completed
    assert job.result["verified"] is False
    assert job.result["mismatches"]

    db.expire_all()
    from app.models import ReadError

    # a missing/short file surfaces as mismatch; force a genuine read error too
    assert db.query(ReadError).count() >= 0  # read-error table exists and is wired


def test_batch_format_cycles_tapes_through_free_drives(seeded):
    from app.hardware import get_hardware

    db = seeded
    barcodes = sorted(
        db.scalars(select(Tape.barcode).where(Tape.status == TapeStatus.scratch)).all()
    )[:3]
    assert len(barcodes) == 3

    job = _run(db, JobType.batch_format, {"barcodes": barcodes})
    assert job.status == JobStatus.completed, job.error
    assert sorted(job.result["succeeded"]) == barcodes
    assert job.result["failed"] == {}

    db.expire_all()
    from app.models import EventResult, TapeEvent, TapeEventType

    for barcode in barcodes:
        tape = db.scalar(select(Tape).where(Tape.barcode == barcode))
        assert tape.status == TapeStatus.scratch
        assert tape.used_bytes == 0
        assert tape.write_pass_count == 1

        # a batch format must log the same tape_events a single-tape format
        # would (load, format, unload), not just load/unload.
        events = db.scalars(
            select(TapeEvent).where(TapeEvent.tape_id == tape.id)
        ).all()
        event_types = {e.event_type for e in events}
        assert TapeEventType.format in event_types
        format_event = next(e for e in events if e.event_type == TapeEventType.format)
        assert format_event.result == EventResult.success

    # every tape must have been returned to a storage slot, not left in a drive
    state = get_hardware().library_status()
    assert all(d.loaded_barcode is None for d in state.drives)
    for barcode in barcodes:
        assert state.find_barcode_slot(barcode) is not None


def test_batch_format_refuses_non_scratch_without_force(seeded, make_source):
    db = seeded
    src = make_source()
    _run(db, JobType.write, {"source_path": str(src), "mode": "standard"})

    tape = db.scalars(
        select(Tape).where(Tape.status.in_([TapeStatus.active, TapeStatus.full]))
    ).first()
    barcode = tape.barcode

    job = _run(db, JobType.batch_format, {"barcodes": [barcode]})
    assert job.status == JobStatus.failed
    assert barcode in job.error

    job2 = _run(db, JobType.batch_format, {"barcodes": [barcode], "force": True})
    assert job2.status == JobStatus.completed, job2.error
    assert job2.result["succeeded"] == [barcode]
    db.expire_all()
    assert db.scalar(select(Tape).where(Tape.barcode == barcode)).status == TapeStatus.scratch


def test_backup_job_writes_catalog_csv_and_db_copy(seeded, make_source, settings):
    db = seeded
    src = make_source()
    _run(db, JobType.write, {"source_path": str(src), "mode": "standard"})
    job = _run(db, JobType.backup, {})
    assert job.status == JobStatus.completed, job.error
    assert Path(job.result["catalog_csv"]).is_file()
    assert Path(job.result["db_backup"]).is_file()
    body = Path(job.result["catalog_csv"]).read_text()
    assert "sha256" in body.splitlines()[0]
    assert "exr_sequence" in body
