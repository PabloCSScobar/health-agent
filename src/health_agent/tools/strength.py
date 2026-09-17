"""Narzędzia StrengthCoach - treningi siłowe z ręcznych wpisów (manual_logs,
kind="strength") + sesje "WeightTraining" z zegarka (Intervals.icu: czas,
tętno, bez ćwiczeń).

Kanoniczny schemat payloadu wpisu (orchestrator ma to w prompcie):
    {"cwiczenia": [
        {"nazwa": "wyciskanie sztangi", "serie": 4, "powtorzenia": 8, "ciezar_kg": 80},
        {"nazwa": "dipy", "serie": 3, "powtorzenia": 12},                # bez ciężaru = masa ciała
        {"nazwa": "przysiad", "serie": [{"powtorzenia": 5, "ciezar_kg": 100}, {"powtorzenia": 5, "ciezar_kg": 105}]}
    ], "notatka": "opcjonalnie"}
Parser toleruje polskie znaki i synonimy (ćwiczenia/exercises, powtórzenia/
reps, ciężar_kg/waga_kg/kg), bo model nie zawsze trzyma się schematu.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections import defaultdict

from sqlalchemy import select

from health_agent.db.models import ManualLog, Workout
from health_agent.db.session import get_session

MUSCLE_GROUPS = {
    "klatka": ("wyciskanie", "rozpiętki", "rozpietki", "dipy", "pompki", "klatka", "bench"),
    "plecy": ("wiosłowanie", "wioslowanie", "podciąganie", "podciaganie", "martwy", "ściąganie", "sciaganie", "plecy", "row", "pull"),
    "nogi": ("przysiad", "wykroki", "wypychanie", "nogi", "uda", "łydki", "lydki", "squat", "leg", "hip thrust", "pośladk", "posladk"),
    "barki": ("żołnierskie", "zolnierskie", "ohp", "barki", "unoszenie", "arnold", "press nad"),
    "biceps": ("biceps", "uginanie", "curl"),
    "triceps": ("triceps", "francuskie", "prostowanie ramion"),
    "brzuch": ("brzuch", "plank", "deska", "spięcia", "spiecia", "unoszenie nóg", "core"),
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower()).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip()


def _muscle(name: str) -> str:
    n = name.lower()
    for group, keys in MUSCLE_GROUPS.items():
        if any(k in n for k in keys):
            return group
    return "inne"


def _first(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _parse_sets(ex: dict) -> list[dict]:
    """-> [{"reps": int, "kg": float|None}, ...]"""
    serie = _first(ex, "serie", "sets", "sety")
    reps = _first(ex, "powtorzenia", "powtórzenia", "reps", "powt")
    kg = _first(ex, "ciezar_kg", "ciężar_kg", "waga_kg", "kg", "ciezar", "ciężar", "obciazenie", "obciążenie")
    if isinstance(serie, list):
        out = []
        for s_ in serie:
            if isinstance(s_, dict):
                out.append({"reps": int(_first(s_, "powtorzenia", "powtórzenia", "reps", default=reps or 0)), "kg": _first(s_, "ciezar_kg", "ciężar_kg", "waga_kg", "kg", default=kg)})
            else:
                out.append({"reps": int(s_), "kg": kg})
        return out
    try:
        n = int(serie or 1)
    except (TypeError, ValueError):
        n = 1
    try:
        r = int(reps or 0)
    except (TypeError, ValueError):
        r = 0
    return [{"reps": r, "kg": float(kg) if kg is not None else None} for _ in range(n)]


def _parse_log(log: ManualLog) -> dict | None:
    p = log.payload_json or {}
    exs = _first(p, "cwiczenia", "ćwiczenia", "exercises", "cwiczenie", default=None)
    if isinstance(p, list):
        exs = p
    if not exs:
        return None
    if isinstance(exs, dict):
        exs = [exs]
    exercises = []
    for ex in exs:
        if not isinstance(ex, dict):
            continue
        name = str(_first(ex, "nazwa", "name", "cwiczenie", "ćwiczenie", default="?"))
        sets = _parse_sets(ex)
        volume = sum(s["reps"] * (s["kg"] or 0) for s in sets)
        best = max(sets, key=lambda s: ((s["kg"] or 0), s["reps"]), default=None)
        exercises.append({
            "name": name, "muscle": _muscle(name), "sets": len(sets), "reps_total": sum(s["reps"] for s in sets),
            "top_set": best, "volume_kg": round(volume), "sets_detail": sets,
            "e1rm_kg": round(best["kg"] * (1 + best["reps"] / 30), 1) if best and best["kg"] and 0 < best["reps"] <= 12 else None,
        })
    return {"date": log.logged_at.date().isoformat(), "log_id": log.id, "exercises": exercises,
            "volume_kg": round(sum(e["volume_kg"] for e in exercises)), "sets": sum(e["sets"] for e in exercises),
            "note": p.get("notatka") or p.get("note"), "text": log.text_original}


def _sessions(days: int) -> list[dict]:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    with get_session() as session:
        logs = session.execute(
            select(ManualLog).where(ManualLog.kind == "strength", ManualLog.logged_at >= since).order_by(ManualLog.logged_at)
        ).scalars().all()
        parsed = [s for s in (_parse_log(lg) for lg in logs) if s]
    return parsed


def get_strength_sessions(days: int = 90) -> dict:
    """ZACZNIJ OD TEGO. Sesje siłowe z ostatnich `days` dni: z ręcznych
    wpisów (ćwiczenia, serie x powtórzenia x kg, objętość, e1RM) oraz - do
    porównania - sesje "WeightTraining" z zegarka (data, czas, tętno; bez
    ćwiczeń). Dni z zegarka BEZ wpisu ręcznego = trening był, ale nie ma
    szczegółów - zachęć do wpisania, nie zgaduj co robił. Zwraca też
    liczbę sesji/tydz. i dni od ostatniej."""
    parsed = _sessions(days)
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    with get_session() as session:
        watch = session.execute(
            select(Workout).where(Workout.sport == "WeightTraining", Workout.started_at >= since).order_by(Workout.started_at)
        ).scalars().all()
        watch_rows = [{"date": w.started_at.date().isoformat(), "min": round((w.duration_s or 0) / 60), "avg_hr": w.avg_hr, "max_hr": w.max_hr} for w in watch]
    manual_dates = {s["date"] for s in parsed}
    for wr in watch_rows:
        wr["has_manual_log"] = wr["date"] in manual_dates
    all_dates = sorted(manual_dates | {w["date"] for w in watch_rows})
    last = dt.date.fromisoformat(all_dates[-1]) if all_dates else None
    weeks = max(1, days / 7)
    return {
        "days": days,
        "manual_sessions": [{k: v for k, v in s.items() if k != "text"} for s in parsed][-12:],
        "watch_sessions_without_details": [w for w in watch_rows if not w["has_manual_log"]][-12:],
        "sessions_total": len(all_dates),
        "sessions_per_week": round(len(all_dates) / weeks, 1),
        "days_since_last": (dt.date.today() - last).days if last else None,
        "note": "e1RM (Epley) tylko dla serii <=12 powt. z ciężarem; objętość = suma powt. x kg (masa ciała liczona jako 0)",
    }


def get_exercise_progress(exercise: str, days: int = 180) -> dict:
    """Progresja JEDNEGO ćwiczenia (dopasowanie po fragmencie nazwy, bez
    polskich znaków - 'wyciskanie', 'przysiad'): per sesja najcięższa seria,
    e1RM, objętość, serie; rekord (PR) e1RM z datą; zmiana e1RM pierwsza vs
    ostatnia sesja. Do pytań "czy robię postępy w X", "jaki mam rekord"."""
    q = _norm(exercise)
    hist = []
    for s in _sessions(days):
        for e in s["exercises"]:
            if q in _norm(e["name"]):
                hist.append({"date": s["date"], "name": e["name"], "sets": e["sets"], "top_set": e["top_set"], "e1rm_kg": e["e1rm_kg"], "volume_kg": e["volume_kg"]})
    if not hist:
        return {"exercise": exercise, "sessions": 0, "note": "brak wpisów z tym ćwiczeniem w tym okresie"}
    with_e1rm = [h for h in hist if h["e1rm_kg"]]
    pr = max(with_e1rm, key=lambda h: h["e1rm_kg"]) if with_e1rm else None
    return {
        "exercise": exercise, "sessions": len(hist), "history": hist[-10:],
        "pr_e1rm": {"kg": pr["e1rm_kg"], "date": pr["date"], "top_set": pr["top_set"]} if pr else None,
        "e1rm_change_first_to_last_kg": round(with_e1rm[-1]["e1rm_kg"] - with_e1rm[0]["e1rm_kg"], 1) if len(with_e1rm) > 1 else None,
        "volume_change_first_to_last_pct": round((hist[-1]["volume_kg"] - hist[0]["volume_kg"]) / hist[0]["volume_kg"] * 100) if len(hist) > 1 and hist[0]["volume_kg"] else None,
    }


def get_strength_weekly_volume(weeks: int = 8) -> dict:
    """Tydzień po tygodniu: sesje, serie łącznie, objętość (kg), serie per
    grupa mięśniowa (klatka/plecy/nogi/barki/biceps/triceps/brzuch/inne -
    mapowanie po nazwie ćwiczenia, przybliżone). Punkt odniesienia:
    ~10-20 serii roboczych na grupę na tydzień dla hipertrofii, <10 =
    podtrzymanie. Do pytań "czy trenuję wystarczająco nogi", "ile serii
    robię na klatkę"."""
    sessions = _sessions(weeks * 7 + 7)
    by_week: dict[dt.date, list[dict]] = defaultdict(list)
    for s in sessions:
        d = dt.date.fromisoformat(s["date"])
        by_week[d - dt.timedelta(days=d.weekday())].append(s)
    today = dt.date.today()
    this_monday = today - dt.timedelta(days=today.weekday())
    out = []
    for wk in sorted(by_week)[-weeks:]:
        ss = by_week[wk]
        sets_by_muscle: dict[str, int] = defaultdict(int)
        for s in ss:
            for e in s["exercises"]:
                sets_by_muscle[e["muscle"]] += e["sets"]
        out.append({"week_start": wk.isoformat(), "partial_current_week": wk == this_monday, "sessions": len(ss), "sets": sum(s["sets"] for s in ss),
                    "volume_kg": sum(s["volume_kg"] for s in ss), "sets_by_muscle": dict(sets_by_muscle)})
    return {"weeks": out, "guidance": "10-20 serii/grupa/tydz. = wzrost; progresja: +1 powt. lub +2.5 kg gdy wszystkie serie wychodzą; deload co 4-8 tyg."}
