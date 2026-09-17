"""Narzędzia analityczne RunningCoach - liczą w Pythonie to, co trener
policzyłby sam (strefy, obciążenie tygodniowe, rozkład intensywności,
efektywność, dryf tętna, porównania), zamiast wrzucać modelowi surową listę
treningów "do interpretacji". Model dostaje gotowe liczby WZGLĘDEM bazy
użytkownika (z-score, % zmiany, pasmo), bo to jest to, na czym polega
trenerska interpretacja - nie na czytaniu wartości bezwzględnych.

Źródło wszystkich danych: `workouts.raw_json` z Intervals.icu (sprawdzone
na prawdziwych rekordach, patrz scripts/README.md): `icu_hr_zones` (7 progów
wg Friela z LTHR), `icu_hr_zone_times` (sekundy w strefach),
`icu_training_load` (HRSS), `icu_ctl`/`icu_atl` w momencie treningu,
`athlete_max_hr`, `lthr`, `trainer`, `feel` (1-5), `interval_summary`.
Dzienne CTL/ATL/rampRate: `recovery.raw_json` z endpointu /wellness.
Strumienie (tętno/prędkość sekunda po sekundzie) - na żądanie z API
Intervals.icu (`/activity/{id}/streams`), bo nie trzymamy ich w bazie.

WAŻNE - rzeczywistość danych użytkownika (sprawdzone 2026-09-16):
"VirtualRun" z bieżni to MARSZE (tempo 14-15 min/km, tętno ~80), nie biegi.
Wliczanie ich do średnich tempa/tętna psuje każdą analizę. Stąd `_is_walk`
i konsekwentne rozdzielanie "biegów" od "marszów" we wszystkich narzędziach.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict

import requests
from sqlalchemy import select

from health_agent.db.models import Recovery, Workout
from health_agent.db.session import get_session
from health_agent.settings import settings

RUN_SPORTS = ("Run", "VirtualRun", "TrailRun")
WALK_PACE_THRESHOLD_S_KM = 540  # wolniej niż 9:00/km = marsz, nie bieg
WALK_HR_THRESHOLD = 105
MIN_ANALYSIS_KM = 3  # krótsze "biegi" (dobieg do bramy, test zegarka) zaśmiecają EF i klasyfikację sesji

# Friel, 7 stref od LTHR - dokładnie tak liczy je Intervals.icu (progi w
# `icu_hr_zones` to górne granice stref 1..7). Do analizy 80/20 zbijamy je do
# trzech: łatwo (Z1-Z2, < 90% LTHR), umiarkowanie (Z3, tempo), mocno (Z4+).
ZONE_NAMES = ["Z1 regeneracja", "Z2 aerobowa", "Z3 tempo", "Z4 podprogowa", "Z5a progowa", "Z5b VO2max", "Z5c anaerobowa"]

# Strefy Form (TSB = CTL - ATL) wg Joe Friela; Intervals.icu stosuje te same
# liczby do "Form %" (TSB / CTL * 100), które pokazuje domyślnie - dlatego
# zwracamy obie wersje, żeby coach mówił tym samym językiem co apka.
FORM_BANDS = [
    (25, None, "transition (>+25): odpoczynek, tracisz formę"),
    (5, 25, "fresh (+5..+25): świeży - dzień startu / po deloadzie"),
    (-10, 5, "grey zone (-10..+5): plateau, mało bodźca"),
    (-30, -10, "optimal (-30..-10): produktywne obciążenie"),
    (None, -30, "high risk (<-30): przeciążenie / ryzyko kontuzji lub choroby"),
]


def _form_band(tsb: float) -> str:
    for lo, hi, label in FORM_BANDS:
        if (lo is None or tsb >= lo) and (hi is None or tsb < hi):
            return label
    return "?"


def _pace_str(sec_per_km: float | None) -> str | None:
    if not sec_per_km:
        return None
    m, s = divmod(int(round(sec_per_km)), 60)
    return f"{m}:{s:02d}/km"


def _is_walk(w: Workout) -> bool:
    if w.avg_pace and w.avg_pace > WALK_PACE_THRESHOLD_S_KM:
        return True
    if w.avg_hr and w.avg_hr < WALK_HR_THRESHOLD:
        return True
    return False


def _raw(w: Workout, key: str, default=None):
    return (w.raw_json or {}).get(key, default)


def _efficiency(w: Workout) -> float | None:
    """Efficiency Factor = prędkość [m/min] / średnie tętno. Rośnie = ta sama
    prędkość przy niższym tętnie = lepsza baza aerobowa. Tylko dla biegów
    ze stałym wysiłkiem ma sens; dla interwałów jest zaszumiony."""
    if not (w.distance_m and w.duration_s and w.avg_hr):
        return None
    speed_m_min = w.distance_m / (w.duration_s / 60)
    return round(speed_m_min / w.avg_hr, 3)


def _run_row(w: Workout) -> dict:
    return {
        "external_id": w.external_id,
        "date": w.started_at.date().isoformat() if w.started_at else None,
        "type": w.sport,
        "is_walk": _is_walk(w),
        "trainer": bool(_raw(w, "trainer")),
        "km": round((w.distance_m or 0) / 1000, 2),
        "moving_min": round((w.duration_s or 0) / 60),
        "pace": _pace_str(w.avg_pace),
        "pace_s_km": round(w.avg_pace) if w.avg_pace else None,
        "avg_hr": w.avg_hr,
        "max_hr": w.max_hr,
        "load": _raw(w, "icu_training_load"),
        "efficiency": _efficiency(w),
        "ascent_m": round(w.ascent_m) if w.ascent_m else 0,
        "feel_1_5": _raw(w, "feel"),
    }


def _load_runs(days: int, include_walks: bool = False) -> list[Workout]:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    with get_session() as session:
        rows = session.execute(
            select(Workout)
            .where(Workout.sport.in_(RUN_SPORTS), Workout.started_at >= since)
            .order_by(Workout.started_at)
        ).scalars().all()
        # raw_json jest ładowane leniwie? Nie - JSON column, jest w wierszu.
        session.expunge_all()
    return [w for w in rows if include_walks or not _is_walk(w)]


def _load_all_workouts(days: int) -> list[Workout]:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    with get_session() as session:
        rows = session.execute(
            select(Workout).where(Workout.started_at >= since).order_by(Workout.started_at)
        ).scalars().all()
        session.expunge_all()
    return rows


def _latest_wellness() -> dict | None:
    with get_session() as session:
        row = session.execute(select(Recovery).order_by(Recovery.date.desc()).limit(1)).scalar_one_or_none()
        if row is None:
            return None
        raw = row.raw_json or {}
        return {"date": row.date.isoformat(), "ctl": raw.get("ctl"), "atl": raw.get("atl"), "ramp_rate": raw.get("rampRate")}


def _form_summary(ctl: float | None, atl: float | None) -> dict:
    if ctl is None or atl is None:
        return {"ctl": ctl, "atl": atl, "tsb": None, "form_pct": None, "band": "brak danych"}
    tsb = ctl - atl
    form_pct = (tsb / ctl * 100) if ctl else None
    # Przy niskim CTL (mała baza - początkujący / powrót po przerwie) Form %
    # eksploduje (TSB -8 przy CTL 19 = -44%) i wskazuje "high risk", podczas
    # gdy bezwzględne TSB mówi "szara strefa". Progi Friela powstały dla
    # bezwzględnego TSB przy CTL rzędu 50-100 - poniżej ~30 ufaj TSB, nie %.
    low_ctl = ctl < 30
    return {
        "ctl_fitness": round(ctl, 1),
        "atl_fatigue": round(atl, 1),
        "tsb_form": round(tsb, 1),
        "band_by_tsb": _form_band(tsb),
        "form_pct": round(form_pct, 1) if form_pct is not None else None,
        "band_by_form_pct": _form_band(form_pct) if form_pct is not None else None,
        "use": "band_by_tsb (CTL<30: Form % przesadza, apka Intervals.icu pokaże czerwono - wyjaśnij to)" if low_ctl else "band_by_form_pct (tak jak pokazuje Intervals.icu)",
    }


def get_running_profile() -> dict:
    """Profil biegowy użytkownika - ZACZNIJ OD TEGO przy każdej analizie:
    strefy tętna (Friel, z LTHR, dokładnie jak w Intervals.icu), max HR,
    tętno spoczynkowe, waga, aktualna forma (CTL/ATL/TSB), baza objętości
    z ostatnich 4 pełnych tygodni, typowe tempo łatwe, najdłuższy bieg z 90
    dni, dni od ostatniego biegu. Bez tego nie wiesz, co dla TEGO biegacza
    jest "dużo", "szybko" ani "mocno"."""
    runs = _load_runs(90)
    walks = [w for w in _load_runs(90, include_walks=True) if _is_walk(w)]
    latest_with_zones = next((w for w in reversed(runs) if _raw(w, "icu_hr_zones")), None)

    zones = []
    lthr = max_hr = resting_hr = weight = None
    if latest_with_zones:
        bounds = _raw(latest_with_zones, "icu_hr_zones") or []
        lthr = _raw(latest_with_zones, "lthr")
        max_hr = _raw(latest_with_zones, "athlete_max_hr")
        resting_hr = _raw(latest_with_zones, "icu_resting_hr")
        weight = _raw(latest_with_zones, "icu_weight")
        lo = 0
        for i, hi in enumerate(bounds):
            zones.append({"zone": ZONE_NAMES[i] if i < len(ZONE_NAMES) else f"Z{i+1}", "from_hr": lo, "to_hr": hi})
            lo = hi + 1

    today = dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    weekly_km: dict[dt.date, float] = defaultdict(float)
    weekly_runs: dict[dt.date, int] = defaultdict(int)
    for w in runs:
        d = w.started_at.date()
        wk = d - dt.timedelta(days=d.weekday())
        if wk < monday:  # tylko pełne tygodnie
            weekly_km[wk] += (w.distance_m or 0) / 1000
            weekly_runs[wk] += 1
    last4 = sorted(weekly_km)[-4:]
    base_km = statistics.mean([weekly_km[k] for k in last4]) if last4 else 0
    base_runs = statistics.mean([weekly_runs[k] for k in last4]) if last4 else 0

    easy_upper = zones[1]["to_hr"] if len(zones) > 1 else None
    easy_paces = [w.avg_pace for w in runs if w.avg_pace and w.avg_hr and easy_upper and w.avg_hr <= easy_upper]
    all_paces = [w.avg_pace for w in runs if w.avg_pace]
    longest = max(runs, key=lambda w: w.distance_m or 0) if runs else None
    last_run = runs[-1] if runs else None

    wellness = _latest_wellness() or {}
    return {
        "hr_zones_friel_7": zones,
        "lthr": lthr,
        "max_hr": max_hr,
        "resting_hr_latest": resting_hr,
        "weight_kg": weight,
        "form_today": {**_form_summary(wellness.get("ctl"), wellness.get("atl")), "ramp_rate_ctl_per_week": round(wellness["ramp_rate"], 1) if wellness.get("ramp_rate") is not None else None, "as_of": wellness.get("date")},
        "base_last_4_full_weeks": {"avg_km_per_week": round(base_km, 1), "avg_runs_per_week": round(base_runs, 1), "weeks_counted": len(last4)},
        "typical_easy_pace": _pace_str(statistics.median(easy_paces)) if easy_paces else None,
        "typical_pace_all_runs": _pace_str(statistics.median(all_paces)) if all_paces else None,
        "longest_run_90d": _run_row(longest) if longest else None,
        "last_run": _run_row(last_run) if last_run else None,
        "days_since_last_run": (today - last_run.started_at.date()).days if last_run else None,
        "runs_90d": len(runs),
        "treadmill_walks_90d": {"count": len(walks), "km": round(sum((w.distance_m or 0) for w in walks) / 1000, 1),
                                "note": "VirtualRun z bieżni o tempie >9:00/km lub HR<105 = marsz; liczony osobno, nie jako bieg"},
    }


def get_weekly_running_load(weeks: int = 8) -> dict:
    """Tydzień po tygodniu (pon-nd, bieżący tydzień oznaczony jako
    niepełny): liczba biegów, km, minuty, obciążenie (suma HRSS), średnie
    tętno, przewyższenie, najdłuższy bieg, marsze osobno, zmiana km i
    obciążenia vs poprzedni tydzień. Plus ACWR (acute:chronic = obciążenie
    z 7 dni / średnie tygodniowe z 28 dni) osobno dla biegania i dla
    WSZYSTKICH sportów (rower, siłownia też męczą). Do pytań "jak wygląda
    mój tydzień", "czy nie robię za dużo", "progresja objętości"."""
    days = weeks * 7 + 7
    runs = _load_runs(days, include_walks=True)
    all_w = _load_all_workouts(35)
    today = dt.date.today()
    this_monday = today - dt.timedelta(days=today.weekday())

    by_week: dict[dt.date, list[Workout]] = defaultdict(list)
    for w in runs:
        d = w.started_at.date()
        by_week[d - dt.timedelta(days=d.weekday())].append(w)

    weeks_out = []
    prev = None
    for wk in sorted(by_week)[-weeks:]:
        ws = by_week[wk]
        real = [w for w in ws if not _is_walk(w)]
        walks = [w for w in ws if _is_walk(w)]
        km = sum((w.distance_m or 0) for w in real) / 1000
        mins = sum((w.duration_s or 0) for w in real) / 60
        load = sum((_raw(w, "icu_training_load") or 0) for w in real)
        hr_w = [(w.avg_hr, w.duration_s) for w in real if w.avg_hr and w.duration_s]
        avg_hr = round(sum(h * s for h, s in hr_w) / sum(s for _, s in hr_w)) if hr_w else None
        longest = max(real, key=lambda w: w.distance_m or 0) if real else None
        row = {
            "week_start": wk.isoformat(),
            "partial_current_week": wk == this_monday,
            "runs": len(real),
            "km": round(km, 1),
            "moving_min": round(mins),
            "load_hrss": round(load),
            "avg_hr": avg_hr,
            "ascent_m": round(sum((w.ascent_m or 0) for w in real)),
            "longest_run_km": round((longest.distance_m or 0) / 1000, 1) if longest else 0,
            "walks_km": round(sum((w.distance_m or 0) for w in walks) / 1000, 1),
            "km_change_pct": round((km - prev["km"]) / prev["km"] * 100) if prev and prev["km"] and wk != this_monday else None,
            "load_change_pct": round((load - prev["load_hrss"]) / prev["load_hrss"] * 100) if prev and prev["load_hrss"] and wk != this_monday else None,
        }
        weeks_out.append(row)
        prev = row

    def acwr(items: list[Workout]) -> dict:
        now = dt.datetime.now(dt.timezone.utc)
        acute = sum((_raw(w, "icu_training_load") or 0) for w in items if w.started_at >= now - dt.timedelta(days=7))
        chronic_total = sum((_raw(w, "icu_training_load") or 0) for w in items if w.started_at >= now - dt.timedelta(days=28))
        chronic_weekly = chronic_total / 4
        ratio = round(acute / chronic_weekly, 2) if chronic_weekly else None
        band = None
        if ratio is not None:
            band = "niedotrenowanie (<0.8)" if ratio < 0.8 else "sweet spot (0.8-1.3)" if ratio <= 1.3 else "podwyższone ryzyko (1.3-1.5)" if ratio <= 1.5 else "strefa niebezpieczna (>1.5)"
        return {"acute_7d_load": round(acute), "chronic_weekly_avg_28d": round(chronic_weekly), "acwr": ratio, "band": band}

    return {
        "weeks": weeks_out,
        "acwr_running_only": acwr([w for w in _load_runs(28)]),
        "acwr_all_sports": acwr(all_w),
        "guidance": "ramp objętości >10%/tydz. przez kilka tygodni z rzędu = ryzyko; ACWR 0.8-1.3 bezpieczne (Gabbett).",
    }


