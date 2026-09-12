"""Maps a queued :class:`Job` to the service call that does the work."""
from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.models import Job, JobType
from app.services.batch_format import run_batch_format
from app.services.export_csv import write_full_catalog_export
from app.services.restore import run_restore
from app.services.verification import run_verify
from app.services.writer import run_write

ProgressCb = Callable[[int, int, str], None]
CancelCb = Callable[[], bool]


def dispatch(db: Session, job: Job, progress: ProgressCb, is_cancelled: CancelCb) -> dict:
    params = dict(job.params or {})

    if job.job_type == JobType.write:
        return run_write(db, job_id=job.id, progress=progress, is_cancelled=is_cancelled, **params)

    if job.job_type == JobType.verify:
        return run_verify(db, job_id=job.id, progress=progress, is_cancelled=is_cancelled, **params)

    if job.job_type == JobType.restore:
        return run_restore(db, job_id=job.id, progress=progress, is_cancelled=is_cancelled, **params)

    if job.job_type == JobType.batch_format:
        return run_batch_format(db, job_id=job.id, progress=progress, is_cancelled=is_cancelled, **params)

    if job.job_type == JobType.backup:
        progress(0, 1, "exporting full catalog CSV")
        path = write_full_catalog_export(db)
        from app.scripts_support import run_db_backup

        db_backup = run_db_backup()
        progress(1, 1, "done")
        return {"catalog_csv": path, "db_backup": db_backup}

    raise RuntimeError(f"no handler for job type {job.job_type}")
