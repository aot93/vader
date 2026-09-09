"""In-memory tape library with a directory-backed fake LTFS volume.

State is persisted to ``$DATA_DIR/sim/library.json`` so it survives an app
restart (the real library obviously keeps its own state across restarts too).
Each "tape" is a directory under ``$DATA_DIR/sim/volumes/<barcode>/`` — mounting
LTFS just exposes that directory at the drive's mount point via a symlink.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from app.config import get_settings
from app.hardware.base import (
    DriveState,
    HardwareError,
    LibraryState,
    SlotState,
    TapeHardware,
)


class SimulatedHardware(TapeHardware):
    backend = "simulator"

    def __init__(self) -> None:
        self.settings = get_settings()
        self.root = self.settings.data_dir / "sim"
        self.volumes = self.root / "volumes"
        self.volumes.mkdir(parents=True, exist_ok=True)
        self.state_file = self.root / "library.json"
        self.mounts = self.root / "mounts"
        self.mounts.mkdir(parents=True, exist_ok=True)
        self._state = self._load_state()

    def mount_point_for(self, drive: int):  # override: keep sim mounts under DATA_DIR
        return self.mounts / f"drive{drive}"

    # --- persistence -------------------------------------------------------

    def _load_state(self) -> dict:
        if self.state_file.is_file():
            return json.loads(self.state_file.read_text())
        state = self._seed_state()
        self._save(state)
        return state

    def _seed_state(self) -> dict:
        s = self.settings
        slots = []
        for i in range(1, s.sim_storage_slots + 1):
            barcode = f"TEST{i:03d}L8" if s.sim_seed_tapes else None
            slots.append({"number": i, "barcode": barcode, "is_cleaning_tape": False, "kind": "storage"})
        # one cleaning cartridge in the last slot
        if s.sim_seed_tapes and slots:
            slots[-1]["barcode"] = "CLN001L1"
            slots[-1]["is_cleaning_tape"] = True
        drives = [
            {"number": d, "loaded_barcode": None, "loaded_from_slot": None,
             "mounted": False, "activity": "idle"}
            for d in range(s.sim_drives)
        ]
        return {"slots": slots, "drives": drives}

    def _save(self, state: dict | None = None) -> None:
        self.state_file.write_text(json.dumps(state or self._state, indent=2))

    # --- helpers ---------------------------------------------------------

    def _drive(self, number: int) -> dict:
        for d in self._state["drives"]:
            if d["number"] == number:
                return d
        raise HardwareError(f"no such drive: {number}")

    def _slot(self, number: int) -> dict:
        for sl in self._state["slots"]:
            if sl["number"] == number:
                return sl
        raise HardwareError(f"no such slot: {number}")

    def _volume_dir(self, barcode: str) -> Path:
        return self.volumes / barcode

    # --- TapeHardware API ------------------------------------------------

    def library_status(self) -> LibraryState:
        slots = [
            SlotState(number=s["number"], barcode=s["barcode"],
                      is_cleaning_tape=s["is_cleaning_tape"], kind=s.get("kind", "storage"))
            for s in self._state["slots"]
        ]
        drives = []
        for d in self._state["drives"]:
            mp = self.mount_point_for(d["number"]) if d["mounted"] else None
            drives.append(DriveState(
                number=d["number"], device=f"sim:/dev/nst{d['number']}",
                loaded_barcode=d["loaded_barcode"], loaded_from_slot=d["loaded_from_slot"],
                mount_point=str(mp) if mp else None, activity=d["activity"],
            ))
        return LibraryState(changer_device="sim:/dev/sg-changer", slots=slots, drives=drives)

    def inventory(self) -> LibraryState:
        # Nothing changes outside the app in the sim, but re-persist to mimic a scan.
        self._save()
        return self.library_status()

    def load(self, slot: int, drive: int) -> None:
        sl = self._slot(slot)
        dr = self._drive(drive)
        if dr["loaded_barcode"] is not None:
            raise HardwareError(f"drive {drive} already holds {dr['loaded_barcode']}")
        if sl["barcode"] is None:
            raise HardwareError(f"slot {slot} is empty")
        dr["loaded_barcode"] = sl["barcode"]
        dr["loaded_from_slot"] = slot
        sl["barcode"] = None
        sl["is_cleaning_tape"] = False
        self._save()

    def unload(self, slot: int, drive: int) -> None:
        dr = self._drive(drive)
        if dr["loaded_barcode"] is None:
            raise HardwareError(f"drive {drive} is empty")
        if dr["mounted"]:
            self.unmount_ltfs(drive)
        target = self._slot(slot)
        if target["barcode"] is not None:
            raise HardwareError(f"slot {slot} is occupied by {target['barcode']}")
        target["barcode"] = dr["loaded_barcode"]
        target["is_cleaning_tape"] = dr["loaded_barcode"].startswith("CLN")
        dr["loaded_barcode"] = None
        dr["loaded_from_slot"] = None
        dr["activity"] = "idle"
        self._save()

    def mkltfs(self, drive: int, barcode: str) -> None:
        dr = self._drive(drive)
        if dr["loaded_barcode"] != barcode:
            raise HardwareError(
                f"drive {drive} holds {dr['loaded_barcode']}, refusing to format {barcode}"
            )
        vol = self._volume_dir(barcode)
        if vol.exists():
            shutil.rmtree(vol)
        vol.mkdir(parents=True)
        (vol / ".ltfs_label").write_text(f"barcode={barcode}\n")

    def mount_ltfs(self, drive: int) -> Path:
        dr = self._drive(drive)
        barcode = dr["loaded_barcode"]
        if barcode is None:
            raise HardwareError(f"drive {drive} is empty")
        vol = self._volume_dir(barcode)
        if not vol.exists():
            # An unformatted tape: LTFS would refuse to mount.
            raise HardwareError(f"tape {barcode} is not LTFS-formatted (run Format first)")
        mp = self.mount_point_for(drive)
        mp.parent.mkdir(parents=True, exist_ok=True)
        if mp.is_symlink() or mp.exists():
            if mp.is_symlink():
                mp.unlink()
            else:
                shutil.rmtree(mp)
        os.symlink(vol, mp)
        dr["mounted"] = True
        self._save()
        return mp

    def unmount_ltfs(self, drive: int) -> None:
        dr = self._drive(drive)
        mp = self.mount_point_for(drive)
        if mp.is_symlink():
            mp.unlink()
        dr["mounted"] = False
        dr["activity"] = "idle"
        self._save()

    def clean_drive(self, drive: int, cleaning_slot: int) -> None:
        sl = self._slot(cleaning_slot)
        if not sl["is_cleaning_tape"]:
            raise HardwareError(f"slot {cleaning_slot} does not hold a cleaning cartridge")
        self.load(cleaning_slot, drive)
        # a real clean cycle runs here
        self.unload(cleaning_slot, drive)

    # --- test / seed support -----------------------------------------------

    def reset(self) -> None:
        if self.volumes.exists():
            shutil.rmtree(self.volumes)
        self.volumes.mkdir(parents=True)
        self._state = self._seed_state()
        self._save()
