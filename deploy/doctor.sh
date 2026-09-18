#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT_DIR"

if [[ ! -f .env ]]; then
    echo "Brak .env. Uruchom ./deploy/install.sh." >&2
    exit 1
fi

require_running() {
    local service=$1 container_id
    container_id=$(docker compose ps -q "$service")
    if [[ -z "$container_id" || "$(docker inspect --format '{{.State.Running}}' "$container_id")" != true ]]; then
        echo "Usługa $service nie działa." >&2
        exit 1
    fi
}

echo "== Konfiguracja Compose =="
docker compose config --quiet

echo "== Usługi =="
docker compose ps
for service in db api bot; do
    require_running "$service"
done

echo "== PostgreSQL =="
docker compose exec -T db pg_isready -U health_agent -d health_agent

echo "== Migracje =="
current_revision=$(docker compose run --rm --no-deps migrate alembic current | awk 'NR == 1 {print $1}')
head_revision=$(docker compose run --rm --no-deps migrate alembic heads | awk 'NR == 1 {print $1}')
if [[ -z "$current_revision" || "$current_revision" != "$head_revision" ]]; then
    echo "Baza nie jest na aktualnej rewizji Alembica: current=$current_revision, head=$head_revision" >&2
    exit 1
fi
echo "Alembic: $current_revision"

echo "== API =="
docker compose exec -T api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"

echo "== Katalog backupów =="
docker compose exec -T api python -c "from pathlib import Path; p=Path('/app/backups/.write-test'); p.write_text('ok'); p.unlink()"

echo "Wszystkie kontrole zakończone powodzeniem."
