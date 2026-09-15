"""Concurrency safety of SimulatedHardware's internal state.

Regression coverage for the state-mutation race found while scoping
concurrent job execution (plan item 3): SimulatedHardware is a process-wide
singleton with one mutable ``self._state`` dict, persisted to
``sim/library.json`` on every mutating call, with no locking before this
fix — safe only because a single worker thread serialized every hardware
call. Once different jobs can run on different drives at the same time,
concurrent load/unload/mkltfs/mount calls need to not corrupt that shared
state or its on-disk snapshot, and the lock protecting it must be
reentrant: ``unload()`` calls ``self.unmount_ltfs()`` internally, and
``clean_drive()`` calls ``self.load()``/``self.unload()`` internally — a
plain (non-reentrant) ``Lock`` would deadlock the instant either of those
nested calls tried to re-acquire it on the same thread.
"""
from __future__ import annotations

import json
import threading
import time

from app.hardware import get_hardware


def _all_barcodes(state) -> list[str]:
    out = [s.barcode for s in state.slots if s.barcode]
    out += [d.loaded_barcode for d in state.drives if d.loaded_barcode]
    return out


def test_state_lock_actually_blocks_a_concurrent_mutator(seeded):
    """Deterministic proof the lock is real, not just present: hold it on
    the main thread, start a mutator on another thread, and confirm that
    thread cannot proceed until the lock is released."""
    hw = get_hardware()
    hw._state_lock.acquire()
    finished = threading.Event()

    def do_load():
        hw.load(1, 0)
        finished.set()

    t = threading.Thread(target=do_load, daemon=True)
    t.start()
    time.sleep(0.1)  # give the thread every chance to (wrongly) proceed
    assert not finished.is_set(), "load() proceeded while the state lock was held elsewhere"

    hw._state_lock.release()
    t.join(timeout=1)
    assert finished.is_set()
    assert hw.library_status().drive(0).loaded_barcode is not None


def test_unload_while_still_mounted_does_not_deadlock(seeded):
    """unload() calls self.unmount_ltfs() internally when the drive is
    still mounted — proves the lock is reentrant (RLock, not Lock)."""
    hw = get_hardware()
    hw.load(1, 0)
    hw.mkltfs(0, hw.library_status().drive(0).loaded_barcode, force=True)
    hw.mount_ltfs(0)
    assert hw.library_status().drive(0).mount_point is not None

    # daemon=True: if this ever regresses to a real deadlock, the assertion
    # below fails cleanly instead of hanging the whole pytest process at
    # interpreter exit waiting to join a thread stuck forever.
    t = threading.Thread(target=hw.unload, args=(1, 0), daemon=True)
    t.start()
    t.join(timeout=2)
    assert not t.is_alive(), "unload() deadlocked re-acquiring its own lock via unmount_ltfs()"

    d = hw.library_status().drive(0)
    assert d.loaded_barcode is None and d.mount_point is None


def test_clean_drive_does_not_deadlock(seeded):
    """clean_drive() calls self.load() then self.unload() internally —
    same reentrancy requirement as above, via a different call path."""
    hw = get_hardware()
    state = hw.library_status()
    cleaning_slot = next(s.number for s in state.slots if s.is_cleaning_tape)

    t = threading.Thread(target=hw.clean_drive, args=(0, cleaning_slot), daemon=True)
    t.start()
    t.join(timeout=2)
    assert not t.is_alive(), "clean_drive() deadlocked re-acquiring its own lock"

    d = hw.library_status().drive(0)
    assert d.loaded_barcode is None  # cleaning cartridge returned to its slot


def test_concurrent_cycles_on_different_drives_stay_consistent(seeded):
    """Broader sanity check: many interleaved load/format/mount/unmount/
    unload cycles on two independent drives, run concurrently, end in a
    fully consistent state with no duplicated or lost barcode and a valid
    (non-corrupted) persisted JSON snapshot."""
    hw = get_hardware()
    before = hw.library_status()
    expected_barcodes = sorted(_all_barcodes(before))

    pairs = [(1, 0), (2, 1)]
    errors: list[Exception] = []

    def cycle(slot: int, drive: int, n: int) -> None:
        try:
            for _ in range(n):
                hw.load(slot, drive)
                hw.mkltfs(drive, hw.library_status().drive(drive).loaded_barcode, force=True)
                hw.mount_ltfs(drive)
                hw.unmount_ltfs(drive)
                hw.unload(slot, drive)
        except Exception as exc:  # noqa: BLE001 - surfaced via `errors` after join
            errors.append(exc)

    threads = [threading.Thread(target=cycle, args=(slot, drive, 20)) for slot, drive in pairs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors

    after = hw.library_status()
    assert sorted(_all_barcodes(after)) == expected_barcodes
    for _slot, drive in pairs:
        d = after.drive(drive)
        assert d.loaded_barcode is None and d.mount_point is None

    on_disk = json.loads(hw.state_file.read_text())  # raises if the JSON was ever torn
    persisted_barcodes = sorted(
        [s["barcode"] for s in on_disk["slots"] if s["barcode"]]
        + [d["loaded_barcode"] for d in on_disk["drives"] if d["loaded_barcode"]]
    )
    assert persisted_barcodes == expected_barcodes
