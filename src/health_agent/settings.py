"""Centralna konfiguracja aplikacji, czytana z .env."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Baza danych ---
    database_url: str = "postgresql+psycopg://health_agent:health_agent_dev@localhost:5432/health_agent"

    # --- Health Connect Webhook ---
    webhook_shared_secret: str | None = None

    # --- Intervals.icu ---
    intervals_api_key: str | None = None
    intervals_athlete_id: str | None = None

    # --- Fitatu (ścieżka zapasowa) ---
    fitatu_bearer_token: str | None = None
    fitatu_refresh_token: str | None = None
    fitatu_user_id: str | None = None
    fitatu_api_cluster: str | None = None
    fitatu_app_os: str = "FITATU-WEB"
    fitatu_app_version: str = "4.9.1"
    fitatu_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    )

    # --- Telegram ---
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # --- LLM ---
    anthropic_api_key: str | None = None
    claude_model: str = "anthropic:claude-opus-5"
    ollama_model: str = "qwen3:14b"
    ollama_base_url: str = "http://localhost:11434/v1"

    # --- Alerty (martwe źródła danych, patrz scheduler.py) ---
    alerts_enabled: bool = True
    alerts_stale_hours: int = 48
    alerts_check_interval_minutes: int = 60

    # --- Backup bazy (pg_dump przez `docker compose exec`, patrz scheduler.py) ---
    backup_enabled: bool = True
    backup_dir: str = "backups"
    backup_interval_hours: int = 24
    backup_retention_days: int = 14


settings = Settings()
