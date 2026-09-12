"""Simulated mount backend — no root, no systemd, no real Windows box required.

Lets the Source Manager be added, health-checked, and removed on a plain dev
machine, exercising the same credentials-file security property (mode 600) as
the real backend. Health can be flipped to "down" for testing by dropping a
``.simulate-down`` file into the simulated mount directory.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from app.config import get_settings
from app.mounts.base import MountBackend, SourceSpec


def _sim_root() -> Path:
    return get_settings().data_dir / "sim"


def _sim_mount_dir(spec: SourceSpec) -> Path:
    return _sim_root() / "mounts" / spec.hostname


def _sim_unit_marker(spec: SourceSpec) -> Path:
    return _sim_root() / "systemd" / f"{spec.unit_name}.marker"


class SimulatedMountBackend(MountBackend):
    backend = "simulator"

    def provision(self, spec: SourceSpec, password: str) -> None:
        cred_path = Path(spec.credentials_path)
        cred_path.parent.mkdir(parents=True, exist_ok=True)
        cred_path.write_text(
            f"username={spec.username}\npassword={password}\ndomain={spec.domain or ''}\n"
        )
        cred_path.chmod(0o600)

        mount_dir = _sim_mount_dir(spec)
        mount_dir.mkdir(parents=True, exist_ok=True)
        (mount_dir / ".simulated-mount").write_text(
            f"pretend SMB mount of //{spec.hostname}/{spec.share}\n"
        )

        marker = _sim_unit_marker(spec)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            f"[Mount]\nWhat=//{spec.hostname}/{spec.share}\nWhere={spec.mount_path}\n"
            f"Options=credentials={spec.credentials_path},vers={spec.smb_version},ro\n"
        )

    def deprovision(self, spec: SourceSpec) -> None:
        Path(spec.credentials_path).unlink(missing_ok=True)
        shutil.rmtree(_sim_mount_dir(spec), ignore_errors=True)
        _sim_unit_marker(spec).unlink(missing_ok=True)

    def check_health(self, spec: SourceSpec) -> tuple[bool, str | None]:
        mount_dir = _sim_mount_dir(spec)
        try:
            entries = os.listdir(mount_dir)
        except OSError as exc:
            return False, str(exc)
        if (mount_dir / ".simulate-down").exists():
            return False, "simulated outage (.simulate-down marker present)"
        if ".simulated-mount" not in entries:
            return False, "simulated mount marker missing"
        return True, None
