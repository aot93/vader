#!/usr/bin/env bash
# Catalog durability (framework doc §8): dump the database and the full-catalog
# CSV to $BACKUP_DIR, which should live on a different machine/share from the VM.
#
# Run from cron on the VM, e.g. weekly during the annual run and monthly otherwise:
#   0 3 * * 0  cd /opt/vader && ./scripts/backup.sh >> /var/log/vader-backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

"$PY" - <<'PYEOF'
from app.db import session_scope
from app.scripts_support import run_db_backup
from app.services.export_csv import write_full_catalog_export

db_path = run_db_backup()
with session_scope() as db:
    csv_path = write_full_catalog_export(db)
print(f"database backup : {db_path}")
print(f"catalog csv     : {csv_path}")
PYEOF

echo "backup complete: $(date -u +%FT%TZ)"