def get_intensity_distribution(days: int = 28) -> dict:
    """Rozkład intensywności biegów z ostatnich `days` dni w dwóch ujęciach:
    (1) CZAS w strefach tętna (z `icu_hr_zone_times`) zbity do łatwo/umiar-
    kowanie/mocno, (2) liczba SESJI wg średniego tętna. Do pytań "czy biegam
    za mocno", "czy mam wystarczająco łatwych biegów", "80/20". UWAGA: czas w
    strefach wg tętna zawyża udział "łatwo" (tętno rośnie z opóźnieniem na
    interwałach) - ujęcie sesyjne jest bliższe temu, jak Seiler definiuje
    80/20."""
    runs = _load_runs(days)
    zone_secs = [0] * 7
    with_zones = 0
    sessions = {"easy": 0, "moderate": 0, "hard": 0}
    session_rows = []
    for w in runs:
        zt = _raw(w, "icu_hr_zone_times")
        bounds = _raw(w, "icu_hr_zones") or []
        if zt and len(zt) == 7:
            with_zones += 1
            for i, s in enumerate(zt):
                zone_secs[i] += s or 0
        if w.avg_hr and len(bounds) >= 3 and (w.distance_m or 0) >= MIN_ANALYSIS_KM * 1000:
            cls = "easy" if w.avg_hr <= bounds[1] else "moderate" if w.avg_hr <= bounds[2] else "hard"
            sessions[cls] += 1
            session_rows.append({"date": w.started_at.date().isoformat(), "km": round((w.distance_m or 0) / 1000, 1), "avg_hr": w.avg_hr, "class": cls})
    total = sum(zone_secs) or 1
    easy = zone_secs[0] + zone_secs[1]
    moderate = zone_secs[2]
    hard = sum(zone_secs[3:])
    n_sess = sum(sessions.values()) or 1
    return {
        "days": days,
        "runs": len(runs),
        "time_in_zones_pct": {
            "easy_Z1_Z2": round(easy / total * 100),
            "moderate_Z3": round(moderate / total * 100),
            "hard_Z4_plus": round(hard / total * 100),
            "per_zone_min": [round(s / 60) for s in zone_secs],
            "runs_with_zone_data": with_zones,
        },
        "sessions_pct": {k: round(v / n_sess * 100) for k, v in sessions.items()},
        "sessions": session_rows,
        "target": "polaryzacja ~80% łatwo / ~20% umiarkowanie+mocno; dużo Z3 (tempo) bez celu = 'szara strefa'",
    }


