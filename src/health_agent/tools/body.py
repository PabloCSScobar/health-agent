"""Narzędzia odczytu wagi/składu ciała - wołane przez BodyCompCoach."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel
from sqlalchemy import select

from health_agent.db.models import BodyComposition
from health_agent.db.session import get_session
from health_agent.time_utils import local_date, local_today, utc_day_bounds


class BodyCompositionPoint(BaseModel):
    measured_at: dt.datetime
    source: str
    weight_kg: float | None
    fat_pct: float | None
    muscle_kg: float | None
    bone_kg: float | None


def get_body_composition_latest() -> BodyCompositionPoint | None:
    with get_session() as session:
        row = session.execute(
            select(BodyComposition).order_by(BodyComposition.measured_at.desc()).limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        return BodyCompositionPoint(
            measured_at=row.measured_at, source=row.source, weight_kg=row.weight_kg,
            fat_pct=row.fat_pct, muscle_kg=row.muscle_kg, bone_kg=row.bone_kg,
        )


def get_body_composition_trend(days: int = 30) -> list[BodyCompositionPoint]:
    since, _ = utc_day_bounds(local_today() - dt.timedelta(days=days))
    with get_session() as session:
        rows = session.execute(
            select(BodyComposition).where(BodyComposition.measured_at >= since).order_by(BodyComposition.measured_at)
        ).scalars().all()
        return [
            BodyCompositionPoint(
                measured_at=r.measured_at, source=r.source, weight_kg=r.weight_kg,
                fat_pct=r.fat_pct, muscle_kg=r.muscle_kg, bone_kg=r.bone_kg,
            )
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Analityka: średnia krocząca, nachylenie kg/tydz., porównanie z bilansem
# energetycznym - pojedynczy odczyt z wagi BIA to szum (±1-2 kg wody), liczy
# się trend.
# ---------------------------------------------------------------------------

import statistics  # noqa: E402

from health_agent.tools.nutrition import get_energy_balance  # noqa: E402
from health_agent.tools.profile import _profile_number  # noqa: E402

MIN_POINTS_FOR_TREND = 3
MIN_SPAN_DAYS_FOR_TREND = 7


def get_body_trend(days: int = 30) -> dict:
    """ZACZNIJ OD TEGO. Pomiary z ostatnich `days` dni (jeden na dzień -
    ostatni z danego dnia), średnia krocząca 7 dni, nachylenie trendu wagi
    w kg/tydzień (regresja liniowa - liczona TYLKO gdy >=3 pomiary
    rozpięte na >=7 dni, inaczej None z powodem), zmiana % tłuszczu
    pierwszy->ostatni, dystans do celu z profilu, oraz porównanie
    obserwowanej zmiany z OCZEKIWANĄ z bilansu energetycznego (7700 kcal ≈
    1 kg). Pole `data_sufficiency` mówi wprost, czy to już trend, czy
    dopiero pojedyncze punkty - nie wyciągaj wniosków z 2 pomiarów."""
    since, _ = utc_day_bounds(local_today() - dt.timedelta(days=days))
    with get_session() as session:
        rows = session.execute(
            select(BodyComposition).where(BodyComposition.measured_at >= since, BodyComposition.weight_kg.isnot(None)).order_by(BodyComposition.measured_at)
        ).scalars().all()
        session.expunge_all()
    by_day: dict[dt.date, BodyComposition] = {}
    for r in rows:
        by_day[local_date(r.measured_at)] = r  # ostatni pomiar dnia wygrywa
    pts = [by_day[d] for d in sorted(by_day)]
    if not pts:
        return {"days": days, "points": 0, "data_sufficiency": "brak pomiarów w tym okresie"}

    weights = [(local_date(p.measured_at), p.weight_kg) for p in pts]
    first_day = weights[0][0]
    span = (weights[-1][0] - first_day).days
    slope = None
    if len(weights) >= MIN_POINTS_FOR_TREND and span >= MIN_SPAN_DAYS_FOR_TREND:
        xs = [(d - first_day).days for d, _ in weights]
        ys = [w for _, w in weights]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        denom = sum((x - mx) ** 2 for x in xs)
        slope = round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom * 7, 2) if denom else None

    rolling = []
    for i, (d, _) in enumerate(weights):
        window = [w for dd, w in weights if 0 <= (d - dd).days < 7]
        rolling.append({"date": d.isoformat(), "weight_kg": round(weights[i][1], 1), "avg_7d": round(statistics.mean(window), 1), "fat_pct": round(pts[i].fat_pct, 1) if pts[i].fat_pct else None, "source": pts[i].source})

    fat_first = next((p.fat_pct for p in pts if p.fat_pct), None)
    fat_last = next((p.fat_pct for p in reversed(pts) if p.fat_pct), None)
    goal = _profile_number("cel_waga_kg")
    latest_w = weights[-1][1]
    balance = get_energy_balance(min(days, span + 1) if span else 7)
    observed = round(weights[-1][1] - weights[0][1], 2) if len(weights) > 1 else None
    return {
        "days": days,
        "points": len(weights),
        "span_days": span,
        "data_sufficiency": (
            "OK - trend policzony" if slope is not None else
            f"za mało danych na trend: {len(weights)} pomiar(y) na {span} dni (potrzeba >={MIN_POINTS_FOR_TREND} na >={MIN_SPAN_DAYS_FOR_TREND} dni) - podawaj tylko aktualną wagę, nie 'trend'"
        ),
        "latest": {"date": weights[-1][0].isoformat(), "weight_kg": round(latest_w, 1), "fat_pct": round(pts[-1].fat_pct, 1) if pts[-1].fat_pct else None, "muscle_kg": pts[-1].muscle_kg, "bone_kg": pts[-1].bone_kg},
        "avg_7d_kg": rolling[-1]["avg_7d"],
        "slope_kg_per_week": slope,
        "observed_change_kg": observed,
        "fat_pct_change": round(fat_last - fat_first, 1) if fat_first is not None and fat_last is not None else None,
        "goal": {"target_kg": goal, "to_go_kg": round(latest_w - goal, 1) if goal else None, "weeks_at_current_slope": round((latest_w - goal) / abs(slope), 1) if goal and slope and (latest_w - goal) * slope < 0 else None},
        "expected_from_energy_balance": {"days_with_data": balance["days_with_data"], "sum_balance_kcal": balance["sum_balance_kcal"], "implied_kg_change": balance["implied_kg_change"], "missing": balance["missing_for_estimate"]},
        "series": rolling[-14:],
        "note": "waga BIA: ±1-2 kg dziennie od wody/glikogenu/soli; %tłuszczu z BIA zmienia się realnie o ~0.1-0.2 pkt/tydz., większe skoki to szum",
    }
