"""app.services.library.resolve_drives — the drive-selection logic shared by
batch_format.py/tape_import.py (previously duplicated verbatim, including
each module's own private ``_free_drives()``, in both)."""
from __future__ import annotations

from app.hardware import get_hardware
from app.services import library as lib


def test_resolve_drives_uses_the_given_claim_as_is_sorted_and_truncated():
    # order-scrambled and over-long on purpose — must come back sorted and
    # capped at n, regardless of physical hardware state
    assert lib.resolve_drives(frozenset({5, 1, 3}), n=2) == [1, 3]
    assert lib.resolve_drives(frozenset({1}), n=5) == [1]


def test_resolve_drives_discovers_free_drives_when_no_claim_given(seeded):
    # SIM_DRIVES=2 -> drives 0 and 1 both physically free at test start
    assert lib.resolve_drives(None, n=5) == [0, 1]
    assert lib.resolve_drives(None, n=1) == [0]


def test_resolve_drives_excludes_physically_occupied_drives_when_no_claim_given(seeded):
    hw = get_hardware()
    state = hw.library_status()
    slot = next(s.number for s in state.slots if s.barcode and not s.is_cleaning_tape)
    hw.load(slot, 0)  # drive 0 now physically occupied

    assert lib.resolve_drives(None, n=5) == [1]
