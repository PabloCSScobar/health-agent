#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT_DIR"

requested_mode=${1:-}
if [[ -n "$requested_mode" && "$requested_mode" != development && "$requested_mode" != production ]]; then
    echo "Użycie: ./deploy/install.sh [development|production]" >&2
    exit 2
fi

for command_name in docker openssl; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "Brak wymaganej komendy: $command_name" >&2
        exit 1
    fi
done
if ! docker compose version >/dev/null 2>&1; then
    echo "Brak Docker Compose v2 (komenda: docker compose)." >&2
    exit 1
fi

created_env=false
if [[ ! -f .env ]]; then
    cp .env.example .env
    created_env=true
    echo "Utworzono .env z szablonu."
fi

env_value() {
    sed -n "s/^${1}=//p" .env | tail -n 1
}

set_env_value() {
    local key=$1 value=$2 tmp
    tmp=$(mktemp)
    awk -v key="$key" -v value="$value" '
        BEGIN { replaced = 0 }
        $0 ~ "^" key "=" { if (!replaced) print key "=" value; replaced = 1; next }
        { print }
        END { if (!replaced) print key "=" value }
    ' .env >"$tmp"
    mv "$tmp" .env
}

install_mode=$requested_mode
if [[ -z "$install_mode" ]]; then
    install_mode=$(env_value APP_ENV)
fi
if [[ -z "$install_mode" ]]; then
    echo "Istniejący .env nie ma APP_ENV; wybierz jawnie development albo production." >&2
    echo "Użycie: ./deploy/install.sh [development|production]" >&2
    exit 2
fi
if [[ "$install_mode" != development && "$install_mode" != production && "$install_mode" != test ]]; then
    echo "Nieprawidłowe APP_ENV w .env: ${install_mode}" >&2
    exit 2
fi
set_env_value APP_ENV "$install_mode"
if [[ "$install_mode" == production ]]; then
    set_env_value SCHEDULER_ENABLED true
else
    set_env_value SCHEDULER_ENABLED false
fi
echo "Tryb instalacji: ${install_mode} (scheduler: $(env_value SCHEDULER_ENABLED))."

postgres_password=$(env_value POSTGRES_PASSWORD)
if [[ -z "$postgres_password" ]]; then
    if [[ "$created_env" != true ]]; then
        echo "Istniejący .env nie ma POSTGRES_PASSWORD." >&2
        echo "Ustaw hasło zgodne z istniejącym wolumenem bazy; instalator nie zmieni go automatycznie." >&2
        exit 1
    fi
    postgres_password=$(openssl rand -hex 24)
    set_env_value POSTGRES_PASSWORD "$postgres_password"
    set_env_value DATABASE_URL "postgresql+psycopg://health_agent:${postgres_password}@localhost:5432/health_agent"
fi

if [[ -z "$(env_value WEBHOOK_SHARED_SECRET)" ]]; then
    set_env_value WEBHOOK_SHARED_SECRET "$(openssl rand -hex 32)"
    echo "Wygenerowano WEBHOOK_SHARED_SECRET; wpisz tę samą wartość w aplikacji telefonu."
fi

required=(POSTGRES_PASSWORD DATABASE_URL ANTHROPIC_API_KEY INTERVALS_API_KEY INTERVALS_ATHLETE_ID TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID WEBHOOK_SHARED_SECRET)
missing=()
for key in "${required[@]}"; do
    value=$(env_value "$key")
    if [[ -z "$value" || "$value" == *UZUPELNIJ* ]]; then
        missing+=("$key")
    fi
done
if (("${#missing[@]}")); then
    echo "Uzupełnij w .env: ${missing[*]}" >&2
    echo "Następnie uruchom ponownie: ./deploy/install.sh" >&2
    exit 1
fi

docker compose config --quiet
docker compose build
docker compose up -d --wait --wait-timeout 180
"$ROOT_DIR/deploy/doctor.sh"

echo "Instalacja zakończona. API lokalnie: http://127.0.0.1:$(env_value API_PORT)/health"
