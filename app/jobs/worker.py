"""Single background worker thread.

Tape operations are serial by nature (one changer, one operator), so one worker
that runs one job at a time is the honest model. Jobs are persisted, so:

* a crash / VM restart mid-job is detected on startup (``running`` -> flagged
  ``interrupted``) and the operator can safely re-run it — write and verify are
  idempotent (framework doc §6);
* progress survives a page reload.
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import UTC, datetime

from sqlalchemy import select

from app.db import session_scope
from app.jobs.handlers import dispatch
from app.models import Job, JobStatus

_POLL_SECONDS = 1.5


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _make_progress(job_id: int):
    def cb(current: int, total: int, message: str = "") -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is None:
                return
            job.progress_current = int(current)
            job.progress_total = int(total)
            job.progress_message = (message or "")[:500]
            job.heartbeat_at = _utcnow()

    return cb


def _make_cancel(job_id: int):
    def check() -> bool:
        with session_scope() as s:
            job = s.get(Job, job_id)
            return bool(job and job.cancel_requested)

    return check


class JobWorker(threading.Thread):
    daemon = True

    def __init__(self) -> None:
        super().__init__(name="vader-job-worker")
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self._recover_interrupted()
        while not self._stop.is_set():
            claimed = self._claim_next()
            if claimed is None:
                time.sleep(_POLL_SECONDS)
                continue
            self._run_job(claimed)

    # --- internals ---------------------------------------------------

    def _recover_interrupted(self) -> None:
        with session_scope() as s:
            for job in s.scalars(select(Job).where(Job.status == JobStatus.running)).all():
                job.status = JobStatus.interrupted
                job.error = "worker restarted while this job was running — safe to re-run"
                job.finished_at = _utcnow()

    def _claim_next(self) -> int | None:
        with session_scope() as s:
            job = s.scalars(
                select(Job).where(Job.status == JobStatus.queued).order_by(Job.created_at).limit(1)
            ).first()
            if job is None:
                return None
            job.status = JobStatus.running
            job.started_at = _utcnow()
            job.heartbeat_at = _utcnow()
            return job.id

    def _run_job(self, job_id: int) -> None:
        progress = _make_progress(job_id)
        is_cancelled = _make_cancel(job_id)
        try:
            with session_scope() as s:
                job = s.get(Job, job_id)
                result = dispatch(s, job, progress, is_cancelled)
            with session_scope() as s:
                job = s.get(Job, job_id)
                if is_cancelled():
                    job.status = JobStatus.cancelled
                else:
                    job.status = JobStatus.completed
                    job.result = result
                job.finished_at = _utcnow()
        except Exception as exc:  # noqa: BLE001 - record everything
            with session_scope() as s:
                job = s.get(Job, job_id)
                job.status = JobStatus.failed
                job.error = f"{exc}\n\n{traceback.format_exc()}"
                job.finished_at = _utcnow()


_worker: JobWorker | None = None


def start_worker() -> JobWorker:
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = JobWorker()
        _worker.start()
    return _worker


def stop_worker() -> None:
    global _worker
    if _worker is not None:
        _worker.stop()
        _worker = None


def run_pending_jobs_inline(max_jobs: int = 50) -> None:
    """Test / CLI helper: run queued jobs synchronously in the current thread."""
    worker = JobWorker()
    worker._recover_interrupted()
    for _ in range(max_jobs):
        claimed = worker._claim_next()
        if claimed is None:
            return
        worker._run_job(claimed)
