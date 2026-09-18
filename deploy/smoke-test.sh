#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT_DIR"

for command_name in docker openssl; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "Brak wymaganej komendy: $command_name" >&2
        exit 1
    fi
done

suffix="$(date +%s)-$$"
network_name="health-agent-smoke-network-$suffix"
db_container="health-agent-smoke-db-$suffix"
backup_volume="health-agent-smoke-backups-$suffix"
db_password=$(openssl rand -hex 24)
database_url="postgresql+psycopg://smoke:${db_password}@${db_container}:5432/health_agent"

cleanup() {
    docker rm -f "$db_container" >/dev/null 2>&1 || true
    docker volume rm "$backup_volume" >/dev/null 2>&1 || true
    docker network rm "$network_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== Obraz =="
docker build --tag health-agent:local .

echo "== Tymczasowy PostgreSQL =="
docker network create "$network_name" >/dev/null
docker volume create "$backup_volume" >/dev/null
docker run --detach \
    --name "$db_container" \
    --network "$network_name" \
    --env POSTGRES_USER=smoke \
    --env POSTGRES_PASSWORD="$db_password" \
    --env POSTGRES_DB=health_agent \
    postgres:16 >/dev/null

database_ready=false
for _ in {1..60}; do
    if docker exec "$db_container" pg_isready -U smoke -d health_agent >/dev/null 2>&1; then
        database_ready=true
        break
    fi
    sleep 1
done
if [[ "$database_ready" != true ]]; then
    echo "Tymczasowy PostgreSQL nie wystartował w ciągu 60 sekund." >&2
    exit 1
fi

echo "== Migracje =="
docker run --rm \
    --network "$network_name" \
    --env DATABASE_URL="$database_url" \
    health-agent:local alembic upgrade head

docker exec "$db_container" psql -U smoke -d health_agent -v ON_ERROR_STOP=1 -c \
    "CREATE TABLE deployment_smoke (id integer PRIMARY KEY, value text NOT NULL); INSERT INTO deployment_smoke VALUES (1, 'backup-ok');" \
    >/dev/null

echo "== Backup =="
docker run --rm \
    --network "$network_name" \
    --env DATABASE_URL="$database_url" \
    --env BACKUP_ENABLED=true \
    --env BACKUP_DIR=/app/backups \
    --volume "$backup_volume:/app/backups" \
    health-agent:local \
    python -c "from health_agent.scheduler import backup_database; assert backup_database() is not None"

echo "== Odtworzenie =="
docker exec "$db_container" createdb -U smoke restored
docker run --rm \
    --volume "$backup_volume:/app/backups:ro" \
    health-agent:local \
    python -c "import glob, gzip, shutil, sys; shutil.copyfileobj(gzip.open(glob.glob('/app/backups/health_agent_*.sql.gz')[0], 'rb'), sys.stdout.buffer)" \
    | docker exec -i "$db_container" psql -U smoke -d restored -v ON_ERROR_STOP=1 >/dev/null

restored_value=$(docker exec "$db_container" psql -U smoke -d restored -Atc \
    "SELECT value FROM deployment_smoke WHERE id = 1")
migration_count=$(docker exec "$db_container" psql -U smoke -d restored -Atc \
    "SELECT count(*) FROM alembic_version")

if [[ "$restored_value" != "backup-ok" || "$migration_count" != "1" ]]; then
    echo "Odtworzona baza nie zawiera oczekiwanych danych lub rewizji Alembica." >&2
    exit 1
fi

echo "Smoke test zakończony powodzeniem: migracje, backup i restore działają."
