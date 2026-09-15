"""Maps a queued :class:`Job` to the service call that does the work."""
from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.models import Job, JobType
from app.services import library as lib
from app.services.batch_format import run_batch_format
from app.services.export_csv import write_full_catalog_export
from app.services.restore import run_restore
from app.services.tape_import import run_tape_import
from app.services.verification import run_verify
from app.services.writer import run_write

ProgressCb = Callable[[int, int, str], None]
CancelCb = Callable[[], bool]


def dispatch(
    db: Session, job: Job, progress: ProgressCb, is_cancelled: CancelCb,
    claimed_drives: frozenset[int] = frozenset(),
) -> dict:
    params = dict(job.params or {})

    if job.job_type == JobType.write:
        return run_write(db, job_id=job.id, progress=progress, is_cancelled=is_cancelled, **params)

    if job.job_type == JobType.verify:
        return run_verify(db, job_id=job.id, progress=progress, is_cancelled=is_cancelled, **params)

    if job.job_type == JobType.restore:
        # restore has no `drive` param of its own (see app.jobs.drives.
        # drive_need) — the worker auto-picks any one free drive via the
        # claim registry and hands it in here.
        drive = next(iter(claimed_drives), 0)
        return run_restore(db, job_id=job.id, drive=drive, progress=progress,
                           is_cancelled=is_cancelled, **params)

    if job.job_type == JobType.batch_format:
        return run_batch_format(db, job_id=job.id, claimed_drives=claimed_drives,
                                progress=progress, is_cancelled=is_cancelled, **params)

    if job.job_type == JobType.format:
        drive, barcode = params["drive"], params["barcode"]
        progress(0, 1, f"formatting drive {drive}: {barcode}")
        lib.format_tape(db, drive, barcode, force=params.get("force", False),
                        initiated_by=job.created_by, job_id=job.id)
        db.commit()
        # progress AFTER commit so the write lock is released first (writer.py does the same)
        progress(1, 1, "done")
        return {"drive": drive, "barcode": barcode}

    if job.job_type == JobType.tape_import:
        return run_tape_import(db, job_id=job.id, claimed_drives=claimed_drives,
                               progress=progress, is_cancelled=is_cancelled, **params)

    if job.job_type == JobType.backup:
        progress(0, 1, "exporting full catalog CSV")
        path = write_full_catalog_export(db)
        from app.scripts_support import run_db_backup

        db_backup = run_db_backup()
        progress(1, 1, "done")
        return {"catalog_csv": path, "db_backup": db_backup}

    raise RuntimeError(f"no handler for job type {job.job_type}")
