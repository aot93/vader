"""``_default_capacity_bytes`` must never let the simulator's deliberately
tiny SIM_TAPE_CAPACITY_BYTES knob leak into a real-hardware capacity guess —
that bug made every freshly-registered real tape look full after ~12 GB."""
from __future__ import annotations

from app.config import Settings
from app.services.writer import _default_capacity_bytes


def test_real_backend_uses_default_tape_capacity_bytes():
    settings = Settings(hardware_backend="real", default_tape_capacity_bytes=12_000_000_000_000,
                         sim_tape_capacity_bytes=2_000_000)
    assert _default_capacity_bytes(settings) == 12_000_000_000_000


def test_simulator_backend_still_uses_sim_tape_capacity_bytes():
    settings = Settings(hardware_backend="simulator", default_tape_capacity_bytes=12_000_000_000_000,
                         sim_tape_capacity_bytes=2_000_000)
    assert _default_capacity_bytes(settings) == 2_000_000
