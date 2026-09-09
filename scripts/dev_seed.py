"""Populate a local simulator instance with a believable archive so the UI has
something to show. Safe to run repeatedly (idempotent write jobs).

    HARDWARE_BACKEND=simulator .venv/bin/python scripts/dev_seed.py
"""
from __future__ import annotations

import os
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("HARDWARE_BACKEND", "simulator")
os.environ.setdefault("DATABASE_URL", "sqlite:///./data/vader.sqlite3")

from sqlalchemy import select  # noqa: E402

from app.db import init_db, session_scope  # noqa: E402
from app.hardware import get_hardware  # noqa: E402
from app.jobs import enqueue  # noqa: E402
from app.jobs.worker import run_pending_jobs_inline  # noqa: E402
from app.models import BackupCategory, JobType, Tape, TapeStatus  # noqa: E402


def build_tree(root: Path) -> None:
    rng = random.Random(42)
    for proj in ("Skyline", "Harbour"):
        for seq in ("seq010", "seq020"):
            shot = root / proj / seq / f"{seq}_0100"
            shot.mkdir(parents=True, exist_ok=True)
            for f in range(1, 25):
                (shot / f"{seq}_0100.{f:04d}.exr").write_bytes(bytes(rng.randrange(256) for _ in range(3000)))
        vids = root / proj / "video"
        vids.mkdir(parents=True, exist_ok=True)
        for c in range(1, 4):
            (vids / f"{proj.lower()}_master.{c:03d}.mov").write_bytes(b"\x00" * 40_000)
        cfg = root / proj / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "pipeline.json").write_bytes(b'{"ver": 3}\n')
        aud = root / proj / "audio"
        aud.mkdir(parents=True, exist_ok=True)
        (aud / "ambience.wav").write_bytes(b"RIFF" + b"\x00" * 20_000)


def main() -> None:
    init_db()
    src = Path(tempfile.mkdtemp(prefix="vader-seed-"))
    build_tree(src)

    with session_scope() as db:
        hw = get_hardware()
        known = {t.barcode for t in db.scalars(select(Tape)).all()}
        for slot in hw.library_status().slots:
            if slot.barcode and not slot.is_cleaning_tape and slot.barcode not in known:
                db.add(Tape(barcode=slot.barcode, lto_generation="LTO-8",
                            capacity_native_bytes=2_000_000,
                            status=TapeStatus.scratch, physical_location=f"slot {slot.number}"))

    with session_scope() as db:
        enqueue(db, JobType.write, {
            "source_path": str(src / "Skyline"), "mode": "standard",
            "source_machine": "render-fleet", "backup_category": BackupCategory.project_archive.value,
        })
        enqueue(db, JobType.write, {
            "source_path": str(src / "Harbour"), "mode": "greedy", "greedy_source": "Harbour-NAS",
            "backup_category": BackupCategory.machine_drive_backup.value,
        })
    run_pending_jobs_inline()
    print("seed complete — start the app and open http://localhost:8000/")


if __name__ == "__main__":
    main()