def get_efficiency_trend(days: int = 90) -> dict:
    """Trend efektywności aerobowej: Efficiency Factor (m/min na uderzenie
    tętna) per bieg, mediany tygodniowe i zmiana pierwsza vs druga połowa
    okna. Rosnący EF przy tym samym tętnie = poprawa bazy; spadający przy
    normalnym śnie/HRV = zmęczenie lub upał. Tylko biegi na zewnątrz (nie
    bieżnia) - prędkość z bieżni jest niewiarygodna. Do pytań "czy robię
    postępy", "czy biegam szybciej przy tym samym tętnie"."""
    runs = [w for w in _load_runs(days) if not _raw(w, "trainer") and (w.distance_m or 0) >= MIN_ANALYSIS_KM * 1000]
    rows = []
    by_week: dict[dt.date, list[float]] = defaultdict(list)
    for w in runs:
        ef = _efficiency(w)
        if ef is None:
            continue
        d = w.started_at.date()
        by_week[d - dt.timedelta(days=d.weekday())].append(ef)
        rows.append({"date": d.isoformat(), "km": round((w.distance_m or 0) / 1000, 1), "pace": _pace_str(w.avg_pace), "avg_hr": w.avg_hr, "ef": ef, "temp_c": _raw(w, "average_temp") and round(_raw(w, "average_temp"))})
    weekly = [{"week_start": k.isoformat(), "median_ef": round(statistics.median(v), 3), "runs": len(v)} for k, v in sorted(by_week.items())]
    efs = [r["ef"] for r in rows]
    half = len(efs) // 2
    change_pct = None
    if half >= 2:
        first, second = statistics.mean(efs[:half]), statistics.mean(efs[half:])
        change_pct = round((second - first) / first * 100, 1)
    return {"days": days, "runs": rows[-12:], "weekly_median_ef": weekly, "ef_change_first_vs_second_half_pct": change_pct,
            "note": "EF porównuj tylko między biegami o podobnym charakterze; upał (temp_c) i przewyższenie obniżają EF niezależnie od formy."}


