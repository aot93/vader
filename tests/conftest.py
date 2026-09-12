"""Test fixtures.

Environment is pinned to a throwaway directory + SQLite file *before* any app
module is imported, so ``get_settings()`` (cached) picks it up.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="vader-test-"))
os.environ["VADER_ENV_FILE"] = "/dev/null"
os.environ["DATA_DIR"] = str(_TMP / "data")
os.environ["BACKUP_DIR"] = str(_TMP / "backups")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'vader.sqlite3'}"
os.environ["HARDWARE_BACKEND"] = "simulator"
os.environ["SIM_STORAGE_SLOTS"] = "8"
os.environ["SIM_DRIVES"] = "2"
os.environ["SIM_TAPE_CAPACITY_BYTES"] = str(2_000_000)  # 2 MB tapes -> spanning is cheap to test
os.environ["SIM_SEED_TAPES"] = "true"
os.environ["AUTH_TOKEN"] = ""
os.environ["WRITE_READBACK_VERIFY"] = "true"

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.hardware import get_hardware  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_state():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    hw = get_hardware()
    if hasattr(hw, "reset"):
        hw.reset()
    settings = get_settings()
    for sub in ("manifests", "exports", "restores", "smb_credentials", "sim/mounts", "sim/systemd"):
        p = settings.data_dir / sub
        if p.exists():
            shutil.rmtree(p)
    settings.ensure_dirs()
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def settings():
    return get_settings()


def _seed_scratch_tapes(session):
    from sqlalchemy import select

    from app.models import Tape, TapeStatus

    hw = get_hardware()
    state = hw.library_status()
    known = {t.barcode for t in session.scalars(select(Tape)).all()}
    for slot in state.slots:
        if not slot.barcode or slot.is_cleaning_tape or slot.barcode in known:
            continue
        session.add(Tape(
            barcode=slot.barcode, lto_generation="LTO-8",
            capacity_native_bytes=get_settings().sim_tape_capacity_bytes,
            status=TapeStatus.scratch, physical_location=f"slot {slot.number}",
        ))
    session.commit()


@pytest.fixture
def seeded(db):
    _seed_scratch_tapes(db)
    return db


@pytest.fixture
def make_source(tmp_path):
    """Build a small archive-shaped source tree and return its path."""

    def _make(name: str = "Project-Foo", *, frames: int = 6, frame_size: int = 20_000,
              chunks: int = 3, chunk_size: int = 30_000, with_config: bool = True,
              root: Path | None = None):
        root = root if root is not None else tmp_path / name
        shot = root / "seq010" / "shot0100"
        shot.mkdir(parents=True)
        for i in range(1, frames + 1):
            (shot / f"shot0100.{i:04d}.exr").write_bytes(bytes([i % 256]) * frame_size)
        vids = root / "video"
        vids.mkdir(parents=True)
        for i in range(1, chunks + 1):
            (vids / f"reel_a.{i:03d}.mov").write_bytes(bytes([(i * 7) % 256]) * chunk_size)
        if with_config:
            cfg = root / "config"
            cfg.mkdir()
            (cfg / "render.json").write_bytes(b'{"fps": 24}\n' * 10)
            (root / "notes.txt").write_bytes(b"hello\n" * 50)
        return root

    return _make
