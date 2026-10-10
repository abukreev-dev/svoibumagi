#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail

cd /home/www/svoibumagi.bukreev.pro
umask 077
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="/backups/svoibumagi-${stamp}.zip"

docker compose -f compose.yaml -f deploy/compose.ddl.yaml --env-file .env \
  exec -T app python -m svoibumagi backup --data-dir /data --output "$archive"

python3 - "$PWD/backups/$(basename "$archive")" <<'PY'
from pathlib import Path
import sys
import zipfile

path = Path(sys.argv[1])
with zipfile.ZipFile(path) as backup:
    if backup.testzip() is not None or not {"manifest.json", "ledger.sqlite3"}.issubset(backup.namelist()):
        raise SystemExit("Backup verification failed")
print(path)
PY
