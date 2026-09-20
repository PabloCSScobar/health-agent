"""Deterministyczne agregaty dla dashboardu.

Liczby powstają w Pythonie na bazie `daily_frame`; brak pomiaru pozostaje None
i nigdy nie jest zamieniany na zero. Moduł nie jest narzędziem agentów.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select

from health_agent.db.models import DataFreshness, Workout
from health_agent.db.session import get_session
from health_agent.time_utils import app_timezone, local_date, local_today, utc_day_bounds
from health_agent.tools.correlations import daily_frame


@dataclass(frozen=True)
class MetricSpec:
    key: str
    label: str
    unit: str
    decimals: int
    kind: str  # "level" (stan, np. waga) albo "total" (suma dnia, np. kroki)


METRIC_SPECS: tuple[MetricSpec, ...] = (
    MetricSpec("weight_kg", "Waga", "kg", 1, "level"),
    MetricSpec("fat_pct", "Tkanka tłuszczowa", "%", 1, "level"),
    MetricSpec("sleep_h", "Sen", "h", 1, "level"),
    MetricSpec("hrv", "HRV", "ms", 0, "level"),
    MetricSpec("steps", "Kroki", "", 0, "total"),
    MetricSpec("run_km", "Bieg", "km", 1, "total"),
    MetricSpec("protein_g", "Białko", "g", 0, "total"),
    MetricSpec("kcal", "Kalorie", "kcal", 0, "total"),
    MetricSpec("wellbeing", "Samopoczucie (1–5)", "", 1, "level"),
)

FRESHNESS_LABELS = {
    "workouts": "Treningi",
    "steps": "Kroki",
    "sleep": "Sen",
    "hrv": "HRV",
    "nutrition": "Żywienie",
    "body": "Skład ciała",
}

SOURCE_LABELS = {
    "intervals_icu": "Intervals.icu",
    "fitatu_healthconnect": "Fitatu (Health Connect)",
    "fitdays_healthconnect": "Fitdays (Health Connect)",
    "healthconnect": "Health Connect",
    "manual": "Wpis ręczny",
}


def _round(value: float | None, decimals: int) -> float | int | None:
    if value is None:
        return None
    rounded = round(float(value), decimals)
    return int(rounded) if decimals == 0 else rounded


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize_metric(
    rows: list[dict], previous_rows: list[dict], spec: MetricSpec
) -> dict:
    """Ostatnia wartość, średnia i porównanie z poprzednim oknem tej samej długości.

    Dla metryk "total" średnia liczona jest wyłącznie z dni, które mają wpis,
    więc brak danych nie zaniża wyniku.
    """
    values = [(row["date"], float(row[spec.key])) for row in rows if row[spec.key] is not None]
    previous = [float(row[spec.key]) for row in previous_rows if row[spec.key] is not None]
    latest_date, latest_value = values[-1] if values else (None, None)
    current_avg = _mean([value for _, value in values])
    previous_avg = _mean(previous)
    current_count = len(values)
    previous_count = len(previous)
    counts_comparable = (
        current_count >= 2
        and previous_count >= 2
        and min(current_count, previous_count) * 2
        >= max(current_count, previous_count)
    )
    delta = (
        current_avg - previous_avg
        if current_avg is not None and previous_avg is not None and counts_comparable
        else None
    )
    return {
        "key": spec.key,
        "label": spec.label,
        "unit": spec.unit,
        "decimals": spec.decimals,
        "kind": spec.kind,
        "latest": _round(latest_value, spec.decimals),
        "latest_date": latest_date.isoformat() if latest_date else None,
        "average": _round(current_avg, spec.decimals),
        "previous_average": _round(previous_avg, spec.decimals),
        "delta": _round(delta, spec.decimals),
        "min": _round(min((value for _, value in values), default=None), spec.decimals),
        "max": _round(max((value for _, value in values), default=None), spec.decimals),
        "days_with_data": current_count,
        "previous_days_with_data": previous_count,
        "comparison_available": counts_comparable,
        "days_total": len(rows),
    }


def summarize_frame(rows: list[dict], previous_rows: list[dict]) -> list[dict]:
    return [summarize_metric(rows, previous_rows, spec) for spec in METRIC_SPECS]


def _serialize_rows(rows: list[dict]) -> list[dict]:
    return [
        {key: value.isoformat() if isinstance(value, dt.date) else value for key, value in row.items()}
        for row in rows
    ]


def _recent_workouts(start: dt.date, end: dt.date, limit: int) -> list[dict]:
    start_utc, _ = utc_day_bounds(start)
    _, end_utc = utc_day_bounds(end)
    with get_session() as session:
        workouts = session.execute(
            select(Workout)
            .where(Workout.started_at >= start_utc, Workout.started_at < end_utc)
            .order_by(Workout.started_at.desc())
            .limit(limit)
        ).scalars().all()
        return [
            {
                "id": workout.id,
                "date": local_date(workout.started_at).isoformat(),
                "started_at": workout.started_at.astimezone(app_timezone()).isoformat(),
                "sport": workout.sport,
                "duration_min": _round(workout.duration_s / 60, 0) if workout.duration_s else None,
                "distance_km": _round(workout.distance_m / 1000, 2) if workout.distance_m else None,
                "avg_hr": workout.avg_hr,
                "ascent_m": _round(workout.ascent_m, 0),
                "source": workout.source,
            }
            for workout in workouts
            if workout.started_at is not None
        ]


def _freshness() -> list[dict]:
    with get_session() as session:
        rows = session.execute(
            select(DataFreshness).order_by(DataFreshness.metric, DataFreshness.source)
        ).scalars().all()
        return [
            {
                "metric": row.metric,
                "label": FRESHNESS_LABELS.get(row.metric, row.metric),
                "source": row.source,
                "source_label": SOURCE_LABELS.get(row.source, row.source),
                "observed_through": row.observed_through.isoformat(),
                "received_at": row.received_at.isoformat(),
            }
            for row in rows
        ]


def overview_payload(days: int, *, end_date: dt.date | None = None) -> dict:
    """Pełny zestaw danych zakładki Przegląd dla okna `days` kończącego się dziś."""
    days = min(max(days, 7), 90)
    end = end_date or local_today()
    start = end - dt.timedelta(days=days - 1)
    rows = daily_frame(end_date=end, days=days)
    previous_rows = daily_frame(end_date=start - dt.timedelta(days=1), days=days)
    return {
        "range": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        "today": local_today().isoformat(),
        "timezone": str(app_timezone()),
        "metrics": summarize_frame(rows, previous_rows),
        "series": _serialize_rows(rows),
        "workouts": _recent_workouts(start, end, limit=15),
        "freshness": _freshness(),
    }
