"""Catalog-durability helpers (framework doc §8): database backup + CSV export.

Importable so the background ``backup`` job and ``scripts/backup.sh`` share one
implementation.
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from app.config import get_settings


def run_db_backup() -> str:
    """Copy the SQLite file, or ``pg_dump`` a Postgres database, into BACKUP_DIR."""
    settings = get_settings()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    settings.backup_dir.mkdir(parents=True, exist_ok=True)

    if settings.is_sqlite:
        src = settings.database_url.split("///", 1)[-1]
        src_path = Path(src).resolve()
        dst = settings.backup_dir / f"vader-{stamp}.sqlite3"
        shutil.copy2(src_path, dst)
        return str(dst)

    parsed = urlparse(settings.database_url.replace("+psycopg", "").replace("+psycopg2", ""))
    dst = settings.backup_dir / f"vader-{stamp}.sql"
    env_pass = parsed.password or ""
    cmd = [
        "pg_dump",
        "-h", parsed.hostname or "localhost",
        "-p", str(parsed.port or 5432),
        "-U", parsed.username or "vader",
        "-d", (parsed.path or "/vader").lstrip("/"),
        "-f", str(dst),
    ]
    subprocess.run(cmd, check=True, env={"PGPASSWORD": env_pass, "PATH": "/usr/bin:/usr/local/bin"})
    return str(dst)