def _fetch_streams(external_id: str) -> dict[str, list]:
    resp = requests.get(
        f"https://intervals.icu/api/v1/activity/{external_id}/streams",
        params={"types": "time,heartrate,velocity_smooth,distance"},
        auth=("API_KEY", settings.intervals_api_key),
        timeout=30,
    )
    resp.raise_for_status()
    return {s["type"]: s.get("data", []) for s in resp.json()}


def _hr_drift_and_splits(streams: dict[str, list]) -> dict:
    t, hr, v, dist = streams.get("time", []), streams.get("heartrate", []), streams.get("velocity_smooth", []), streams.get("distance", [])
    n = min(len(t), len(hr), len(v), len(dist))
    pts = [(t[i], hr[i], v[i], dist[i]) for i in range(n) if hr[i] is not None and v[i] is not None and dist[i] is not None and v[i] > 0.5]
    if len(pts) < 600:
        return {"hr_drift_pct": None, "note": "za mało danych (<10 min ruchu)"}
    # Dryf (Pa:Hr decoupling, Friel): pomiń 10% czasu na rozgrzewkę, resztę
    # podziel na pół; porównaj prędkość/tętno w obu połowach.
    start = pts[int(len(pts) * 0.1)][0]
    body = [p for p in pts if p[0] >= start]
    mid = len(body) // 2
    def ratio(seg):
        return statistics.mean(p[2] for p in seg) / statistics.mean(p[1] for p in seg)
    r1, r2 = ratio(body[:mid]), ratio(body[mid:])
    drift = round((r1 - r2) / r1 * 100, 1)
    h1, h2 = body[:mid], body[mid:]
    # Splity co 1 km: tempo i średnie tętno.
    splits = []
    km_idx, seg = 1, []
    for p in pts:
        seg.append(p)
        if p[3] >= km_idx * 1000:
            dur = seg[-1][0] - seg[0][0]
            if dur > 0:
                splits.append({"km": km_idx, "pace": _pace_str(dur), "avg_hr": round(statistics.mean(x[1] for x in seg))})
            km_idx, seg = km_idx + 1, []
    return {
        "hr_drift_pct": drift,
        "first_half": {"pace": _pace_str(1000 / statistics.mean(p[2] for p in h1)), "avg_hr": round(statistics.mean(p[1] for p in h1))},
        "second_half": {"pace": _pace_str(1000 / statistics.mean(p[2] for p in h2)), "avg_hr": round(statistics.mean(p[1] for p in h2))},
        "interpretation": "<5%: baza aerobowa wystarcza na ten czas trwania; 5-10%: na granicy; >10%: za mocno/za długo jak na obecną bazę, albo upał/odwodnienie",
        "km_splits": splits[:30],
    }


