#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT_DIR"

echo "== Budowanie obrazu =="
docker compose build

echo "== Backup przed migracją =="
docker compose run --rm --no-deps api python -c "from health_agent.scheduler import backup_database; from health_agent.settings import settings; settings.backup_enabled = True; path = backup_database(); assert path is not None, 'Backup bazy nie powiódł się'; print(path)"

echo "== Migracje =="
docker compose stop api bot
if ! docker compose run --rm migrate; then
    echo "Migracja nie powiodła się. API i bot pozostają zatrzymane; sprawdź log oraz backup przed ponownym uruchomieniem." >&2
    exit 1
fi

docker compose up -d --wait --wait-timeout 180
"$ROOT_DIR/deploy/doctor.sh"
