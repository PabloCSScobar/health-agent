"""Fail-closed guard dla testów, które czyszczą całe tabele."""

from __future__ import annotations

import os

from sqlalchemy.engine import make_url

from health_agent.settings import settings

ISOLATED_DATABASE_TOKEN = "health-agent-isolated-postgres-v1"


def is_isolated_test_database() -> bool:
    try:
        url = make_url(settings.database_url)
    except Exception:
        return False
    return bool(
        settings.app_env == "test"
        and os.environ.get("HEALTH_AGENT_TEST_DATABASE_TOKEN")
        == ISOLATED_DATABASE_TOKEN
        and url.get_backend_name() == "postgresql"
        and url.username == "health_test"
        and url.database == "health_test"
    )


def require_isolated_test_database() -> None:
    if not is_isolated_test_database():
        raise RuntimeError(
            "Odmowa destrukcyjnego cleanupu: wymagana izolowana baza "
            "PostgreSQL health_test jako użytkownik health_test i jawny token testowy"
        )
