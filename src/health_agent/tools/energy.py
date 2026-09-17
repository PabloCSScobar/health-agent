"""Szacunek CAŁODNIOWEGO wydatku energetycznego - dana, której nie daje żadne
źródło (sprawdzone: Intervals.icu, Health Connect przez Suunto/Health Sync,
patrz scripts/README.md). Bez niej pytanie "jaki mam deficyt?" wymagało
ręcznego "spaliłem dziś X".

Priorytet źródeł dla danego dnia:
1. wpis ręczny użytkownika (daily_activity.source="manual") - jego liczba,
2. calories_total z Health Connect, jeśli kiedyś się pojawi,
3. SZACUNEK: BMR (Mifflin-St Jeor: waga z wagi Fitdays, wzrost/wiek/płeć z
   profilu) + kalorie z treningów tego dnia (zegarek) + NEAT z kroków poza
   treningiem + TEF (~10% spożycia, jeśli znane). Zawsze oznaczany jako
   szacunek z listą brakujących danych - model ma to powtórzyć użytkownikowi.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from health_agent.db.models import BodyComposition, DailyActivity, NutritionDay, Workout
from health_agent.db.session import get_session
from health_agent.tools.profile import _profile_number, get_user_profile

KCAL_PER_STEP_PER_KG = 0.00052  # ~0.045 kcal/krok przy 88 kg; marsz 4-5 km/h
RUN_STEPS_PER_KM = 1100  # przy ~165 spm i 6:30/km; do odjęcia kroków z biegu od NEAT


def _bmr_mifflin(weight_kg: float, height_cm: float, age: float, sex: str) -> float:
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base + 5 if sex.upper().startswith("M") else base - 161


def _latest_weight(on_or_before: dt.date) -> float | None:
    with get_session() as session:
        row = session.execute(
            select(BodyComposition)
            .where(BodyComposition.weight_kg.isnot(None), BodyComposition.measured_at < dt.datetime.combine(on_or_before + dt.timedelta(days=1), dt.time.min, tzinfo=dt.timezone.utc))
            .order_by(BodyComposition.measured_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        return row.weight_kg if row else None


def estimate_daily_expenditure(date: dt.date) -> dict:
    """Całodniowy wydatek energetyczny dla `date` (YYYY-MM-DD): wpis ręczny
    użytkownika jeśli jest, inaczej SZACUNEK = BMR + treningi + kroki + TEF.
    Zwraca `method` (manual/healthconnect/estimate), składowe i listę
    brakujących danych - jeśli `missing` niepuste, powiedz użytkownikowi, co
    podać do profilu (wzrost, wiek, płeć), żeby szacunek był pełny."""
    with get_session() as session:
        acts = session.execute(select(DailyActivity).where(DailyActivity.date == date)).scalars().all()
        manual = next((a.calories_total for a in acts if a.source == "manual" and a.calories_total), None)
        hc = next((a.calories_total for a in acts if a.source == "healthconnect" and a.calories_total), None)
        steps = max((a.steps or 0) for a in acts) if acts else 0
        start = dt.datetime.combine(date, dt.time.min, tzinfo=dt.timezone.utc)
        workouts = session.execute(
            select(Workout).where(Workout.started_at >= start, Workout.started_at < start + dt.timedelta(days=1))
        ).scalars().all()
        workout_kcal = sum((w.calories or 0) for w in workouts)
        run_km = sum((w.distance_m or 0) for w in workouts if w.sport in ("Run", "VirtualRun", "TrailRun")) / 1000
        nd = session.get(NutritionDay, date)
        intake = nd.kcal if nd else None

    if manual:
        return {"date": date.isoformat(), "method": "manual", "total_kcal": round(manual), "note": "wpisane przez użytkownika (z zegarka/apki)", "missing": []}

    weight = _latest_weight(date)
    height, age = _profile_number("wzrost_cm"), _profile_number("wiek")
    sex = get_user_profile().get("plec")
    missing = [k for k, v in (("waga (z wagi Fitdays)", weight), ("wzrost_cm w profilu", height), ("wiek w profilu", age), ("plec w profilu", sex)) if not v]
    bmr = _bmr_mifflin(weight, height, age, sex) if not missing else None

    # Health Connect `total_calories` bywa CZĘŚCIOWE - złapane na żywo: 275 kcal
    # "za dobę" = kalorie jednego marszu na bieżni zapisane przez apkę, nie
    # suma dobowa (żadna z apek użytkownika nie pisze prawdziwej sumy, patrz
    # scripts/README.md). Doba poniżej ~80% BMR jest fizycznie niemożliwa -
    # taką wartość ignorujemy i liczymy szacunek.
    if hc and hc >= (0.8 * bmr if bmr else 1200):
        return {"date": date.isoformat(), "method": "healthconnect", "total_kcal": round(hc), "missing": []}
    neat_steps = max(0, steps - run_km * RUN_STEPS_PER_KM)
    neat = neat_steps * (weight or 80) * KCAL_PER_STEP_PER_KG
    tef = 0.1 * intake if intake else 0
    total = (bmr + workout_kcal + neat + tef) if bmr else None
    return {
        "date": date.isoformat(),
        "method": "estimate",
        "total_kcal": round(total) if total else None,
        "components": {
            "bmr": round(bmr) if bmr else None,
            "workouts_kcal": round(workout_kcal),
            "workouts": [{"sport": w.sport, "kcal": w.calories} for w in workouts],
            "neat_from_steps_kcal": round(neat),
            "steps_total": steps,
            "steps_outside_workouts": round(neat_steps),
            "tef_kcal": round(tef),
        },
        "missing": missing,
        "ignored_healthconnect_total": round(hc) if hc else None,
        "note": "SZACUNEK (±10-15%): Mifflin-St Jeor + kalorie z zegarka + kroki + TEF. Powiedz to użytkownikowi." if total else "nie da się policzyć BMR - brak: " + ", ".join(missing),
    }
