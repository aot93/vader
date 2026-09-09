"""Central configuration, loaded once from the environment / .env file.

Kept deliberately small and dependency-light so the app still starts cleanly
after sitting idle for the best part of a year (see framework doc §6).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader — avoids a hard dependency for a one-file need."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(Path(os.environ.get("VADER_ENV_FILE", ".env")))


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    database_url: str = os.environ.get("DATABASE_URL", "sqlite:///./data/vader.sqlite3")

    hardware_backend: str = os.environ.get("HARDWARE_BACKEND", "simulator")
    changer_device: str = os.environ.get("CHANGER_DEVICE", "/dev/sg3")
    drive_devices: list[str] = field(
        default_factory=lambda: _list("DRIVE_DEVICES", ["/dev/nst0", "/dev/nst1"])
    )
    ltfs_mount_base: str = os.environ.get("LTFS_MOUNT_BASE", "/mnt/ltfs")

    sim_storage_slots: int = _int("SIM_STORAGE_SLOTS", 24)
    sim_drives: int = _int("SIM_DRIVES", 2)
    sim_tape_capacity_bytes: int = _int("SIM_TAPE_CAPACITY_BYTES", 12_000_000_000)
    sim_seed_tapes: bool = _bool("SIM_SEED_TAPES", True)

    data_dir: Path = Path(os.environ.get("DATA_DIR", "./data")).resolve()
    backup_dir: Path = Path(os.environ.get("BACKUP_DIR", "./data/backups")).resolve()

    reverify_months: int = _int("REVERIFY_MONTHS", 12)
    default_verify_sample_fraction: float = _float("DEFAULT_VERIFY_SAMPLE_FRACTION", 0.1)
    write_readback_verify: bool = _bool("WRITE_READBACK_VERIFY", True)

    auth_token: str = os.environ.get("AUTH_TOKEN", "").strip()
    session_secret: str = os.environ.get("SESSION_SECRET", "change-me-please")

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_token)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "manifests").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "exports").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "sim").mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
