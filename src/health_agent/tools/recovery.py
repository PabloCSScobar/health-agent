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


# ---------------------------------------------------------------------------
# Analityka: baza 7/28 dni, z-score, readiness - to, co analityk regeneracji
# policzyłby sam, zamiast czytać 14 surowych wierszy i "interpretować".
# ---------------------------------------------------------------------------

import statistics  # noqa: E402

SLEEP_TARGET_H = 7.5


def _stats(values: list[float]) -> dict:
    vals = [v for v in values if v is not None]
    if not vals:
        return {"mean": None, "sd": None, "n": 0}
    return {"mean": round(statistics.mean(vals), 1), "sd": round(statistics.pstdev(vals), 1) if len(vals) > 1 else 0.0, "n": len(vals)}


def _z(value: float | None, st: dict) -> float | None:
    if value is None or st["mean"] is None or not st["sd"]:
        return None
    return round((value - st["mean"]) / st["sd"], 2)


def get_recovery_baseline(days: int = 28) -> dict:
    """ZACZNIJ OD TEGO. Dla HRV, tętna spoczynkowego, snu (czas, score) i
    kroków: najnowsza wartość, średnia 7 dni, baza `days` dni (średnia ± SD),
    z-score najnowszej vs baza, trend (7d vs baza). Plus dług snu z 7 dni,
    forma CTL/ATL/TSB (jak w Intervals.icu), flagi ostrzegawcze (RHR +5 nad
    bazą, HRV < baza-1SD, sen <6h) i heurystyczny readiness. Zastępuje
    wołanie get_recovery_day kilka razy - jedno wywołanie, gotowe
    porównania. Wartości interpretuj WYŁĄCZNIE względem bazy tego
    użytkownika (HRV 60 może być świetne dla jednego, słabe dla innego)."""
    points = _recovery_points(dt.date.today() - dt.timedelta(days=days), dt.date.today())
    if not points:
        return {"note": "brak danych regeneracyjnych"}
    last = points[-1]
    last7 = points[-7:]

    def series(attr):
        return [getattr(p, attr) for p in points]

    out: dict = {"as_of": last.date.isoformat(), "days_in_baseline": len(points), "metrics": {}}
    for attr, label, good in (("hrv", "hrv_ms", "wyżej=lepiej"), ("resting_hr", "resting_hr_bpm", "niżej=lepiej"), ("sleep_score", "sleep_score", "wyżej=lepiej"), ("steps", "steps", "aktywność")):
        base = _stats(series(attr))
        week = _stats([getattr(p, attr) for p in last7])
        latest = getattr(last, attr)
        out["metrics"][label] = {
            "latest": latest,
            "avg_7d": week["mean"],
            "baseline_mean": base["mean"],
            "baseline_sd": base["sd"],
            "z_latest_vs_baseline": _z(latest, base),
            "trend_7d_vs_baseline": round(week["mean"] - base["mean"], 1) if week["mean"] is not None and base["mean"] is not None else None,
            "direction": good,
        }
    sleep_h = [p.sleep_duration_s / 3600 for p in points if p.sleep_duration_s]
    # zaokrąglone do 0.1 h - te same liczby, które pokazujemy w last_7_days,
    # żeby dług snu zgadzał się z tym, co model (i użytkownik) może przeliczyć
    sleep7 = [round(p.sleep_duration_s / 3600, 1) for p in last7 if p.sleep_duration_s]
    out["sleep_hours"] = {
        "latest": round(last.sleep_duration_s / 3600, 1) if last.sleep_duration_s else None,
        "avg_7d": round(statistics.mean(sleep7), 1) if sleep7 else None,
        "baseline_mean": round(statistics.mean(sleep_h), 1) if sleep_h else None,
        "debt_7d_h_vs_target": round(sum(SLEEP_TARGET_H - h for h in sleep7), 1) if sleep7 else None,
        "target_h": SLEEP_TARGET_H,
        "last_7_days": [{"date": p.date.isoformat(), "h": round(p.sleep_duration_s / 3600, 1) if p.sleep_duration_s else None, "score": p.sleep_score} for p in last7],
    }

    from health_agent.tools.running import _form_summary, _latest_wellness  # noqa: PLC0415 - unikamy cyklu importów na poziomie modułu

    w = _latest_wellness() or {}
    out["form"] = _form_summary(w.get("ctl"), w.get("atl"))

    flags = []
    m = out["metrics"]
    if m["resting_hr_bpm"]["latest"] and m["resting_hr_bpm"]["baseline_mean"] and m["resting_hr_bpm"]["latest"] >= m["resting_hr_bpm"]["baseline_mean"] + 5:
        flags.append("tętno spoczynkowe >= +5 nad bazą")
    if (z := m["hrv_ms"]["z_latest_vs_baseline"]) is not None and z <= -1:
        flags.append("HRV poniżej bazy o >=1 SD")
    if out["sleep_hours"]["latest"] is not None and out["sleep_hours"]["latest"] < 6:
        flags.append("sen <6h")
    if out["sleep_hours"]["debt_7d_h_vs_target"] is not None and out["sleep_hours"]["debt_7d_h_vs_target"] > 5:
        flags.append("dług snu >5h w tygodniu")
    # Readiness: prosta suma z-score (HRV +, RHR -, sen +), 3 poziomy - heurystyka, nie nauka.
    zs = [m["hrv_ms"]["z_latest_vs_baseline"], -(m["resting_hr_bpm"]["z_latest_vs_baseline"] or 0) if m["resting_hr_bpm"]["z_latest_vs_baseline"] is not None else None, m["sleep_score"]["z_latest_vs_baseline"]]
    zs = [x for x in zs if x is not None]
    score = round(sum(zs) / len(zs), 2) if zs else None
    out["readiness"] = {
        "composite_z": score,
        "label": None if score is None else ("wysoka - dzień na mocny bodziec" if score > 0.3 else "normalna - trening wg planu" if score > -0.5 else "obniżona - dzień lekki/wolny"),
        "flags": flags,
        "note": "heurystyka z z-score HRV/RHR/snu vs baza; jedna zła noc to nie alarm, 3 dni z rzędu poniżej bazy - tak",
    }
    return out
