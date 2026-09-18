"""Automatyczny, cykliczny polling źródeł danych - działa w tle w tym samym
procesie co serwer FastAPI (patrz api/app.py, lifespan startup/shutdown).

Ręczne pobranie danych (przez użytkownika z CLI, albo w przyszłości przez
narzędzie agenta) nie jest tu duplikowane - i scheduler, i CLI, i przyszłe
narzędzie agenta wywołują dokładnie tę samą funkcję `ingest_range` z
`ingest/intervals.py`. Dzięki idempotentnym upsertom bezpiecznie mogą się
zazębiać (np. ręczne pobranie w trakcie oczekiwania na kolejny cykl
schedulera nie zepsuje niczego).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import logging
import subprocess
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from health_agent.db.models import BodyComposition, IngestState, NutritionDay, Workout
from health_agent.db.session import get_session
from health_agent.ingest.intervals import ingest_range
from health_agent.settings import settings
from health_agent.tools.memory import recall_all, remember

logger = logging.getLogger("health_agent.scheduler")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Okno "do tyłu" przy każdym cyklu - nie tylko "dziś", bo Intervals.icu
# potrafi doliczać/poprawiać dane (np. ctl/atl, spóźniona synchronizacja
# zegarka) także dla ostatnich kilku dni, nie tylko bieżącego.
POLL_LOOKBACK_DAYS = 3
POLL_MAX_BACKFILL_DAYS = 60  # bezpiecznik - dłuższa przerwa niż to wymaga ręcznego `health-agent ingest intervals --since`
POLL_INTERVAL_MINUTES = 60
_INGEST_SOURCE = "intervals_icu"


def _last_synced_date(session, source: str) -> dt.date | None:
    row = session.get(IngestState, source)
    return row.last_synced_date if row else None


def _mark_synced(session, source: str, date: dt.date) -> None:
    stmt = (
        pg_insert(IngestState)
        .values(source=source, last_synced_date=date)
        .on_conflict_do_update(index_elements=["source"], set_={"last_synced_date": date})
    )
    session.execute(stmt)


def poll_intervals_icu() -> None:
    """Okno pobierania liczone OD OSTATNIEGO UDANEGO SYNCU, nie od stałych
    N dni wstecz - złapane na żywo: serwer stał ~16h po restarcie WSL, przy
    stałym oknie 3 dni nic by się nie zgubiło tym razem, ale przy dłuższej
    przerwie (>3 dni) luka nigdy nie zostałaby dociągnięta automatycznie.
    Teraz: `oldest` = min(dziś - POLL_LOOKBACK_DAYS, ostatni sync - 1 dzień
    zakładki), ograniczone z dołu przez POLL_MAX_BACKFILL_DAYS (dłuższa
    przerwa = ręczny `ingest intervals --since`, żeby nie ciągnąć
    bezterminowo przy np. skasowanym stanie)."""
    newest = dt.date.today()
    with get_session() as session:
        last_synced = _last_synced_date(session, _INGEST_SOURCE)
    default_oldest = newest - dt.timedelta(days=POLL_LOOKBACK_DAYS)
    if last_synced is None:
        oldest = default_oldest
    else:
        oldest = min(default_oldest, last_synced - dt.timedelta(days=1))
    floor = newest - dt.timedelta(days=POLL_MAX_BACKFILL_DAYS)
    if oldest < floor:
        logger.warning(
            "Luka w danych Intervals.icu większa niż %d dni (ostatni sync: %s) - "
            "pobieram tylko od %s, resztę dociągnij ręcznie: "
            "`uv run health-agent ingest intervals --since <data>`",
            POLL_MAX_BACKFILL_DAYS, last_synced, floor,
        )
        oldest = floor

    try:
        with get_session() as session:
            summary = ingest_range(session, oldest, newest)
            _mark_synced(session, _INGEST_SOURCE, newest)
        logger.info("Intervals.icu poll OK (od %s): %s", oldest, summary)
    except Exception:
        # Świadomie łapiemy wszystko i tylko logujemy - błąd jednego cyklu
        # (np. chwilowy problem sieciowy) nie może ubić całego schedulera,
        # kolejny cykl i tak spróbuje ponownie za POLL_INTERVAL_MINUTES.
        # `_mark_synced` NIE woła się przy wyjątku - luka zostaje widoczna
        # i następny cykl spróbuje ją dociągnąć ponownie, zamiast po cichu
        # przeskoczyć dzień, który się nie udał.
        logger.exception("Intervals.icu poll nieudany")


def _send_telegram_message(text: str) -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning("Brak TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID - alert NIE wysłany: %s", text)
        return
    from telegram import Bot

    async def _send() -> None:
        bot = Bot(token=settings.telegram_bot_token)
        await bot.send_message(chat_id=settings.telegram_chat_id, text=text)

    asyncio.run(_send())


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


def backup_database() -> None:
    """`docker compose exec db pg_dump ...` (nie lokalny `pg_dump` - nie ma
    gwarancji, że jest zainstalowany na hoście w kompatybilnej wersji; sam
    Postgres i tak żyje tylko w Dockerze, patrz docker-compose.yml) ->
    gzip -> plik w `settings.backup_dir`. Czyści backupy starsze niż
    `settings.backup_retention_days`, żeby katalog nie rósł bez końca."""
    if not settings.backup_enabled:
        return

    backup_dir = Path(settings.backup_dir)
    if not backup_dir.is_absolute():
        backup_dir = _PROJECT_ROOT / backup_dir
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_file = backup_dir / f"health_agent_{timestamp}.sql.gz"

    try:
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", "db", "pg_dump", "-U", "health_agent", "health_agent"],
            cwd=_PROJECT_ROOT,
            capture_output=True,
            check=True,
        )
        with gzip.open(out_file, "wb") as f:
            f.write(result.stdout)
        logger.info("Backup bazy zapisany: %s (%d bajtów)", out_file, out_file.stat().st_size)
    except subprocess.CalledProcessError:
        logger.exception("Backup bazy nieudany (pg_dump)")
        out_file.unlink(missing_ok=True)
        return
    except Exception:
        logger.exception("Backup bazy nieudany")
        out_file.unlink(missing_ok=True)
        return

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=settings.backup_retention_days)
    for old_file in backup_dir.glob("health_agent_*.sql.gz"):
        if dt.datetime.fromtimestamp(old_file.stat().st_mtime, tz=dt.timezone.utc) < cutoff:
            old_file.unlink(missing_ok=True)
            logger.info("Usunięto stary backup: %s", old_file)


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
    return scheduler