def _latest_run_id() -> str | None:
    runs = _load_runs(120)
    return runs[-1].external_id if runs else None


def analyze_run(external_id: str | None = None) -> dict | None:
    """Pełna analiza JEDNEGO biegu (domyślnie OSTATNIEGO - nie musisz wcześniej
    wołać get_latest_workout): tempo, tętno, czas w strefach, obciążenie,
    forma w dniu treningu (CTL/ATL), samopoczucie, temperatura, kadencja,
    laps, ORAZ dryf tętna i splity co 1 km policzone ze strumieni z
    Intervals.icu (jedno wywołanie API). Konkretny `external_id` bierz z
    get_workouts_on_date/get_weekly... - nigdy nie zgaduj. Do pytań "jak
    wyszedł ten bieg", "dlaczego było ciężko", "czy dobrze rozłożyłem siły"."""
    external_id = external_id or _latest_run_id()
    if external_id is None:
        return None
    with get_session() as session:
        w = session.execute(select(Workout).where(Workout.external_id == external_id)).scalar_one_or_none()
        if w is None:
            return None
        session.expunge(w)
    if w.sport not in RUN_SPORTS:
        return {"error": f"to nie bieg (typ: {w.sport}) - użyj get_workouts/get_workout_detail"}

    zt = _raw(w, "icu_hr_zone_times") or []
    bounds = _raw(w, "icu_hr_zones") or []
    zones = [{"zone": ZONE_NAMES[i], "to_hr": bounds[i] if i < len(bounds) else None, "min": round(s / 60)} for i, s in enumerate(zt) if i < 7] if zt else []
    ctl, atl = _raw(w, "icu_ctl"), _raw(w, "icu_atl")
    out = {
        **_run_row(w),
        "elapsed_min": round((_raw(w, "elapsed_time") or 0) / 60),
        "time_in_zones": zones,
        "form_on_that_day": _form_summary(ctl, atl),
        "trimp": round(_raw(w, "trimp") or 0),
        "avg_cadence_spm": round((_raw(w, "average_cadence") or 0) * 2) or None,  # Intervals daje kroki na minutę na jedną nogę
        "avg_temp_c": round(_raw(w, "average_temp")) if _raw(w, "average_temp") is not None else None,
        "laps": (_raw(w, "interval_summary") or [])[:12],
        "device": _raw(w, "device_name"),
    }
    if _is_walk(w):
        out["note"] = "to marsz na bieżni, nie bieg - dryf/splity pominięte"
        return out
    try:
        out["streams_analysis"] = _hr_drift_and_splits(_fetch_streams(external_id))
    except Exception as e:  # noqa: BLE001 - brak sieci/API nie może zabić analizy
        out["streams_analysis"] = {"hr_drift_pct": None, "note": f"strumienie niedostępne: {type(e).__name__}"}
    return out


