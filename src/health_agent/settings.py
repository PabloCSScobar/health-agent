"""Centralna konfiguracja aplikacji, czytana z .env."""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Środowisko uruchomieniowe ---
    app_env: Literal["development", "production", "test"] = "development"
    scheduler_enabled: bool = False

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

    # --- Dashboard i archiwum zdjęć ---
    dashboard_password_hash: str | None = None
    dashboard_cookie_secure: bool = True
    dashboard_session_idle_hours: int = 12
    dashboard_session_absolute_days: int = 7
    progress_photos_dir: str = "data/photos"
    progress_photo_max_bytes: int = 15 * 1024 * 1024
    progress_photo_max_pixels: int = 25_000_000

    # --- Korelacje i przypomnienia ---
    correlations_enabled: bool = False
    correlations_day: str = "sun"
    correlations_hour: int = 19
    correlations_minute: int = 0
    reminders_enabled: bool = True
    reminder_data_fresh_hours: int = 2

    # --- LLM ---
    anthropic_api_key: str | None = None
    claude_model: str = "anthropic:claude-opus-5"
    ollama_model: str = "qwen3:14b"
    ollama_base_url: str = "http://localhost:11434/v1"

    # --- Alerty (martwe źródła danych, patrz scheduler.py) ---
    alerts_enabled: bool = True
    alerts_stale_hours: int = 48
    alerts_check_interval_minutes: int = 60

    # --- Automatyczne podsumowania na Telegramie ---
    summary_timezone: str = "Europe/Warsaw"
    daily_summary_enabled: bool = False
    daily_summary_hour: int = 20
    daily_summary_minute: int = 30
    weekly_summary_enabled: bool = False
    weekly_summary_day: str = "sun"
    weekly_summary_hour: int = 20
    weekly_summary_minute: int = 0

    # --- Backup bazy (pg_dump przez `docker compose exec`, patrz scheduler.py) ---
    backup_enabled: bool = True
    backup_dir: str = "backups"
    backup_interval_hours: int = 24
    backup_retention_days: int = 14


settings = Settings()
