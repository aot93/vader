"""Real backend: a CIFS credentials file plus a pair of systemd units
(``.mount`` + ``.automount``), driven with ``systemctl``.

Requires the process to be able to write ``smb_systemd_dir`` (default
``/etc/systemd/system``) and run ``systemctl`` — i.e. root, or a sudoers rule
scoped to this app. Every filesystem/subprocess failure is wrapped in
:class:`MountError` so the router can show it instead of a 500.
"""
from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path

from app.config import get_settings
from app.mounts.base import MountBackend, MountError, SourceSpec

_SYSTEMCTL_TIMEOUT = 20


def _unit_paths(spec: SourceSpec) -> tuple[Path, Path]:
    base = Path(get_settings().smb_systemd_dir)
    return base / f"{spec.unit_name}.mount", base / f"{spec.unit_name}.automount"


def _mount_unit(spec: SourceSpec) -> str:
    return (
        f"[Unit]\nDescription=Mount SMB source {spec.hostname}\n\n"
        f"[Mount]\nWhat=//{spec.hostname}/{spec.share}\nWhere={spec.mount_path}\n"
        f"Type=cifs\nOptions=credentials={spec.credentials_path},_netdev,nofail,"
        f"vers={spec.smb_version},ro\n\n[Install]\nWantedBy=multi-user.target\n"
    )


def _automount_unit(spec: SourceSpec) -> str:
    return (
        f"[Unit]\nDescription=Automount for SMB source {spec.hostname}\n\n"
        f"[Automount]\nWhere={spec.mount_path}\n\n"
        f"[Install]\nWantedBy=multi-user.target\n"
    )


def _systemctl(*args: str) -> None:
    try:
        subprocess.run(
            ["systemctl", *args], check=True, capture_output=True, text=True,
            timeout=_SYSTEMCTL_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise MountError("systemctl not found — is this a systemd host?") from exc
    except subprocess.TimeoutExpired as exc:
        raise MountError(f"systemctl {' '.join(args)} timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise MountError(f"systemctl {' '.join(args)} failed: {exc.stderr.strip()}") from exc


class SystemdMountBackend(MountBackend):
    backend = "real"

    def provision(self, spec: SourceSpec, password: str) -> None:
        cred_path = Path(spec.credentials_path)
        try:
            cred_path.parent.mkdir(parents=True, exist_ok=True)
            cred_path.write_text(
                f"username={spec.username}\npassword={password}\ndomain={spec.domain or ''}\n"
            )
            cred_path.chmod(0o600)

            Path(spec.mount_path).mkdir(parents=True, exist_ok=True)

            mount_unit, automount_unit = _unit_paths(spec)
            mount_unit.write_text(_mount_unit(spec))
            automount_unit.write_text(_automount_unit(spec))
        except OSError as exc:
            raise MountError(f"failed writing mount configuration: {exc}") from exc

        _systemctl("daemon-reload")
        _systemctl("enable", "--now", automount_unit.name)

    def deprovision(self, spec: SourceSpec) -> None:
        mount_unit, automount_unit = _unit_paths(spec)
        for unit in (automount_unit, mount_unit):
            if unit.exists():
                with contextlib.suppress(MountError):
                    _systemctl("disable", "--now", unit.name)
        for unit in (automount_unit, mount_unit):
            unit.unlink(missing_ok=True)
        with contextlib.suppress(MountError):
            _systemctl("daemon-reload")
        Path(spec.credentials_path).unlink(missing_ok=True)
        # Only removed if empty — never recurse-delete whatever the share left
        # behind (it is a read-only ingest mount, but be careful regardless).
        with contextlib.suppress(OSError):
            Path(spec.mount_path).rmdir()

    def check_health(self, spec: SourceSpec) -> tuple[bool, str | None]:
        try:
            os.listdir(spec.mount_path)
            return True, None
        except OSError as exc:
            return False, str(exc)
