"""Background job execution, concurrent and keyed by drive.

Two physically independent tape drives exist, so a slow job on one
shouldn't block a job that only needs the other — the worker scans every
queued job each tick and starts whichever ones can currently get the
drive(s) they need (see ``app.jobs.drives``), each in its own thread, rather
than running exactly one job globally at a time. Physical arm moves
(load/unload/clean) are still serialised app-wide by a lock inside
``app.services.library`` — the changer only has one arm regardless of how
many drives exist.

Jobs are persisted, so:

* a crash / VM restart mid-job is detected on startup (``running`` -> flagged
  ``interrupted``) and the operator can safely re-run it — write and verify are
  idempotent (framework doc §6);
* progress survives a page reload.

Fairness note: scanning "every queued job, start whichever is currently
startable" means an old job wanting several drives at once could in theory
keep losing out to newer jobs that opportunistically grab a single free
drive as it appears. At this app's scale (two drives, one operator) that's
an accepted tradeoff, not engineered around here.
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import UTC, datetime

from sqlalchemy import select

from app.db import session_scope
from app.hardware import get_hardware
from app.jobs import drives
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


def _make_pause(job_id: int):
    def check() -> bool:
        with session_scope() as s:
            job = s.get(Job, job_id)
            return bool(job and job.pause_requested)

    return check


class JobWorker(threading.Thread):
    daemon = True

    def __init__(self) -> None:
        super().__init__(name="vader-job-worker")
        # Named _stop_event, not _stop — threading.Thread has its own private
        # _stop() method it calls internally during shutdown; shadowing it
        # with an Event crashes that cleanup with 'Event' object is not
        # callable, which breaks graceful shutdown on every restart.
        self._stop_event = threading.Event()
        # One thread per currently-running job, keyed by job id, so a job
        # already started this tick is never claimed/started again on the
        # next, and shutdown/tests can enumerate what's in flight.
        self._threads: dict[int, threading.Thread] = {}
        self._threads_lock = threading.Lock()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        self._recover_interrupted()
        while not self._stop_event.is_set():
            try:
                started = self._scan_and_start()
            except Exception:
                # A transient hardware error here (e.g. `mtx status` timing
                # out) must not kill this thread — it's the *only* thing
                # that ever starts a queued job. Before this was caught, one
                # bad tick permanently stopped all job processing until the
                # next service restart, silently: nothing but the systemd
                # journal ("Exception in thread vader-job-worker") showed
                # it, and every job submitted afterwards just sat in
                # 'queued' forever. Log and retry next tick instead.
                traceback.print_exc()
                started = 0
            if not started:
                time.sleep(_POLL_SECONDS)

    # --- internals ---------------------------------------------------

    def _recover_interrupted(self) -> None:
        with session_scope() as s:
            for job in s.scalars(select(Job).where(Job.status == JobStatus.running)).all():
                job.status = JobStatus.interrupted
                job.error = "worker restarted while this job was running — safe to re-run"
                job.finished_at = _utcnow()

    def _has_running(self) -> bool:
        with self._threads_lock:
            return bool(self._threads)

    def _scan_and_start(self) -> int:
        """Look at every queued job, oldest first, and start every one whose
        drive need can be satisfied against the claim registry right now —
        not just the first. Returns how many were started.

        Each job's claim-and-flip-to-running is its own short transaction,
        committed *before* that job's thread is started — not one
        transaction wrapping the whole scan. A started job's thread opens
        its own session immediately and, for anything beyond a trivial job,
        commits inside its own work; if that happened while this scan's
        transaction (with its own uncommitted status flip) were still open,
        the two would contend for SQLite's single write lock — the same
        deadlock-shaped bug already fixed in tape_import/verify/restore,
        just newly possible here since starting a job's thread now happens
        inside the scan loop instead of after a single job was claimed.
        """
        with session_scope() as s:
            queued_ids = [
                j.id for j in s.scalars(
                    select(Job).where(Job.status == JobStatus.queued).order_by(Job.created_at)
                ).all()
            ]

        # Fetched once per tick, not once per queued job: on real hardware
        # library_status() shells out to `mtx status` (up to a 120s
        # timeout), and drive occupancy can't change *during* this loop
        # anyway — every claim attempt below reconciling against a state
        # fetched N times over was N-1 redundant subprocess calls for the
        # same answer.
        state = get_hardware().library_status()

        started = 0
        for job_id in queued_ids:
            with session_scope() as s:
                job = s.get(Job, job_id)
                if job is None or job.status != JobStatus.queued:
                    continue  # claimed/changed by a concurrent pass already
                claimed = drives.try_claim_for(job, state)
                if claimed is None:
                    continue
                job.status = JobStatus.running
                job.started_at = _utcnow()
                job.heartbeat_at = _utcnow()
            # `s` has committed and closed here — safe to start the thread now.
            self._start_job_thread(job_id, claimed)
            started += 1
        return started

    def _start_job_thread(self, job_id: int, claimed: set[int]) -> None:
        t = threading.Thread(
            target=self._run_job, args=(job_id, claimed),
            name=f"vader-job-{job_id}", daemon=True,
        )
        with self._threads_lock:
            self._threads[job_id] = t
        t.start()

    def _run_job(self, job_id: int, claimed: set[int]) -> None:
        progress = _make_progress(job_id)
        is_cancelled = _make_cancel(job_id)
        is_paused = _make_pause(job_id)
        try:
            try:
                with session_scope() as s:
                    job = s.get(Job, job_id)
                    result = dispatch(s, job, progress, is_cancelled, frozenset(claimed),
                                      is_paused=is_paused)
                with session_scope() as s:
                    job = s.get(Job, job_id)
                    # cancel wins over pause if somehow both were requested —
                    # cancel is the terminal-for-good action.
                    if is_cancelled():
                        job.status = JobStatus.cancelled
                    elif is_paused():
                        job.status = JobStatus.paused
                        job.result = result
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
        finally:
            drives.release(claimed)
            with self._threads_lock:
                self._threads.pop(job_id, None)

    def _join_all(self, timeout: float = 30.0) -> None:
        with self._threads_lock:
            in_flight = list(self._threads.values())
        for t in in_flight:
            t.join(timeout=timeout)


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


def run_pending_jobs_inline(timeout: float = 30.0) -> None:
    """Test / CLI helper: run queued jobs synchronously (from the caller's
    point of view — internally still concurrent per drive) in the current
    process. Alternates "start everything currently startable" with "wait
    briefly for something to finish", since a drive-blocked job only
    becomes startable once an earlier one releases its claim.

    Bounded by wall-clock time, not a fixed iteration count: an earlier
    version capped at 50 scan iterations (each waiting up to 0.05s while
    something ran) regardless of elapsed time — fine against the fast
    simulator this is normally used with, but it would silently give up
    mid-drain, leaving a still-queued job queued forever with no error
    surfaced, the moment total run time exceeded that ~2.5s budget (e.g.
    this helper pointed at slower, real-hardware-timed work). `timeout`
    bounds actual elapsed time instead, so a job that's genuinely still
    running (not stalled) never gets starved just because a lot of
    scan/sleep cycles happened along the way.
    """
    worker = JobWorker()
    worker._recover_interrupted()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        started = worker._scan_and_start()
        if started == 0 and not worker._has_running():
            break
        if worker._has_running():
            time.sleep(0.05)
    worker._join_all()
