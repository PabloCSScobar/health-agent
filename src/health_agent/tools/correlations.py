"""Deterministyczna analiza wcześniej ustalonych par metryk.

Brakujące pomiary pozostają None. Wyniki są obserwacjami eksploracyjnymi,
nie dowodem przyczynowości; do wiedzy trafiają dopiero przy n>=20 i |rho|>=0.4.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select

from health_agent.db.models import (
    CorrelationResult,
    BodyComposition,
    DailyActivity,
    Knowledge,
    ManualLog,
    NutritionDay,
    Recovery,
    Sleep,
    Workout,
)
from health_agent.db.session import get_session
from health_agent.time_utils import app_timezone, local_date, local_today, utc_day_bounds
from health_agent.tools.running import MIN_ANALYSIS_KM, RUN_SPORTS, _efficiency, _is_walk

METHOD_VERSION = "spearman-v1"
WINDOW_DAYS = 84
MIN_SAMPLES = 20
PUBLISH_ABS_RHO = 0.4


@dataclass(frozen=True)
class PairSpec:
    key: str
    label: str
    domain: str


PAIR_SPECS = {
    "sleep_hrv": PairSpec("sleep_hrv", "czas snu a HRV tego dnia", "recovery"),
    "sleep_wellbeing": PairSpec("sleep_wellbeing", "czas snu a samopoczucie tego dnia", "recovery"),
    "sleep_run_efficiency": PairSpec("sleep_run_efficiency", "czas snu a efektywność biegu tego dnia", "running"),
    "prior_load_hrv": PairSpec("prior_load_hrv", "obciążenie z 2 poprzednich dni a HRV", "recovery"),
    "run_time_next_sleep": PairSpec("run_time_next_sleep", "pora biegu a jakość snu następnej nocy", "running"),
}


def _rank(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i + 1
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        rank = (i + 1 + j) / 2
        for k in range(i, j):
            ranks[indexed[k][0]] = rank
        i = j
    return ranks


def spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) != len(y) or len(x) < 2 or len(set(x)) < 2 or len(set(y)) < 2:
        return None
    rx, ry = _rank(x), _rank(y)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    numerator = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    denominator = math.sqrt(
        sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)
    )
    return None if denominator == 0 else numerator / denominator


def daily_frame(end_date: dt.date | None = None, days: int = WINDOW_DAYS) -> list[dict]:
    end = end_date or local_today()
    start = end - dt.timedelta(days=days - 1)
    frame = {
        start + dt.timedelta(days=offset): {
            "date": start + dt.timedelta(days=offset),
            "sleep_h": None,
            "sleep_score": None,
            "hrv": None,
            "wellbeing": None,
            "steps": None,
            "run_load": None,
            "run_km": None,
            "run_efficiency": None,
            "run_start_hour": None,
            "weight_kg": None,
            "fat_pct": None,
            "kcal": None,
            "protein_g": None,
        }
        for offset in range(days)
    }
    start_utc, _ = utc_day_bounds(start)
    _, end_utc = utc_day_bounds(end)
    with get_session() as session:
        sleeps = session.execute(
            select(Sleep).where(Sleep.date.between(start, end)).order_by(Sleep.date)
        ).scalars().all()
        recoveries = session.execute(
            select(Recovery).where(Recovery.date.between(start, end)).order_by(Recovery.date)
        ).scalars().all()
        activities = session.execute(
            select(DailyActivity).where(DailyActivity.date.between(start, end))
        ).scalars().all()
        workouts = session.execute(
            select(Workout).where(
                Workout.started_at >= start_utc,
                Workout.started_at < end_utc,
                Workout.sport.in_(RUN_SPORTS),
            )
        ).scalars().all()
        body_rows = session.execute(
            select(BodyComposition).where(
                BodyComposition.measured_at >= start_utc,
                BodyComposition.measured_at < end_utc,
            ).order_by(BodyComposition.measured_at)
        ).scalars().all()
        nutrition_rows = session.execute(
            select(NutritionDay).where(NutritionDay.date.between(start, end))
        ).scalars().all()
        logs = session.execute(
            select(ManualLog)
            .where(ManualLog.kind == "wellbeing", ManualLog.logged_at < end_utc)
            .order_by(ManualLog.logged_at)
        ).scalars().all()

    for row in sorted(sleeps, key=lambda item: item.source != "intervals_icu"):
        point = frame.get(row.date)
        if point:
            if point["sleep_h"] is None and row.duration_s is not None:
                point["sleep_h"] = row.duration_s / 3600
            if point["sleep_score"] is None and row.score is not None:
                point["sleep_score"] = float(row.score)
    for row in sorted(recoveries, key=lambda item: item.source != "intervals_icu"):
        point = frame.get(row.date)
        if point and point["hrv"] is None and row.hrv is not None:
            point["hrv"] = float(row.hrv)
    for row in activities:
        point = frame.get(row.date)
        if point and row.steps is not None:
            point["steps"] = max(point["steps"] or 0, row.steps)

    last_wellbeing: dict[dt.date, tuple[dt.datetime, int]] = {}
    for row in logs:
        payload = row.payload_json or {}
        try:
            day = dt.date.fromisoformat(payload["date"]) if payload.get("date") else local_date(row.logged_at)
        except (TypeError, ValueError):
            continue
        score = payload.get("score")
        if day in frame and isinstance(score, int) and not isinstance(score, bool) and 1 <= score <= 5:
            last_wellbeing[day] = (row.logged_at, score)
    for day, (_, score) in last_wellbeing.items():
        frame[day]["wellbeing"] = float(score)
    for row in body_rows:
        point = frame.get(local_date(row.measured_at))
        if point:
            if row.weight_kg is not None:
                point["weight_kg"] = row.weight_kg
            if row.fat_pct is not None:
                point["fat_pct"] = row.fat_pct
    for row in nutrition_rows:
        point = frame.get(row.date)
        if point:
            point["kcal"] = row.kcal
            point["protein_g"] = row.protein_g

    run_loads: dict[dt.date, list[float]] = defaultdict(list)
    run_efs: dict[dt.date, list[float]] = defaultdict(list)
    run_hours: dict[dt.date, list[float]] = defaultdict(list)
    run_distances: dict[dt.date, list[float]] = defaultdict(list)
    for workout in workouts:
        if workout.started_at is None or _is_walk(workout):
            continue
        day = local_date(workout.started_at)
        raw = workout.raw_json or {}
        if isinstance(raw.get("icu_training_load"), (int, float)):
            run_loads[day].append(float(raw["icu_training_load"]))
        if workout.distance_m is not None:
            run_distances[day].append(workout.distance_m / 1000)
        if (workout.distance_m or 0) >= MIN_ANALYSIS_KM * 1000:
            if (ef := _efficiency(workout)) is not None:
                run_efs[day].append(ef)
        local_start = workout.started_at.astimezone(app_timezone())
        run_hours[day].append(local_start.hour + local_start.minute / 60)
    for day in frame:
        if run_loads[day]:
            frame[day]["run_load"] = sum(run_loads[day])
        if run_distances[day]:
            frame[day]["run_km"] = sum(run_distances[day])
        if run_efs[day]:
            frame[day]["run_efficiency"] = statistics.mean(run_efs[day])
        if run_hours[day]:
            frame[day]["run_start_hour"] = max(run_hours[day])
    return [frame[day] for day in sorted(frame)]


def _values(rows: list[dict], key: str) -> list[tuple[float, float, str]]:
    by_day = {row["date"]: row for row in rows}
    pairs: list[tuple[float, float, str]] = []
    for row in rows:
        day = row["date"]
        if key == "sleep_hrv":
            x, y = row["sleep_h"], row["hrv"]
        elif key == "sleep_wellbeing":
            x, y = row["sleep_h"], row["wellbeing"]
        elif key == "sleep_run_efficiency":
            x, y = row["sleep_h"], row["run_efficiency"]
        elif key == "prior_load_hrv":
            loads = [
                by_day.get(day - dt.timedelta(days=lag), {}).get("run_load")
                for lag in (1, 2)
            ]
            x = sum(value for value in loads if value is not None) if any(
                value is not None for value in loads
            ) else None
            y = row["hrv"]
        elif key == "run_time_next_sleep":
            x = row["run_start_hour"]
            y = by_day.get(day + dt.timedelta(days=1), {}).get("sleep_score")
        else:
            raise ValueError(f"Nieznana para: {key}")
        if x is not None and y is not None:
            pairs.append((float(x), float(y), day.isoformat()))
    return pairs


def compute_correlations(rows: list[dict] | None = None) -> list[dict]:
    rows = rows or daily_frame()
    results = []
    for key, spec in PAIR_SPECS.items():
        values = _values(rows, key)
        rho = spearman([x for x, _, _ in values], [y for _, y, _ in values])
        if len(values) < MIN_SAMPLES or rho is None:
            status = "insufficient_data"
        elif abs(rho) >= PUBLISH_ABS_RHO:
            status = "qualifying"
        else:
            status = "below_threshold"
        results.append(
            {
                "pair_key": key,
                "label": spec.label,
                "domain": spec.domain,
                "n": len(values),
                "rho": round(rho, 3) if rho is not None else None,
                "status": status,
                "dates": [day for _, _, day in values],
            }
        )
    return results


def _observation_text(result: dict) -> str:
    direction = "dodatnia" if result["rho"] > 0 else "ujemna"
    return (
        f"Obserwacja eksploracyjna: {result['label']} wykazuje korelację "
        f"{direction} Spearmana rho={result['rho']:.2f} (n={result['n']}, "
        f"okno {WINDOW_DAYS} dni). To współwystępowanie, nie dowód przyczynowości; "
        "wynik może zależeć od braków danych i innych czynników."
    )


def publish_correlations(end_date: dt.date | None = None) -> list[dict]:
    end = end_date or local_today()
    start = end - dt.timedelta(days=WINDOW_DAYS - 1)
    results = compute_correlations(daily_frame(end, WINDOW_DAYS))
    now = dt.datetime.now(dt.timezone.utc)
    with get_session() as session:
        for result in results:
            existing = session.execute(
                select(CorrelationResult).where(
                    CorrelationResult.method_version == METHOD_VERSION,
                    CorrelationResult.pair_key == result["pair_key"],
                    CorrelationResult.period_end == end,
                )
            ).scalar_one_or_none()
            if existing is not None:
                result["result_id"] = existing.id
                result["knowledge_id"] = existing.knowledge_id
                continue
            previous = session.execute(
                select(CorrelationResult)
                .where(
                    CorrelationResult.method_version == METHOD_VERSION,
                    CorrelationResult.pair_key == result["pair_key"],
                    CorrelationResult.knowledge_id.isnot(None),
                )
                .order_by(CorrelationResult.period_end.desc())
                .limit(1)
            ).scalar_one_or_none()
            knowledge_id = None
            if result["status"] == "qualifying":
                knowledge = Knowledge(
                    domain=result["domain"],
                    kind="lekcja",
                    content=_observation_text(result),
                    event_date=end,
                    source_type="agent",
                    source_agent="correlations",
                    confidence="low",
                    active=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(knowledge)
                session.flush()
                knowledge_id = knowledge.id
                if previous and previous.knowledge_id:
                    old = session.get(Knowledge, previous.knowledge_id)
                    if old and old.active:
                        old.active = False
                        old.superseded_by = knowledge_id
            elif result["status"] == "below_threshold" and previous and previous.knowledge_id:
                old = session.get(Knowledge, previous.knowledge_id)
                if old and old.active:
                    old.active = False
            stored = CorrelationResult(
                method_version=METHOD_VERSION,
                pair_key=result["pair_key"],
                period_start=start,
                period_end=end,
                sample_count=result["n"],
                rho=result["rho"],
                status=result["status"],
                details_json={"label": result["label"], "dates": result["dates"]},
                knowledge_id=knowledge_id,
                created_at=now,
            )
            session.add(stored)
            session.flush()
            result["result_id"] = stored.id
            result["knowledge_id"] = knowledge_id
    return results


def get_correlations(days: int = WINDOW_DAYS) -> list[dict]:
    """Policz ustalone korelacje bez zapisu; wynik zawiera n, rho i status."""
    if days < MIN_SAMPLES:
        raise ValueError(f"days musi być >= {MIN_SAMPLES}")
    return compute_correlations(daily_frame(days=days))