def find_comparable_runs(external_id: str | None = None, tolerance_pct: int = 15, days: int = 180) -> dict | None:
    """Biegi o podobnym dystansie (±tolerance_pct) z ostatnich `days` dni,
    bez bieżni, do porównania z podanym biegiem (domyślnie z OSTATNIM): tempo, tętno, EF,
    obciążenie, samopoczucie + różnice. "Ten sam dystans, wyższe tętno przy
    tym samym tempie" to sygnał zmęczenia/upału/choroby - "to samo tętno,
    szybsze tempo" to postęp. Do pytań "czy ten bieg był lepszy niż
    zwykle", "porównaj z poprzednimi"."""
    external_id = external_id or _latest_run_id()
    if external_id is None:
        return None
    with get_session() as session:
        target = session.execute(select(Workout).where(Workout.external_id == external_id)).scalar_one_or_none()
        if target is None:
            return None
        session.expunge(target)
    if not target.distance_m:
        return {"error": "bieg bez dystansu"}
    lo, hi = target.distance_m * (1 - tolerance_pct / 100), target.distance_m * (1 + tolerance_pct / 100)
    cands = [w for w in _load_runs(days) if w.external_id != external_id and w.distance_m and lo <= w.distance_m <= hi and not _raw(w, "trainer")]
    t_row = _run_row(target)
    comps = []
    for w in sorted(cands, key=lambda x: x.started_at, reverse=True)[:6]:
        r = _run_row(w)
        r["delta_vs_target"] = {
            "pace_s_km": (r["pace_s_km"] - t_row["pace_s_km"]) if r["pace_s_km"] and t_row["pace_s_km"] else None,
            "avg_hr": (r["avg_hr"] - t_row["avg_hr"]) if r["avg_hr"] and t_row["avg_hr"] else None,
            "ef": round(r["efficiency"] - t_row["efficiency"], 3) if r["efficiency"] and t_row["efficiency"] else None,
        }
        comps.append(r)
    return {"target": t_row, "comparable": comps, "note": "delta = porównywany - docelowy; ujemne pace_s_km = docelowy był wolniejszy"}


