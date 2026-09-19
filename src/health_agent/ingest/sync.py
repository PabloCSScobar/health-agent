"""Koordynacja ręcznej i automatycznej synchronizacji Intervals.icu."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from health_agent.db.models import IngestState
from health_agent.db.session import get_session
from health_agent.ingest.intervals import ingest_range
from health_agent.time_utils import local_today

POLL_LOOKBACK_DAYS = 3
POLL_MAX_BACKFILL_DAYS = 60
INTERVALS_SOURCE = "intervals_icu"
# Stała, przenośna między procesami wartość; nie używamy losowego hash() Pythona.
_INTERVALS_LOCK_KEY = 0x4841494E54


@dataclass(frozen=True)
class IntervalsSyncResult:
    status: str
    oldest: dt.date | None = None
    newest: dt.date | None = None
    workouts: int = 0
    wellness_days: int = 0
    truncated: bool = False


def _try_advisory_lock(session: Session) -> bool:
    if session.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Synchronizacja Intervals.icu wymaga PostgreSQL")
    return bool(
        session.execute(select(func.pg_try_advisory_xact_lock(_INTERVALS_LOCK_KEY))).scalar_one()
    )


def _automatic_window(session: Session, newest: dt.date) -> tuple[dt.date, bool]:
    row = session.get(IngestState, INTERVALS_SOURCE)
    default_oldest = newest - dt.timedelta(days=POLL_LOOKBACK_DAYS)
    oldest = default_oldest if row is None else min(default_oldest, row.last_synced_date - dt.timedelta(days=1))
    floor = newest - dt.timedelta(days=POLL_MAX_BACKFILL_DAYS)
    truncated = oldest < floor
    return max(oldest, floor), truncated


def _mark_synced(session: Session, newest: dt.date) -> None:
    stmt = (
        pg_insert(IngestState)
        .values(source=INTERVALS_SOURCE, last_synced_date=newest)
        .on_conflict_do_update(
            index_elements=["source"],
            set_={"last_synced_date": newest, "updated_at": dt.datetime.now(dt.timezone.utc)},
        )
    )
    session.execute(stmt)


def sync_intervals(
    oldest: dt.date | None = None,
    newest: dt.date | None = None,
) -> IntervalsSyncResult:
    """Synchronizuj Intervals.icu pod międzyprocesową blokadą.

    Brak jawnego zakresu oznacza ten sam catch-up co scheduler i aktualizuje
    marker udanego pollingu. Jawny zakres (CLI) nie przesuwa markera.
    """
    explicit_range = oldest is not None
    newest = newest or local_today()
    if oldest is not None and oldest > newest:
        raise ValueError("Data początkowa nie może być późniejsza niż końcowa")

    with get_session() as session:
        if not _try_advisory_lock(session):
            return IntervalsSyncResult(status="busy")

        truncated = False
        if oldest is None:
            oldest, truncated = _automatic_window(session, newest)

        summary = ingest_range(session, oldest, newest)
        if not explicit_range:
            _mark_synced(session, newest)

        return IntervalsSyncResult(
            status="ok",
            oldest=oldest,
            newest=newest,
            workouts=summary["workouts"],
            wellness_days=summary["wellness_days"],
            truncated=truncated,
        )
