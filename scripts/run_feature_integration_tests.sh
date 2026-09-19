#!/usr/bin/env bash
set -euo pipefail

test_container="health-agent-feature-test-$$"
test_password="health_test_password"

cleanup() {
    docker stop "$test_container" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run --rm -d \
    --name "$test_container" \
    -e POSTGRES_USER=health_test \
    -e POSTGRES_PASSWORD="$test_password" \
    -e POSTGRES_DB=health_test \
    -p 127.0.0.1::5432 \
    postgres:16 >/dev/null

test_port="$(docker port "$test_container" 5432/tcp | sed 's/.*://')"
test_database_url="postgresql+psycopg://health_test:${test_password}@127.0.0.1:${test_port}/health_test"

for _ in $(seq 1 30); do
    if docker exec "$test_container" pg_isready -U health_test -d health_test >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
docker exec "$test_container" pg_isready -U health_test -d health_test >/dev/null

APP_ENV=test DATABASE_URL="$test_database_url" \
    uv run --frozen --no-sync alembic upgrade head

APP_ENV=test \
DATABASE_URL="$test_database_url" \
HEALTH_AGENT_TEST_DATABASE_TOKEN=health-agent-isolated-postgres-v1 \
PROGRESS_PHOTOS_DIR=/tmp/health-agent-feature-photos \
DASHBOARD_COOKIE_SECURE=false \
RUN_BACKUP_RESTORE_TEST=1 \
TEST_POSTGRES_CONTAINER="$test_container" \
    uv run --frozen --no-sync python -m unittest -v \
        tests.test_feature_stack_postgres \
        tests.test_feature_resilience_postgres \
        tests.test_backup_restore_postgres
