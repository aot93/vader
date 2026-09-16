"""The physical changer arm can only move one tape at a time, so every
action that moves it — load/unload/clean, regardless of which job type or
which drive — must serialize on one shared lock.

Regression coverage for a latent bug found while scoping concurrent job
execution (plan item 3): ``batch_format.py`` and ``tape_import.py`` used to
each keep their own private ``_arm_lock`` — two separate ``Lock`` objects
that never actually serialized each other. That only ever went unnoticed
because the job worker ran one job at a time.

These tests deterministically prove the lock is real and shared, rather
than racing on wall-clock timing: an earlier version of this test tried to
time concurrent ``load``/``unload`` calls and look for overlap, but that
was confounded by SQLite's own write serialization inside
``session_scope()`` giving a false pass even with the lock removed —
holding ``library._arm_lock`` directly and checking a concurrent caller
blocks is the reliable way to prove this.

Every test below MUST release ``lib._arm_lock`` via ``try/finally`` even if
an assertion fails — this is a module-level singleton lock shared across
the whole test session, so a bare ``acquire()`` left held by a failing
assertion would hang every *other* test that touches it too, not just this
one (confirmed while writing these: an earlier draft without the
try/finally turned one intentional regression into a full suite hang).
"""
from __future__ import annotations

import threading
import time

from app.db import session_scope
from app.hardware import get_hardware
from app.services import library as lib


def test_arm_lock_blocks_a_concurrent_load_tape(seeded):
    lib._arm_lock.acquire()
    try:
        finished = threading.Event()

        def do_load():
            with session_scope() as s:
                lib.load_tape(s, 1, 0)
            finished.set()

        t = threading.Thread(target=do_load, daemon=True)
        t.start()
        time.sleep(0.1)
        assert not finished.is_set(), "load_tape proceeded while the arm lock was held elsewhere"
    finally:
        lib._arm_lock.release()

    t.join(timeout=2)
    assert finished.is_set()


def test_arm_lock_is_shared_by_unload_tape_and_clean_drive(seeded):
    """The exact thing the old two-separate-_arm_lock bug got wrong: proves
    load_tape/unload_tape/clean_drive all block on the *same* lock object,
    not independent locks that never actually contend."""

    def blocked_call(fn) -> threading.Thread:
        finished = threading.Event()

        def run():
            with session_scope() as s:
                try:
                    fn(s)
                except Exception:  # noqa: BLE001 - args are nonsense, only blocking matters
                    pass
            finished.set()

        t = threading.Thread(target=run, daemon=True)
        t.finished = finished  # type: ignore[attr-defined]
        t.start()
        return t

    lib._arm_lock.acquire()
    try:
        t_unload = blocked_call(lambda s: lib.unload_tape(s, 99, 0))
        t_clean = blocked_call(lambda s: lib.clean_drive(s, 0, 99))
        time.sleep(0.1)
        assert not t_unload.finished.is_set()  # type: ignore[attr-defined]
        assert not t_clean.finished.is_set()  # type: ignore[attr-defined]
    finally:
        lib._arm_lock.release()

    t_unload.join(timeout=2)
    t_clean.join(timeout=2)
    assert t_unload.finished.is_set()  # type: ignore[attr-defined]
    assert t_clean.finished.is_set()  # type: ignore[attr-defined]


def test_concurrent_unload_tape_any_free_slot_never_collides(seeded):
    """Direct regression test for a real bug found while building this
    file: unload_tape(slot=None) resolves "any free slot" *inside* the arm
    lock now — it used to be resolved by each caller (batch_format.py,
    tape_import.py, writer.py, verification.py, restore.py all did their
    own `next(s for s in ... if s.barcode is None)` lookup) *before* calling
    unload_tape, which raced: two drives unloading at the same moment could
    both see the same slot as free and both target it, one winning and the
    other failing with "slot occupied". Caught for real by
    tests/test_pipeline.py::test_batch_format_cycles_tapes_through_free_drives
    becoming intermittently flaky once two drives could actually unload
    concurrently."""
    hw = get_hardware()
    # load two different tapes onto the two simulated drives first
    with session_scope() as s:
        lib.load_tape(s, 1, 0)
    with session_scope() as s:
        lib.load_tape(s, 2, 1)
    assert hw.library_status().drive(0).loaded_barcode is not None
    assert hw.library_status().drive(1).loaded_barcode is not None

    errors: list[Exception] = []

    def unload(drive: int) -> None:
        try:
            with session_scope() as s:
                lib.unload_tape(s, None, drive)
        except Exception as exc:  # noqa: BLE001 - surfaced via `errors` after join
            errors.append(exc)

    threads = [threading.Thread(target=unload, args=(d,)) for d in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors, errors
    state = hw.library_status()
    assert state.drive(0).loaded_barcode is None
    assert state.drive(1).loaded_barcode is None
    # both tapes landed in *different* slots, not the same one
    occupied = [s.number for s in state.slots if s.barcode is not None]
    assert len(occupied) == len(set(occupied))


def test_unload_tape_skips_a_slot_with_an_unreadable_barcode_label(seeded, monkeypatch):
    """Direct regression test for a bug caught live: `mtx status` reports
    Empty/Full per storage element independent of whether the barcode label
    was readable — a slot holding a tape with a blank/unreadable barcode
    still shows no VolumeTag, which is indistinguishable from a genuinely
    empty slot if occupancy is inferred from barcode alone.
    unload_tape(slot=None)'s "any free slot" search used to do exactly that
    (`s.barcode is None`), picked such a slot, and failed for real against
    the physical changer with "Storage Element N is Already Full" — leaving
    the tape stuck in the drive and every job after it cascading into
    "Drive Full" failures. It must now use `occupied` instead, which the
    real backend derives straight from mtx's Empty/Full token."""
    hw = get_hardware()
    real_status = hw.library_status

    # Free slots 1 and 2 by loading their tapes into the two sim drives.
    with session_scope() as s:
        lib.load_tape(s, 1, 0)
    with session_scope() as s:
        lib.load_tape(s, 2, 1)

    def fake_status():
        # Simulate slot 1's tape label being unreadable: still physically
        # occupied (mtx would report it Full), but no VolumeTag — exactly
        # what a genuinely empty slot also looks like if you only check
        # `barcode is None`.
        state = real_status()
        slot_1 = state.slot(1)
        slot_1.barcode = None
        slot_1.occupied = True
        return state

    monkeypatch.setattr(hw, "library_status", fake_status)

    with session_scope() as s:
        lib.unload_tape(s, None, 0)

    landed_slot = real_status().find_barcode_slot("TEST001L8")
    assert landed_slot == 2, (
        f"expected the drive-0 tape back in the only genuinely free slot (2), "
        f"landed in {landed_slot} instead"
    )
