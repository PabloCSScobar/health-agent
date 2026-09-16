"""Ingestia treningów i wellness z Intervals.icu (oficjalne API, osobisty klucz).

Autoryzacja: HTTP Basic auth, login "API_KEY", hasło = wygenerowany klucz
(patrz scripts/README.md pkt 4 po pełne uzasadnienie, dlaczego to zamiast
bezpośredniego API Suunto).

Nazwy pól potwierdzone na prawdziwej odpowiedzi API (nie z dokumentacji):
- `/activities`: `id` (stabilny, np. "i186942652"), `type`, `start_date`
  (UTC), `moving_time`, `distance`, `average_heartrate`, `max_heartrate`,
  `calories`, `total_elevation_gain`, `average_speed` (m/s).
  UWAGA: Intervals.icu NIE ma Training Effect ani VO2max na poziomie
  treningu (to specyfika Suunto/Garmin) - te pola zostają puste.
- `/wellness`: `id` = data (YYYY-MM-DD), `restingHR`, `hrv`, `steps`,
  `sleepSecs`, `sleepScore`, `stress`. UWAGA: brak podziału snu na
  fazy (deep/rem/light) i brak `calories_active`/`calories_total` -
  Intervals.icu tego nie udostępnia w tym endpoincie.
"""

from __future__ import annotations

import datetime as dt

import requests
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from health_agent.db.models import DailyActivity, RawPayload, Recovery, Sleep, Workout
from health_agent.settings import settings

BASE_URL = "https://intervals.icu/api/v1"
SOURCE = "intervals_icu"


def _auth() -> tuple[str, str]:
    if not settings.intervals_api_key:
        raise RuntimeError("Brak INTERVALS_API_KEY w .env")
    return ("API_KEY", settings.intervals_api_key)


def fetch_activities(oldest: dt.date, newest: dt.date) -> list[dict]:
    resp = requests.get(
        f"{BASE_URL}/athlete/{settings.intervals_athlete_id}/activities",
        params={"oldest": oldest.isoformat(), "newest": newest.isoformat()},
        auth=_auth(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_wellness(oldest: dt.date, newest: dt.date) -> list[dict]:
    resp = requests.get(
        f"{BASE_URL}/athlete/{settings.intervals_athlete_id}/wellness",
        params={"oldest": oldest.isoformat(), "newest": newest.isoformat()},
        auth=_auth(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _upsert_workout(session: Session, activity: dict) -> None:
    started_at = None
    if activity.get("start_date"):
        started_at = dt.datetime.fromisoformat(activity["start_date"].replace("Z", "+00:00"))

    avg_speed = activity.get("average_speed")
    avg_pace = (1000 / avg_speed) if avg_speed else None  # sek/km

    fields = {
        "sport": activity.get("type"),
        "started_at": started_at,
        "duration_s": activity.get("moving_time"),
        "distance_m": activity.get("distance"),
        "avg_hr": activity.get("average_heartrate"),
        "max_hr": activity.get("max_heartrate"),
        "calories": activity.get("calories"),
        "ascent_m": activity.get("total_elevation_gain"),
        "avg_pace": avg_pace,
        "training_effect_aerobic": None,  # niedostępne w Intervals.icu
        "vo2max_est": None,  # niedostępne na poziomie treningu
        "raw_json": activity,
    }
    stmt = (
        pg_insert(Workout)
        .values(source=SOURCE, external_id=activity["id"], **fields)
        .on_conflict_do_update(index_elements=["source", "external_id"], set_=fields)
    )
    session.execute(stmt)


def _upsert_wellness(session: Session, day_row: dict) -> None:
    date = dt.date.fromisoformat(day_row["id"])

    daily_fields = {
        "steps": day_row.get("steps"),
        "calories_active": None,  # niedostępne w Intervals.icu /wellness
        "calories_total": None,
        "resting_hr": day_row.get("restingHR"),
    }
    stmt = (
        pg_insert(DailyActivity)
        .values(source=SOURCE, date=date, **daily_fields)
        .on_conflict_do_update(index_elements=["source", "date"], set_=daily_fields)
    )
    session.execute(stmt)

    sleep_fields = {
        "start": None,
        "end": None,
        "duration_s": day_row.get("sleepSecs"),
        "deep_s": None,  # Intervals.icu nie rozbija snu na fazy
        "rem_s": None,
        "light_s": None,
        "score": day_row.get("sleepScore"),
        "raw_json": day_row,
    }
    stmt = (
        pg_insert(Sleep)
        .values(source=SOURCE, date=date, **sleep_fields)
        .on_conflict_do_update(index_elements=["source", "date"], set_=sleep_fields)
    )
    session.execute(stmt)

    recovery_fields = {
        "hrv": day_row.get("hrv"),
        "resources_pct": None,  # odpowiednik "Suunto Resources" niedostępny
        "stress": day_row.get("stress"),
        "raw_json": day_row,
    }
    stmt = (
        pg_insert(Recovery)
        .values(source=SOURCE, date=date, **recovery_fields)
        .on_conflict_do_update(index_elements=["source", "date"], set_=recovery_fields)
    )
    session.execute(stmt)


def ingest_range(session: Session, oldest: dt.date, newest: dt.date) -> dict:
    activities = fetch_activities(oldest, newest)
    session.add(RawPayload(source="intervals_icu_activities", payload_json=activities))
    for activity in activities:
        _upsert_workout(session, activity)

    wellness = fetch_wellness(oldest, newest)
    session.add(RawPayload(source="intervals_icu_wellness", payload_json=wellness))
    for day_row in wellness:
        _upsert_wellness(session, day_row)

    return {"workouts": len(activities), "wellness_days": len(wellness)}
