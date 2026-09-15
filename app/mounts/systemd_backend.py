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
from app.mounts.base import ConnectionSpec, MountBackend, MountError

_SYSTEMCTL_TIMEOUT = 20


def _systemd_escape_path(path: str) -> str:
    """The basename systemd requires for a .mount/.automount unit whose
    Where= is ``path`` — NOT a hand-rolled slug. A unit's filename must be
    the exact systemd-escaped form of its Where= path (only "/" becomes "-";
    a literal "-" or "." or uppercase letter already in the path is kept
    almost as-is, with genuinely special characters \\xHH-escaped) or systemd
    refuses to load it outright ("Where= setting doesn't match unit name").
    ``Connection.unit_name`` (from ``app.mounts.base.unit_name_for``) is a
    naive hostname slug that gets this wrong for almost any real hostname
    (dots, hyphens, mixed case) — shell out to the real tool instead of
    re-deriving its rules by hand a second time and risking the same bug
    again in a new shape.
    """
    try:
        proc = subprocess.run(
            ["systemd-escape", "--path", path], check=True, capture_output=True,
            text=True, timeout=5,
        )
    except FileNotFoundError as exc:
        raise MountError("systemd-escape not found — is this a systemd host?") from exc
    except subprocess.TimeoutExpired as exc:
        raise MountError(f"systemd-escape --path {path} timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise MountError(f"systemd-escape --path {path} failed: {exc.stderr.strip()}") from exc
    return proc.stdout.strip()


def _unit_paths(spec: ConnectionSpec) -> tuple[Path, Path]:
    base = Path(get_settings().smb_systemd_dir)
    name = _systemd_escape_path(spec.mount_path)
    return base / f"{name}.mount", base / f"{name}.automount"


def _mount_unit(spec: ConnectionSpec) -> str:
    mode = "ro" if spec.read_only else "rw"
    return (
        f"[Unit]\nDescription=Mount SMB connection {spec.hostname}\n\n"
        f"[Mount]\nWhat=//{spec.hostname}/{spec.share}\nWhere={spec.mount_path}\n"
        f"Type=cifs\nOptions=credentials={spec.credentials_path},_netdev,nofail,"
        f"vers={spec.smb_version},{mode}\n\n[Install]\nWantedBy=multi-user.target\n"
    )


def _automount_unit(spec: ConnectionSpec) -> str:
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

    def mount_root(self) -> Path:
        return Path(get_settings().smb_mount_base)

    def provision(self, spec: ConnectionSpec, password: str) -> None:
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

    def deprovision(self, spec: ConnectionSpec) -> None:
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
        # behind (an ingest mount is read-only anyway, but a restore
        # destination is not — be careful regardless).
        with contextlib.suppress(OSError):
            Path(spec.mount_path).rmdir()

    def check_health(self, spec: ConnectionSpec) -> tuple[bool, str | None]:
        # A plain, never-mounted directory (created by provision()'s mkdir)
        # lists successfully and even satisfies os.path.ismount() once the
        # automount unit is enabled — the automount trigger itself is a mount
        # (autofs) from the moment it's enabled, regardless of whether the
        # underlying CIFS connection ever actually comes up. So neither
        # listdir() nor ismount() alone can tell "mounted" from "not mounted".
        # listdir() first (to trigger the automount if it hasn't fired yet),
        # then confirm against /proc/mounts that the mount at this exact path
        # really is a cifs filesystem, not just an idle/failed autofs stub.
        try:
            os.listdir(spec.mount_path)
        except OSError as exc:
            return False, str(exc)
        if not _is_cifs_mounted(spec.mount_path):
            return False, f"{spec.mount_path} is not an active CIFS mount"
        return True, None


def _is_cifs_mounted(mount_path: str, mounts_file: str = "/proc/mounts") -> bool:
    target = str(Path(mount_path).resolve())
    try:
        with open(mounts_file) as f:
            for line in f:
                fields = line.split()
                if len(fields) >= 3 and fields[1] == target and fields[2] == "cifs":
                    return True
    except OSError:
        return False
    return False
