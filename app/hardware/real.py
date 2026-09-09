"""Real hardware backend — thin, auditable subprocess wrappers.

Every command mirrors what an operator would type by hand (framework doc §3.1),
so behaviour stays predictable a year later. Nothing here parses cleverly; it
runs the tool, checks the return code, and surfaces stderr verbatim.

This module is only imported when ``HARDWARE_BACKEND=real``. It is exercised on
the VM during the annual dry run (LTO-Archive-VM-Setup.md §6), not by the test
suite.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from app.config import get_settings
from app.hardware.base import (
    DriveState,
    HardwareError,
    LibraryState,
    SlotState,
    TapeHardware,
    TapeReadError,
)

_READ_ERROR_RE = re.compile(r"(read error|hard error|sense key|Input/output error)", re.I)


class RealHardware(TapeHardware):
    backend = "real"

    def __init__(self) -> None:
        self.settings = get_settings()
        self.changer = self.settings.changer_device
        self.drives = self.settings.drive_devices

    # --- subprocess plumbing ------------------------------------------------

    def _run(self, argv: list[str], *, timeout: int = 3600) -> str:
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, check=False
            )
        except FileNotFoundError as exc:  # tool not installed
            raise HardwareError(f"{argv[0]} not found — is the tape toolchain installed?") from exc
        except subprocess.TimeoutExpired as exc:
            raise HardwareError(f"{' '.join(argv)} timed out after {timeout}s") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            if _READ_ERROR_RE.search(detail):
                raise TapeReadError(detail)
            raise HardwareError(f"`{' '.join(argv)}` failed ({proc.returncode}): {detail}")
        return proc.stdout

    def _drive_device(self, drive: int) -> str:
        try:
            return self.drives[drive]
        except IndexError as exc:
            raise HardwareError(f"no drive device configured for index {drive}") from exc

    # --- mtx status parsing ----------------------------------------------

    def _parse_status(self, text: str) -> LibraryState:
        drives: list[DriveState] = []
        slots: list[SlotState] = []
        for line in text.splitlines():
            line = line.strip()
            m = re.match(r"Data Transfer Element (\d+):(Empty|Full)(.*)", line)
            if m:
                num = int(m.group(1))
                barcode = None
                from_slot = None
                if m.group(2) == "Full":
                    bc = re.search(r"VolumeTag\s*=\s*(\S+)", m.group(3))
                    barcode = bc.group(1).strip() if bc else None
                    fs = re.search(r"\(Storage Element (\d+) Loaded\)", m.group(3))
                    from_slot = int(fs.group(1)) if fs else None
                drives.append(DriveState(
                    number=num, device=self._drive_device(num) if num < len(self.drives) else "?",
                    loaded_barcode=barcode, loaded_from_slot=from_slot,
                ))
                continue
            m = re.match(r"\s*Storage Element (\d+)(\s+IMPORT/EXPORT)?:(Empty|Full)(.*)", line)
            if m:
                num = int(m.group(1))
                is_ie = bool(m.group(2))
                barcode = None
                if m.group(3) == "Full":
                    bc = re.search(r"VolumeTag\s*=\s*(\S+)", m.group(4))
                    barcode = bc.group(1).strip() if bc else None
                slots.append(SlotState(
                    number=num, barcode=barcode or None,
                    is_cleaning_tape=bool(barcode and barcode.upper().startswith("CLN")),
                    kind="import_export" if is_ie else "storage",
                ))
        return LibraryState(changer_device=self.changer, slots=slots, drives=drives)

    # --- TapeHardware API ------------------------------------------------

    def library_status(self) -> LibraryState:
        return self._parse_status(self._run(["mtx", "-f", self.changer, "status"], timeout=120))

    def inventory(self) -> LibraryState:
        self._run(["mtx", "-f", self.changer, "inventory"], timeout=600)
        return self.library_status()

    def load(self, slot: int, drive: int) -> None:
        self._run(["mtx", "-f", self.changer, "load", str(slot), str(drive)], timeout=600)

    def unload(self, slot: int, drive: int) -> None:
        self._run(["mtx", "-f", self.changer, "unload", str(slot), str(drive)], timeout=600)

    def mkltfs(self, drive: int, barcode: str) -> None:
        dev = self._drive_device(drive)
        self._run(["mkltfs", "-d", dev, "-n", barcode], timeout=7200)

    def mount_ltfs(self, drive: int) -> Path:
        dev = self._drive_device(drive)
        mp = self.mount_point_for(drive)
        mp.mkdir(parents=True, exist_ok=True)
        self._run(["ltfs", "-o", f"devname={dev}", str(mp)], timeout=1800)
        return mp

    def unmount_ltfs(self, drive: int) -> None:
        mp = self.mount_point_for(drive)
        self._run(["umount", str(mp)], timeout=1800)

    def clean_drive(self, drive: int, cleaning_slot: int) -> None:
        self.load(cleaning_slot, drive)
        # LTO drives run the clean cycle automatically when a cleaning cartridge
        # is inserted; give it time, then put the cartridge back.
        self._run(["mt", "-f", self._drive_device(drive), "status"], timeout=600)
        self.unload(cleaning_slot, drive)

    def drive_raw_status(self, drive: int) -> str:
        return self._run(["mt", "-f", self._drive_device(drive), "status"], timeout=120)
