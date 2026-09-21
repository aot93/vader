"""Hardware abstraction — everything that talks to the physical library.

Two implementations exist:

* :class:`~app.hardware.real.RealHardware` — wraps ``mtx`` / ``mt`` / ``mkltfs``
  / ``ltfs`` via subprocess on the Linux VM that owns the DDA-attached HBA.
* :class:`~app.hardware.simulator.SimulatedHardware` — an in-memory library with
  a directory-backed fake LTFS volume, so the whole application can run, be
  demoed and be tested on a machine with no tape hardware at all.

The rest of the app only ever imports :func:`get_hardware`.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from app.config import get_settings


class HardwareError(RuntimeError):
    """Any failure from the library, a drive, or LTFS."""


class TapeReadError(HardwareError):
    """Raised specifically on a read/verify failure so callers can log it
    against the tape record (framework doc §3.4)."""


@dataclass
class DriveState:
    number: int
    device: str
    loaded_barcode: str | None = None
    loaded_from_slot: int | None = None
    mount_point: str | None = None
    activity: str = "idle"  # idle | reading | writing
    # Physical occupancy, independent of whether a barcode label was
    # readable. `mtx status` reports Empty/Full for an element regardless of
    # VolumeTag — a loaded tape with a blank/unreadable label is still Full,
    # so `loaded_barcode is None` alone cannot tell "empty" from "occupied by
    # an unlabelled tape". Real backend sets this from the Empty/Full token
    # directly; simulated tapes always have a known barcode, so there
    # `occupied == (loaded_barcode is not None)`.
    occupied: bool = False


@dataclass
class SlotState:
    number: int
    barcode: str | None = None
    is_cleaning_tape: bool = False
    kind: str = "storage"  # storage | import_export
    # Same rationale as DriveState.occupied — see there.
    occupied: bool = False


@dataclass
class LibraryState:
    changer_device: str
    slots: list[SlotState] = field(default_factory=list)
    drives: list[DriveState] = field(default_factory=list)

    def slot(self, number: int) -> SlotState | None:
        return next((s for s in self.slots if s.number == number), None)

    def drive(self, number: int) -> DriveState | None:
        return next((d for d in self.drives if d.number == number), None)

    def find_barcode_slot(self, barcode: str) -> int | None:
        for s in self.slots:
            if s.barcode == barcode:
                return s.number
        return None


class TapeHardware(abc.ABC):
    """Contract the job layer depends on. Implementations must be safe to call
    from multiple threads concurrently — the job worker can run several jobs
    on different drives at once. Arm-moving operations (load/unload/clean)
    are additionally serialised app-wide by a shared lock in
    ``app.services.library`` (the physical changer arm can only move one
    tape at a time, regardless of how many drives exist); everything else
    (mkltfs, mount/unmount) only needs to be safe *per drive*, which each
    implementation is responsible for on its own — see
    ``SimulatedHardware._state_lock`` for how the simulator backend does it."""

    backend: str = "abstract"

    @abc.abstractmethod
    def library_status(self) -> LibraryState: ...

    @abc.abstractmethod
    def inventory(self) -> LibraryState:
        """Re-scan barcodes (``mtx inventory``) and return fresh state."""

    @abc.abstractmethod
    def load(self, slot: int, drive: int) -> None: ...

    @abc.abstractmethod
    def unload(self, slot: int, drive: int) -> None: ...

    @abc.abstractmethod
    def mkltfs(self, drive: int, barcode: str, *, force: bool = False) -> None:
        """Format the tape in ``drive`` for LTFS use (``mkltfs``). ``force``
        must reach the real tool's own ``-f``/``--force`` flag — mkltfs
        refuses outright on a medium that already carries an LTFS filesystem,
        which is the normal state of any real cartridge being repurposed."""

    @abc.abstractmethod
    def mount_ltfs(self, drive: int, *, read_only: bool = False) -> Path:
        """Mount the loaded tape and return the mount point. ``read_only``
        must make writing to the mount actually fail, not just be a hint —
        callers (e.g. tape import) rely on it as a hard safety guarantee."""

    @abc.abstractmethod
    def unmount_ltfs(self, drive: int) -> None: ...

    @abc.abstractmethod
    def clean_drive(self, drive: int, cleaning_slot: int) -> None: ...

    def unlock_drive(self, drive: int) -> None:
        """Clear a stuck SCSI PREVENT MEDIUM REMOVAL lock on ``drive``.

        LTFS sets this lock on mount and clears it on a clean unmount; an
        unclean shutdown (process killed mid-write) can leave it set, which
        makes the changer refuse to eject the tape (`mtx unload` fails with
        "Illegal Request" / "Medium removal prevented") until this runs.
        No-op unless overridden — only real hardware has this failure mode."""
        return

    # --- convenience shared by both implementations ---------------------------

    def mount_point_for(self, drive: int) -> Path:
        return Path(get_settings().ltfs_mount_base) / f"drive{drive}"


@lru_cache(maxsize=1)
def get_hardware() -> TapeHardware:
    settings = get_settings()
    if settings.hardware_backend == "real":
        from app.hardware.real import RealHardware

        return RealHardware()
    from app.hardware.simulator import SimulatedHardware

    return SimulatedHardware()
