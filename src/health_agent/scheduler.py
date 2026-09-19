"""Automatyczny, cykliczny polling źródeł danych - działa w tle w tym samym
procesie co serwer FastAPI (patrz api/app.py, lifespan startup/shutdown).

Scheduler, Telegram `/sync` i CLI używają wspólnego `sync_intervals`.
Automatyczny catch-up i marker są atomowe, a advisory lock PostgreSQL zapobiega
równoległemu importowi przez osobne procesy API i bota.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import hashlib
import io
import json
import logging
import os
import subprocess
import tarfile
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from health_agent.db.models import BodyComposition, NutritionDay, Workout
from health_agent.db.session import get_session
from health_agent.ingest.sync import sync_intervals
from health_agent.settings import settings
from health_agent.tools.memory import recall_all, remember

logger = logging.getLogger("health_agent.scheduler")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

POLL_INTERVAL_MINUTES = 60


def poll_intervals_icu() -> None:
    """Uruchom wspólny catch-up; błąd jednego cyklu nie zatrzymuje schedulera."""
    try:
        result = sync_intervals()
        if result.status == "busy":
            logger.info("Intervals.icu poll pominięty: inna synchronizacja już trwa")
            return
        if result.truncated:
            logger.warning(
                "Synchronizacja ograniczona do %s–%s; starszą lukę uzupełnij ręcznie",
                result.oldest, result.newest,
            )
        logger.info(
            "Intervals.icu poll OK (%s–%s): workouts=%d wellness_days=%d",
            result.oldest, result.newest, result.workouts, result.wellness_days,
        )
    except Exception:
        logger.exception("Intervals.icu poll nieudany")


def _send_telegram_message(text: str, conversation_id: int | None = None) -> list[int]:
    """Wyślij tekst w bezpiecznych kawałkach; zwróć wszystkie message_id."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning("Brak TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID - wiadomość NIE wysłana")
        return []
    from telegram import Bot
    from health_agent.channels.telegram import _chunks, _update_conversation_messages

    async def _send() -> list[int]:
        bot = Bot(token=settings.telegram_bot_token)
        message_ids = []
        for chunk in _chunks(text):
            message = await bot.send_message(chat_id=settings.telegram_chat_id, text=chunk)
            message_ids.append(message.message_id)
            if conversation_id is not None:
                _update_conversation_messages(conversation_id, message_ids)
        return message_ids

    return asyncio.run(_send())


def _summary_local_date() -> dt.date:
    from zoneinfo import ZoneInfo

    return dt.datetime.now(ZoneInfo(settings.summary_timezone)).date()


