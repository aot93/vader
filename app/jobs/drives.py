"""In-process registry of which physical drive numbers are currently claimed
by a running job.

Exactly one ``JobWorker`` thread runs in one ``uvicorn`` process
(``app/main.py``'s ``lifespan`` hook calls ``start_worker()`` once, no
``--workers`` flag) — a plain in-memory ``set[int]`` behind a ``Lock`` is
sufficient. Nothing here needs to survive a restart, and nothing should: a
fresh process naturally starts with an empty claimed set, and
``JobWorker._recover_interrupted()`` independently flips any stale
``running`` row to ``interrupted`` at startup regardless.

Every claim re-derives "free" against the *physical* hardware state
(``get_hardware().library_status().drives``), not just this module's own
bookkeeping — a drive can be busy for a reason outside job tracking (e.g.
an operator's manual load from the Library page), and that must still
correctly block a claim.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from app.hardware import get_hardware
from app.models import Job, JobType

_lock = threading.Lock()
_claimed: set[int] = set()


@dataclass(frozen=True)
class ExactDrives:
    """Needs these specific drive numbers, all of them, or none."""
    drives: frozenset[int]


@dataclass(frozen=True)
class AnyDrives:
    """Needs at least ``min_n`` free drives, and will take up to ``max_n``
    if more are available (e.g. tape_import fanning out across every free
    drive it can get)."""
    min_n: int
    max_n: int


@dataclass(frozen=True)
class NoDrive:
    """No hardware interaction at all — always immediately startable."""


DriveNeed = ExactDrives | AnyDrives | NoDrive


def drive_need(job: Job) -> DriveNeed:
    params = job.params or {}
    if job.job_type in (JobType.write, JobType.verify, JobType.format):
        return ExactDrives(frozenset({int(params.get("drive", 0))}))
    if job.job_type == JobType.restore:
        # No drive param exists for restore today (it always used to default
        # to drive 0 with no way to pick otherwise) — auto-pick any one free
        # drive instead, so restore actually benefits from concurrency
        # rather than always contending with itself on drive 0.
        return AnyDrives(min_n=1, max_n=1)
    if job.job_type in (JobType.batch_format, JobType.tape_import):
        n_barcodes = len(params.get("barcodes") or [])
        return AnyDrives(min_n=1, max_n=max(n_barcodes, 1))
    return NoDrive()  # backup


def _physically_free() -> set[int]:
    return {d.number for d in get_hardware().library_status().drives if d.loaded_barcode is None}


def try_claim(drives: set[int]) -> bool:
    """All-or-nothing claim of an exact set of drive numbers."""
    with _lock:
        free = _physically_free() - _claimed
        if not drives <= free:
            return False
        _claimed.update(drives)
        return True


def try_claim_any(min_n: int, max_n: int) -> set[int] | None:
    """Claim up to ``max_n`` free drives, or none at all if fewer than
    ``min_n`` are currently free."""
    with _lock:
        free = sorted(_physically_free() - _claimed)
        if len(free) < min_n:
            return None
        chosen = set(free[:max_n])
        _claimed.update(chosen)
        return chosen


def try_claim_for(job: Job) -> set[int] | None:
    """Attempt to claim whatever drives ``job`` needs right now. Returns the
    claimed set (empty for a ``NoDrive`` job — always startable) or ``None``
    if the need can't be satisfied yet."""
    need = drive_need(job)
    if isinstance(need, NoDrive):
        return set()
    if isinstance(need, ExactDrives):
        return set(need.drives) if try_claim(set(need.drives)) else None
    if isinstance(need, AnyDrives):
        return try_claim_any(need.min_n, need.max_n)
    raise TypeError(f"unhandled drive need: {need!r}")  # pragma: no cover


def release(drives: set[int]) -> None:
    with _lock:
        _claimed.difference_update(drives)


def claimed() -> frozenset[int]:
    """Read-only snapshot, mainly for tests/diagnostics."""
    with _lock:
        return frozenset(_claimed)