def get_fitness_form(days: int = 42) -> dict:
    """Przebieg CTL (fitness) / ATL (fatigue) / TSB (form) dzień po dniu z
    Intervals.icu, spróbkowany co tydzień + pełne ostatnie 7 dni, z pasmem
    wg Friela i ramp rate. Do pytań "jaka moja forma", "czy jestem
    zmęczony/wypoczęty", "kiedy zrobić mocny trening", planowania tygodnia."""
    since = dt.date.today() - dt.timedelta(days=days)
    with get_session() as session:
        rows = session.execute(select(Recovery).where(Recovery.date >= since).order_by(Recovery.date)).scalars().all()
        data = [(r.date, r.raw_json or {}) for r in rows]
    series = []
    for i, (d, raw) in enumerate(data):
        if raw.get("ctl") is None:
            continue
        is_recent = (dt.date.today() - d).days <= 7
        if is_recent or i % 7 == 0:
            f = _form_summary(raw.get("ctl"), raw.get("atl"))
            series.append({"date": d.isoformat(), "ctl": f.get("ctl_fitness"), "atl": f.get("atl_fatigue"), "tsb": f.get("tsb_form"), "form_pct": f.get("form_pct"), "ramp_rate": round(raw["rampRate"], 1) if raw.get("rampRate") is not None else None})
    current = series[-1] if series else None
    return {
        "current": {**current, "band": _form_band(current["form_pct"]) if current and current.get("form_pct") is not None else None} if current else None,
        "series": series,
        "bands_friel": [b[2] for b in FORM_BANDS],
        "ramp_rate_guidance": "wzrost CTL >5-8/tydz. przez kilka tygodni = agresywnie; ujemny = tracisz formę (ok w deloadzie/taperze)",
    }