def _send_summary(period: str) -> None:
    """Wygeneruj, wyślij i zapisz raport raz na okres w pojedynczym schedulerze.

    Marker powstaje po potwierdzonej wysyłce: chwilowa awaria może więc dać
    duplikat przy ręcznym retry, ale nie zgubi raportu przed wysłaniem.
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning("Brak TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID - podsumowanie NIE wysłane")
        return

    today = _summary_local_date()
    dedupe_key = today.isoformat() if period == "daily" else (today - dt.timedelta(days=today.weekday())).isoformat()
    memory_key = f"{period}_summary"
    if recall_all("system_summaries").get(memory_key) == dedupe_key:
        logger.info("Podsumowanie %s za %s było już wysłane", period, dedupe_key)
        return

    from health_agent.agents.summaries import build_daily_summary, build_weekly_summary

    conversation_id = None
    try:
        result = asyncio.run(build_daily_summary(today) if period == "daily" else build_weekly_summary(today))
        from health_agent.channels.telegram import (
            _create_assistant_conversation,
            _delete_conversation_if_unsent,
        )

        conversation_id = _create_assistant_conversation(
            str(settings.telegram_chat_id), result.text, period, result.root_run_id
        )
        message_ids = _send_telegram_message(result.text, conversation_id)
        if not message_ids:
            _delete_conversation_if_unsent(conversation_id)
            return
        remember("system_summaries", memory_key, dedupe_key)
        logger.info("Podsumowanie %s za %s wysłane", period, dedupe_key)
    except Exception:
        if conversation_id is not None:
            from health_agent.channels.telegram import _delete_conversation_if_unsent

            _delete_conversation_if_unsent(conversation_id)
        logger.exception("Nie udało się wygenerować lub wysłać podsumowania %s", period)


def send_daily_summary() -> None:
    _send_summary("daily")


def send_weekly_summary() -> None:
    _send_summary("weekly")


def run_correlations() -> None:
    try:
        from health_agent.tools.correlations import publish_correlations

        results = publish_correlations()
        logger.info(
            "Korelacje policzone: %d par, %d kwalifikujących",
            len(results),
            sum(item["status"] == "qualifying" for item in results),
        )
    except Exception:
        logger.exception("Analiza korelacji nieudana")


def process_reminders() -> None:
    try:
        from health_agent.tools.reminders import (
            dispatch_pending_notifications,
            evaluate_due_occurrences,
            materialize_due_occurrences,
        )

        materialize_due_occurrences()
        evaluated = evaluate_due_occurrences()
        delivered = dispatch_pending_notifications()
        if evaluated["queued"] or evaluated["retrying"] or evaluated["skipped"] or delivered["sent"]:
            logger.info("Cykl przypomnień: evaluated=%s delivery=%s", evaluated, delivered)
    except Exception:
        logger.exception("Cykl przypomnień nieudany")


def check_stale_sources() -> None:
    """Alert na Telegram, jeśli któreś źródło nie zsynchronizowało się od
    `settings.alerts_stale_hours` godzin (patrz plan, sekcja 9: apka Health
    Connect Webhook ma lookback tylko 48h - dłuższa przerwa w syncu, np.
    wyłączony Tailscale na telefonie, oznacza BEZPOWROTNĄ utratę danych za
    ten okres, więc wczesne ostrzeżenie ma realną wartość).

    Jeden alert per źródło per dzień (zapamiętane w agent_memory pod
    pseudo-agentem "system_alerts") - inaczej przy source martwym przez
    tydzień dostałbyś ten sam alert co `alerts_check_interval_minutes`."""
    if not settings.alerts_enabled:
        return

    threshold = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=settings.alerts_stale_hours)
    with get_session() as session:
        last_workout = session.execute(select(func.max(Workout.started_at))).scalar()
        last_body = session.execute(select(func.max(BodyComposition.measured_at))).scalar()
        last_nutrition_date = session.execute(select(func.max(NutritionDay.date))).scalar()

    last_nutrition = (
        dt.datetime.combine(last_nutrition_date, dt.time.min, tzinfo=dt.timezone.utc)
        if last_nutrition_date
        else None
    )

    sources = [
        ("workouts", "treningi (Intervals.icu)", last_workout),
        ("body_composition", "waga/skład ciała (Health Connect)", last_body),
        ("nutrition", "odżywianie (Health Connect)", last_nutrition),
    ]
    stale = [(key, label, last) for key, label, last in sources if last is None or last < threshold]
    if not stale:
        return

    today_str = dt.date.today().isoformat()
    already_alerted = recall_all("system_alerts")
    to_notify = [(key, label, last) for key, label, last in stale if already_alerted.get(key) != today_str]
    if not to_notify:
        return

    lines = [f"⚠️ Brak nowych danych z ostatnich {settings.alerts_stale_hours}h:"]
    for key, label, last in to_notify:
        lines.append(f"- {label}: ostatnio {last.date().isoformat() if last else 'nigdy'}")
        remember("system_alerts", key, today_str)

    try:
        _send_telegram_message("\n".join(lines))
        logger.warning("Alert o martwych źródłach wysłany: %s", [k for k, _, _ in to_notify])
    except Exception:
        logger.exception("Nie udało się wysłać alertu o martwych źródłach")


def backup_database() -> Path | None:
    """Wykonuje ``pg_dump`` bazy wskazanej przez ``DATABASE_URL``.

    Obraz aplikacji zawiera klienta PostgreSQL. Dzięki temu backup działa
    tak samo z Compose i z zewnętrzną bazą, bez dostępu do socketa Dockera.
    """
    if not settings.backup_enabled:
        return None

    backup_dir = Path(settings.backup_dir)
    if not backup_dir.is_absolute():
        backup_dir = _PROJECT_ROOT / backup_dir
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_file = backup_dir / f"health_agent_{timestamp}.sql.gz"
    photo_archive = backup_dir / f"health_agent_{timestamp}.photos.tar.gz"

    try:
        database_url = make_url(settings.database_url)
        if database_url.get_backend_name() != "postgresql":
            raise ValueError("Backup obsługuje wyłącznie PostgreSQL")
        if not database_url.host or not database_url.database or not database_url.username:
            raise ValueError("DATABASE_URL musi zawierać host, użytkownika i nazwę bazy")

        command = [
            "pg_dump",
            "--host",
            database_url.host,
            "--port",
            str(database_url.port or 5432),
            "--username",
            database_url.username,
            "--dbname",
            database_url.database,
            "--no-password",
        ]
        process_env = os.environ.copy()
        if database_url.password:
            process_env["PGPASSWORD"] = database_url.password
        if sslmode := database_url.query.get("sslmode"):
            process_env["PGSSLMODE"] = sslmode

        from health_agent.tools.photos import photo_archive_lock, photo_root

        with photo_archive_lock():
            # Jedna blokada obejmuje zrzut bazy i pliki, więc upload/usunięcie
            # zdjęcia nie może rozdzielić odpowiadających sobie artefaktów.
            result = subprocess.run(
                command,
                env=process_env,
                capture_output=True,
                check=True,
            )
            with gzip.open(out_file, "wb") as f:
                f.write(result.stdout)
            root = photo_root()
            files = [
                path for path in root.iterdir()
                if path.is_file() and not path.name.startswith(".")
            ]
            manifest = {
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "database_backup": out_file.name,
                "photos": [],
            }
            with tarfile.open(photo_archive, "w:gz") as archive:
                for path in sorted(files):
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    manifest["photos"].append(
                        {"file": path.name, "bytes": path.stat().st_size, "sha256": digest}
                    )
                    archive.add(path, arcname=f"photos/{path.name}", recursive=False)
                manifest_bytes = json.dumps(
                    manifest, ensure_ascii=False, indent=2
                ).encode("utf-8")
                info = tarfile.TarInfo("manifest.json")
                info.size = len(manifest_bytes)
                info.mtime = int(dt.datetime.now(dt.timezone.utc).timestamp())
                archive.addfile(info, io.BytesIO(manifest_bytes))
        logger.info("Backup bazy zapisany: %s (%d bajtów)", out_file, out_file.stat().st_size)
        logger.info(
            "Backup zdjęć zapisany: %s (%d plików)",
            photo_archive,
            len(manifest["photos"]),
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace").strip()
        logger.error("Backup bazy nieudany (pg_dump): %s", stderr or f"kod {exc.returncode}")
        out_file.unlink(missing_ok=True)
        photo_archive.unlink(missing_ok=True)
        return None
    except Exception:
        logger.exception("Backup bazy nieudany")
        out_file.unlink(missing_ok=True)
        photo_archive.unlink(missing_ok=True)
        return None

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=settings.backup_retention_days)
    for old_file in backup_dir.glob("health_agent_*"):
        if dt.datetime.fromtimestamp(old_file.stat().st_mtime, tz=dt.timezone.utc) < cutoff:
            old_file.unlink(missing_ok=True)
            logger.info("Usunięto stary backup: %s", old_file)

    return out_file


def build_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        poll_intervals_icu,
        "interval",
        minutes=POLL_INTERVAL_MINUTES,
        next_run_time=dt.datetime.now(dt.timezone.utc),  # od razu przy starcie, potem co godzinę
        id="poll_intervals_icu",
    )
    if settings.alerts_enabled:
        scheduler.add_job(
            check_stale_sources,
            "interval",
            minutes=settings.alerts_check_interval_minutes,
            id="check_stale_sources",
        )
    if settings.backup_enabled:
        scheduler.add_job(
            backup_database,
            "interval",
            hours=settings.backup_interval_hours,
            next_run_time=dt.datetime.now(dt.timezone.utc),  # od razu przy starcie, żeby nie czekać cały cykl na pierwszy backup
            id="backup_database",
        )
    if settings.daily_summary_enabled:
        scheduler.add_job(
            send_daily_summary,
            "cron",
            hour=settings.daily_summary_hour,
            minute=settings.daily_summary_minute,
            timezone=settings.summary_timezone,
            id="daily_summary",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
    if settings.weekly_summary_enabled:
        scheduler.add_job(
            send_weekly_summary,
            "cron",
            day_of_week=settings.weekly_summary_day,
            hour=settings.weekly_summary_hour,
            minute=settings.weekly_summary_minute,
            timezone=settings.summary_timezone,
            id="weekly_summary",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
    if settings.correlations_enabled:
        scheduler.add_job(
            run_correlations,
            "cron",
            day_of_week=settings.correlations_day,
            hour=settings.correlations_hour,
            minute=settings.correlations_minute,
            timezone=settings.summary_timezone,
            id="correlations",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
    if settings.reminders_enabled:
        scheduler.add_job(
            process_reminders,
            "interval",
            minutes=1,
            id="process_reminders",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
        )
    return scheduler
