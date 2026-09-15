"""app.jobs.drives in isolation — no JobWorker involved."""
from __future__ import annotations

import pytest

from app.hardware import get_hardware
from app.jobs import drives
from app.models import Job, JobType


@pytest.fixture(autouse=True)
def _clear_registry():
    """The claimed-drive set is process-global module state — clear it
    before every test in this file so tests can't leak claims into each
    other."""
    drives._claimed.clear()
    yield
    drives._claimed.clear()


def _job(job_type: JobType, **params) -> Job:
    return Job(job_type=job_type, params=params)


@pytest.mark.parametrize(
    "job_type,params,expected",
    [
        (JobType.write, {"drive": 1}, drives.ExactDrives(frozenset({1}))),
        (JobType.verify, {"drive": 0}, drives.ExactDrives(frozenset({0}))),
        (JobType.format, {"drive": 1}, drives.ExactDrives(frozenset({1}))),
        (JobType.write, {}, drives.ExactDrives(frozenset({0}))),  # defaults to drive 0
        (JobType.restore, {"restore_id": 5}, drives.AnyDrives(min_n=1, max_n=1)),
        (JobType.batch_format, {"barcodes": ["A", "B", "C"]}, drives.AnyDrives(min_n=1, max_n=3)),
        (JobType.tape_import, {"barcodes": ["A"]}, drives.AnyDrives(min_n=1, max_n=1)),
        (JobType.tape_import, {"barcodes": []}, drives.AnyDrives(min_n=1, max_n=1)),  # never max_n=0
        (JobType.backup, {}, drives.NoDrive()),
    ],
)
def test_drive_need_mapping(job_type, params, expected):
    assert drives.drive_need(_job(job_type, **params)) == expected


def test_try_claim_exact_succeeds_once_then_blocks(seeded):
    assert drives.try_claim({0}) is True
    assert drives.try_claim({0}) is False  # already claimed
    assert drives.try_claim({1}) is True  # different drive, unaffected

    drives.release({0})
    assert drives.try_claim({0}) is True


def test_try_claim_exact_is_all_or_nothing(seeded):
    assert drives.try_claim({1}) is True
    # {0, 1}: drive 1 already claimed -> whole request fails, drive 0 must
    # NOT be partially claimed as a side effect
    assert drives.try_claim({0, 1}) is False
    assert drives.try_claim({0}) is True


def test_try_claim_any_respects_min_and_max(seeded):
    # SIM_DRIVES=2 -> exactly drives {0, 1} physically free at test start
    assert drives.try_claim_any(min_n=1, max_n=1) == {0}
    assert drives.try_claim_any(min_n=1, max_n=5) == {1}  # only 1 left free
    assert drives.try_claim_any(min_n=1, max_n=5) is None  # none left


def test_try_claim_any_returns_none_below_min(seeded):
    drives.try_claim({0})
    drives.try_claim({1})
    assert drives.try_claim_any(min_n=1, max_n=1) is None


def test_release_frees_drives_for_a_later_claim(seeded):
    claimed = drives.try_claim_any(min_n=1, max_n=2)
    assert claimed == {0, 1}
    assert drives.try_claim_any(min_n=1, max_n=1) is None

    drives.release(claimed)
    assert drives.try_claim_any(min_n=1, max_n=1) is not None


def test_claim_reconciles_against_physical_hardware_state(seeded):
    """A drive can be physically busy for a reason outside job tracking
    (e.g. an operator's manual load from the Library page) — the registry
    must still exclude it, not just track its own bookkeeping."""
    hw = get_hardware()
    state = hw.library_status()
    slot = next(s.number for s in state.slots if s.barcode and not s.is_cleaning_tape)
    hw.load(slot, 0)  # drive 0 now physically occupied, with no registry claim at all

    assert drives.claimed() == frozenset()  # registry itself thinks nothing is claimed
    assert drives.try_claim({0}) is False  # but the physical check still blocks it
    assert drives.try_claim_any(min_n=1, max_n=2) == {1}  # only drive 1 offered


def test_try_claim_for_no_drive_job_always_succeeds(seeded):
    job = _job(JobType.backup)
    assert drives.try_claim_for(job) == set()


def test_try_claim_for_exact_drive_job(seeded):
    job = _job(JobType.write, drive=0)
    assert drives.try_claim_for(job) == {0}
    # a second job wanting the same drive can't start yet
    assert drives.try_claim_for(_job(JobType.write, drive=0)) is None
    # a different drive is unaffected
    assert drives.try_claim_for(_job(JobType.verify, drive=1)) == {1}


def test_try_claim_for_any_drives_job(seeded):
    job = _job(JobType.tape_import, barcodes=["A", "B", "C"])
    claimed = drives.try_claim_for(job)
    assert claimed == {0, 1}  # capped by physical drive count, not barcode count
    assert drives.try_claim_for(_job(JobType.write, drive=0)) is None
