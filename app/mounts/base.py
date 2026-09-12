"""Mount-provisioning abstraction for SMB connections — ingest sources
(Vader SMB Source Manager design spec v1.0) and, later, restore destinations.

Two implementations, mirroring the ``app/hardware`` split:

* :class:`~app.mounts.systemd_backend.SystemdMountBackend` — writes a
  credentials file, a mount dir, and a pair of systemd ``.mount``/
  ``.automount`` units, then drives them with ``systemctl`` on a real Linux
  host. Requires the process to be able to write ``/etc/systemd/system`` and
  run ``systemctl`` (root, or a scoped sudoers rule).
* :class:`~app.mounts.simulator.SimulatedMountBackend` — the same file layout
  under ``DATA_DIR/sim/`` instead of ``/etc`` + ``/mnt``, no ``systemctl``
  calls, so the Connection Manager can be demoed, tested, and developed on a
  machine with no root and no real Windows boxes — same rationale as
  ``app.hardware``'s simulator.

The rest of the app only ever imports :func:`get_mount_backend`.
"""
from __future__ import annotations

import abc
import re
from dataclasses import dataclass
from functools import lru_cache

from app.config import get_settings

# Deliberately conservative allowlists. These values end up written verbatim
# into systemd unit files and a credentials file that a real backend runs as
# root — a newline or "[" in an operator-supplied field must never be able to
# inject an extra unit directive or credentials line.
_HOSTNAME_RE = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
_SIMPLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._$-]{0,127}$")


class MountError(RuntimeError):
    """Any failure provisioning, removing, or checking a connection's mount."""


def sanitize_hostname(value: str) -> str:
    value = (value or "").strip()
    if not value or not _HOSTNAME_RE.match(value):
        raise ValueError(f"invalid hostname/IP: {value!r}")
    return value


def sanitize_share(value: str) -> str:
    value = (value or "").strip()
    if not value or not _SIMPLE_RE.match(value):
        raise ValueError(f"invalid share name: {value!r}")
    return value


def sanitize_simple(value: str, field: str) -> str:
    """Username / domain — same conservative charset as a share name, but
    optional (returns "" for a blank domain)."""
    value = (value or "").strip()
    if value and not _SIMPLE_RE.match(value):
        raise ValueError(f"invalid {field}: {value!r}")
    return value


def unit_name_for(hostname: str, purpose: str = "ingest") -> str:
    base = "mnt-vader-" + re.sub(r"[^A-Za-z0-9]+", "-", hostname).strip("-").lower()
    # Ingest keeps its original, suffix-less name — real hosts may already
    # have a systemd unit under this exact name from before restore
    # destinations existed, and renaming it out from under an existing mount
    # would orphan it. Restore destinations are always a distinct connection.
    return base if purpose == "ingest" else f"{base}-restore"


def mount_dir_name(hostname: str, purpose: str = "ingest") -> str:
    """Same rationale as :func:`unit_name_for`: ingest's directory name is
    unchanged so an existing real mount isn't relocated; a restore
    destination for the same hostname gets its own directory."""
    return hostname if purpose == "ingest" else f"{hostname}-restore"


@dataclass(frozen=True)
class ConnectionSpec:
    """Everything a backend needs to provision/check one connection. Carries
    the password only in memory, in transit from the create form to
    :meth:`MountBackend.provision` — it is never persisted on the
    :class:`~app.models.Connection` row."""

    hostname: str
    share: str
    mount_path: str
    credentials_path: str
    unit_name: str
    smb_version: str
    domain: str | None
    username: str
    read_only: bool = True


class MountBackend(abc.ABC):
    backend: str = "abstract"

    @abc.abstractmethod
    def mount_root(self):
        """Base directory this backend actually mounts connections under —
        ``<root>/<mount_dir_name(hostname, purpose)>`` is the real, listable
        path for a given connection. The simulator's root is *not*
        ``smb_mount_base``; callers must ask the backend rather than assume a
        fixed setting, or the path stored on the :class:`~app.models.Connection`
        row can point somewhere the backend never touches."""

    @abc.abstractmethod
    def provision(self, spec: ConnectionSpec, password: str) -> None:
        """Create the credentials file, mount dir and automount unit, then
        enable + start it. Must be safe to call twice (re-running creation
        should not break — design spec "Implementation Notes")."""

    @abc.abstractmethod
    def deprovision(self, spec: ConnectionSpec) -> None:
        """Undo everything ``provision`` did. Must not raise if some of it was
        already missing, so a partially-provisioned connection can still be
        cleanly removed."""

    @abc.abstractmethod
    def check_health(self, spec: ConnectionSpec) -> tuple[bool, str | None]:
        """Return ``(healthy, error_detail)``."""


@lru_cache(maxsize=1)
def get_mount_backend() -> MountBackend:
    settings = get_settings()
    if settings.smb_backend == "real":
        from app.mounts.systemd_backend import SystemdMountBackend

        return SystemdMountBackend()
    from app.mounts.simulator import SimulatedMountBackend

    return SimulatedMountBackend()
