"""Simulated mount backend — no root, no systemd, no real Windows box required.

Lets connections be added, health-checked, and removed on a plain dev
machine, exercising the same credentials-file security property (mode 600) as
the real backend. Health can be flipped to "down" for testing by dropping a
``.simulate-down`` file into the simulated mount directory.

Mounts land under ``DATA_DIR/sim/mounts/<mount_dir_name>`` rather than
``smb_mount_base`` — :meth:`mount_root` is how ``connection_manager`` finds
out that, so ``Connection.mount_path`` always points at wherever this backend
actually put things.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from app.config import get_settings
from app.mounts.base import ConnectionSpec, MountBackend


class SimulatedMountBackend(MountBackend):
    backend = "simulator"

    def mount_root(self) -> Path:
        return get_settings().data_dir / "sim" / "mounts"

    def _unit_marker(self, spec: ConnectionSpec) -> Path:
        return get_settings().data_dir / "sim" / "systemd" / f"{spec.unit_name}.marker"

    def provision(self, spec: ConnectionSpec, password: str) -> None:
        # Credentials file: real file, real chmod 600 — worth exercising even
        # in simulation since the security property matters most here.
        cred_path = Path(spec.credentials_path)
        cred_path.parent.mkdir(parents=True, exist_ok=True)
        cred_path.write_text(
            f"username={spec.username}\npassword={password}\ndomain={spec.domain or ''}\n"
        )
        cred_path.chmod(0o600)

        mount_dir = Path(spec.mount_path)
        mount_dir.mkdir(parents=True, exist_ok=True)
        (mount_dir / ".simulated-mount").write_text(
            f"pretend SMB mount of //{spec.hostname}/{spec.share}\n"
        )

        marker = self._unit_marker(spec)
        marker.parent.mkdir(parents=True, exist_ok=True)
        mode = "ro" if spec.read_only else "rw"
        marker.write_text(
            f"[Mount]\nWhat=//{spec.hostname}/{spec.share}\nWhere={spec.mount_path}\n"
            f"Options=credentials={spec.credentials_path},vers={spec.smb_version},{mode}\n"
        )

    def deprovision(self, spec: ConnectionSpec) -> None:
        Path(spec.credentials_path).unlink(missing_ok=True)
        shutil.rmtree(Path(spec.mount_path), ignore_errors=True)
        self._unit_marker(spec).unlink(missing_ok=True)

    def check_health(self, spec: ConnectionSpec) -> tuple[bool, str | None]:
        mount_dir = Path(spec.mount_path)
        try:
            entries = os.listdir(mount_dir)
        except OSError as exc:
            return False, str(exc)
        if (mount_dir / ".simulate-down").exists():
            return False, "simulated outage (.simulate-down marker present)"
        if ".simulated-mount" not in entries:
            return False, "simulated mount marker missing"
        return True, None
