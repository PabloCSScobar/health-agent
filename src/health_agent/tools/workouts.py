"""Narzędzia odczytu treningów - wołane przez RunningCoach (i inne, ogólnie)."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel
from sqlalchemy import select

from health_agent.db.models import Workout
from health_agent.db.session import get_session
from health_agent.time_utils import utc_day_bounds


class WorkoutSummary(BaseModel):
    external_id: str
    sport: str | None
    started_at: dt.datetime | None
    duration_s: int | None
    distance_m: float | None
    avg_hr: int | None
    max_hr: int | None
    calories: float | None
    ascent_m: float | None
    avg_pace_sec_per_km: float | None


def get_workouts(sport: str | None = None, days: int = 30) -> list[WorkoutSummary]:
    """Lista treningów z ostatnich `days` dni, opcjonalnie filtrowana po sporcie
    (np. "Run", "Ride" - dokładne wartości zależą od źródła, patrz przykłady
    w danych zamiast zgadywać)."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    with get_session() as session:
        stmt = select(Workout).where(Workout.started_at >= since).order_by(Workout.started_at.desc())
        if sport:
            stmt = stmt.where(Workout.sport == sport)
        rows = session.execute(stmt).scalars().all()
        return [
            WorkoutSummary(
                external_id=r.external_id, sport=r.sport, started_at=r.started_at,
                duration_s=r.duration_s, distance_m=r.distance_m, avg_hr=r.avg_hr,
                max_hr=r.max_hr, calories=r.calories, ascent_m=r.ascent_m,
                avg_pace_sec_per_km=r.avg_pace,
            )
            for r in rows
        ]


def get_latest_workout(sport: str | None = None) -> WorkoutSummary | None:
    """Ostatni zarejestrowany trening, NIEZALEŻNIE od tego jak dawno temu był
    (np. tydzień temu) - w przeciwieństwie do get_workouts() nie wymaga
    zgadywania liczby dni wstecz. Użyj tego dla pytań w stylu "ostatni
    trening", "jak wyszedł mój bieg" bez podanej konkretnej daty."""
    with get_session() as session:
        stmt = select(Workout).order_by(Workout.started_at.desc()).limit(1)
        if sport:
            stmt = stmt.where(Workout.sport == sport)
        row = session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        return WorkoutSummary(
            external_id=row.external_id, sport=row.sport, started_at=row.started_at,
            duration_s=row.duration_s, distance_m=row.distance_m, avg_hr=row.avg_hr,
            max_hr=row.max_hr, calories=row.calories, ascent_m=row.ascent_m,
            avg_pace_sec_per_km=row.avg_pace,
        )


def get_workouts_on_date(date: dt.date, sport: str | None = None) -> list[WorkoutSummary]:
    """Treningi z KONKRETNEGO dnia (np. "wczoraj", "15 września" - przelicz to
    najpierw na konkretną datę używając dzisiejszej daty z promptu). Użyj
    tego zamiast get_workouts(days=N) dla pytań o konkretny dzień - nie
    zgaduj liczby dni wstecz, to zawodne."""
    start, end = utc_day_bounds(date)
    with get_session() as session:
        stmt = select(Workout).where(Workout.started_at >= start, Workout.started_at < end).order_by(Workout.started_at)
        if sport:
            stmt = stmt.where(Workout.sport == sport)
        rows = session.execute(stmt).scalars().all()
        return [
            WorkoutSummary(
                external_id=r.external_id, sport=r.sport, started_at=r.started_at,
                duration_s=r.duration_s, distance_m=r.distance_m, avg_hr=r.avg_hr,
                max_hr=r.max_hr, calories=r.calories, ascent_m=r.ascent_m,
                avg_pace_sec_per_km=r.avg_pace,
            )
            for r in rows
        ]


def get_workout_detail(external_id: str) -> dict | None:
    """Pełne surowe dane treningu (raw_json) - do pytań wymagających szczegółów
    niedostępnych w podsumowaniu. UWAGA: external_id musi pochodzić z
    wcześniejszego wyniku get_workouts()/get_latest_workout() - NIGDY nie
    zgaduj/wymyślaj ID."""
    with get_session() as session:
        row = session.execute(
            select(Workout).where(Workout.external_id == external_id)
        ).scalar_one_or_none()
        return row.raw_json if row else None
