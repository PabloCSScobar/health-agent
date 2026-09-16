"""Narzędzia odczytu regeneracji/aktywności dziennej - wołane przez RecoveryAnalyst."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel
from sqlalchemy import select

from health_agent.db.models import DailyActivity, Recovery, Sleep
from health_agent.db.session import get_session


class RecoveryDayPoint(BaseModel):
    date: dt.date
    steps: int | None
    resting_hr: int | None
    calories_active: float | None
    calories_total: float | None
    hrv: float | None
    stress: float | None
    sleep_duration_s: int | None
    sleep_score: float | None


def _recovery_points(since: dt.date, until: dt.date) -> list[RecoveryDayPoint]:
    """Wspólna logika scalania źródeł dla get_recovery_day/get_recovery_range.

    UWAGA: może być więcej niż jeden wiersz DailyActivity na ten sam dzień -
    różne źródła (intervals_icu ma steps/resting_hr, healthconnect może mieć
    steps/calories_active/calories_total). Scalamy pola ze wszystkich źródeł
    per dzień zamiast brać jeden losowy wiersz (co po cichu gubiłoby dane
    drugiego źródła)."""
    with get_session() as session:
        activity_by_date: dict[dt.date, dict] = {}
        for r in session.execute(
            select(DailyActivity).where(DailyActivity.date >= since, DailyActivity.date <= until)
        ).scalars().all():
            merged = activity_by_date.setdefault(r.date, {})
            for field in ("steps", "resting_hr", "calories_active", "calories_total"):
                value = getattr(r, field)
                if value is not None and merged.get(field) is None:
                    merged[field] = value

        recovery_by_date = {
            r.date: r for r in session.execute(
                select(Recovery).where(Recovery.date >= since, Recovery.date <= until)
            ).scalars().all()
        }
        sleep_by_date = {
            r.date: r for r in session.execute(
                select(Sleep).where(Sleep.date >= since, Sleep.date <= until)
            ).scalars().all()
        }

    all_dates = sorted(set(activity_by_date) | set(recovery_by_date) | set(sleep_by_date))
    result = []
    for date in all_dates:
        act = activity_by_date.get(date, {})
        rec = recovery_by_date.get(date)
        slp = sleep_by_date.get(date)
        result.append(
            RecoveryDayPoint(
                date=date,
                steps=act.get("steps"),
                resting_hr=act.get("resting_hr"),
                calories_active=act.get("calories_active"),
                calories_total=act.get("calories_total"),
                hrv=rec.hrv if rec else None,
                stress=rec.stress if rec else None,
                sleep_duration_s=slp.duration_s if slp else None,
                sleep_score=slp.score if slp else None,
            )
        )
    return result


def get_recovery_day(date: dt.date) -> RecoveryDayPoint | None:
    """Regeneracja/aktywność z KONKRETNEGO dnia (np. "wczoraj", "dziś", "w
    poniedziałek" - przelicz to najpierw na konkretną datę używając
    dzisiejszej daty z promptu). Użyj tego zamiast get_recovery_range dla
    pytań o pojedynczy dzień - zwraca tylko potrzebne dane zamiast całego
    okna, taniej i bez ryzyka wybrania złego wiersza z listy."""
    points = _recovery_points(date, date)
    return points[0] if points else None


def get_recovery_range(days: int = 14) -> list[RecoveryDayPoint]:
    """Połączony widok dzień-po-dniu za ostatnie `days` dni - do TRENDÓW
    (np. "jak zmieniało się HRV w tym tygodniu"), NIE do pytań o pojedynczy
    dzień (do tego jest get_recovery_day - taniej i bez ryzyka pomyłki przy
    wyborze wiersza).

    UWAGA: calories_total to CAŁODNIOWY wydatek (BMR + aktywność) - dostępny
    tylko jeśli źródło Health Connect (np. Suunto przez Health Sync) go
    wypełnia, Intervals.icu tego nie ma. Jeśli oba pola calories_* są None,
    to znaczy że tej danej po prostu nie mamy z żadnego źródła - powiedz to
    wprost, nie myl z kaloriami z konkretnego treningu (to inne narzędzie -
    running)."""
    since = dt.date.today() - dt.timedelta(days=days)
    until = dt.date.today()
    return _recovery_points(since, until)
