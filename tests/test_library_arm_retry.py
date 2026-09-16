"""Regression coverage for the bounded retry around the physical arm move in
``app.services.library`` (plan doc: "New finding, post item-3"). A live
write job hit one transient MOVE MEDIUM SCSI error (Illegal Request, sense
53/03) on a post-write unload — retrying the identical ``mtx unload`` by
hand seconds later succeeded immediately, confirming it was transient, not a
persistent fault. ``load_tape``/``unload_tape`` now retry the underlying
``hw.load()``/``hw.unload()`` call once before giving up.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db import session_scope
from app.hardware import HardwareError, get_hardware
from app.models import EventResult, TapeEvent, TapeEventType
from app.services import library as lib


def test_load_tape_recovers_from_one_transient_hardware_error(seeded, monkeypatch):
    hw = get_hardware()
    real_load = hw.load
    calls = {"n": 0}

    def flaky_load(slot, drive):
        calls["n"] += 1
        if calls["n"] == 1:
            raise HardwareError("Illegal Request (sense 53/03)")
        return real_load(slot, drive)

    monkeypatch.setattr(hw, "load", flaky_load)
    monkeypatch.setattr(lib.time, "sleep", lambda *_: None)  # don't actually wait in tests

    with session_scope() as s:
        lib.load_tape(s, 1, 0)

    assert calls["n"] == 2, "load_tape gave up instead of retrying the transient failure"
    assert hw.library_status().drive(0).loaded_barcode is not None

    with session_scope() as s:
        event = s.scalars(
            select(TapeEvent).where(TapeEvent.event_type == TapeEventType.load)
        ).one()
        assert event.result == EventResult.success


def test_load_tape_raises_after_exhausting_retries(seeded, monkeypatch):
    hw = get_hardware()

    def always_fails(slot, drive):
        raise HardwareError("Illegal Request (sense 53/03)")

    monkeypatch.setattr(hw, "load", always_fails)
    monkeypatch.setattr(lib.time, "sleep", lambda *_: None)

    with session_scope() as s:
        with pytest.raises(HardwareError):
            lib.load_tape(s, 1, 0)

    with session_scope() as s:
        event = s.scalars(
            select(TapeEvent).where(TapeEvent.event_type == TapeEventType.load)
        ).one()
        assert event.result == EventResult.error
        assert "53/03" in event.error_detail


def test_unload_tape_recovers_from_one_transient_hardware_error(seeded, monkeypatch):
    hw = get_hardware()
    with session_scope() as s:
        lib.load_tape(s, 1, 0)

    real_unload = hw.unload
    calls = {"n": 0}

    def flaky_unload(slot, drive):
        calls["n"] += 1
        if calls["n"] == 1:
            raise HardwareError("Illegal Request (sense 53/03)")
        return real_unload(slot, drive)

    monkeypatch.setattr(hw, "unload", flaky_unload)
    monkeypatch.setattr(lib.time, "sleep", lambda *_: None)

    with session_scope() as s:
        lib.unload_tape(s, None, 0)

    assert calls["n"] == 2
    assert hw.library_status().drive(0).loaded_barcode is None
