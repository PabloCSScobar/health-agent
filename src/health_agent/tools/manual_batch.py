"""Atomowy zapis jednego lub wielu ręcznych wpisów z jednej wiadomości."""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from health_agent.db.models import BodyComposition, DailyActivity, ManualLog
from health_agent.db.session import get_session
from health_agent.time_utils import local_today
from health_agent.tools.manual import ManualLogEntry, _format_confirmation

_DEDUP_WINDOW_S = 30
_MANUAL_LOCK_KEY = 0x4D414E55414C
_ALLOWED_KINDS = {"strength", "weight", "daily_calories", "note", "wellbeing"}


def _strict_number(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} musi być liczbą")
    return float(value)


def _event_date(entry: ManualLogEntry) -> dt.date:
    if "date" not in entry.payload:
        return local_today()
    raw = entry.payload["date"]
    if not isinstance(raw, str) or not raw:
        raise ValueError("date musi mieć format YYYY-MM-DD")
    parsed = dt.date.fromisoformat(raw)
    if parsed.isoformat() != raw:
        raise ValueError("date musi mieć format YYYY-MM-DD")
    return parsed


def _validate(entries: list[ManualLogEntry]) -> list[ManualLogEntry]:
    if not entries:
        raise ValueError("Lista wpisów nie może być pusta")
    validated = [ManualLogEntry.model_validate(entry) for entry in entries]
    for entry in validated:
        if entry.kind not in _ALLOWED_KINDS:
            raise ValueError(f"Nieobsługiwany rodzaj wpisu: {entry.kind}")
        if not entry.text_original.strip():
            raise ValueError("text_original nie może być pusty")
        if "date" in entry.payload:
            _event_date(entry)
        if entry.kind == "weight":
            weight = _strict_number(entry.payload.get("weight_kg"), "weight_kg")
            if weight <= 0:
                raise ValueError("weight_kg musi być większe od zera")
            if entry.payload.get("fat_pct") is not None:
                _strict_number(entry.payload["fat_pct"], "fat_pct")
        elif entry.kind == "daily_calories":
            calories = _strict_number(entry.payload.get("calories_total"), "calories_total")
            if calories < 0:
                raise ValueError("calories_total nie może być ujemne")
        elif entry.kind == "wellbeing":
            score = entry.payload.get("score")
            if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
                raise ValueError("score samopoczucia musi być liczbą całkowitą 1–5")
    return validated


def _normalize_json_value(value):
    """Ujednolić reprezentację JSON bez zaokrąglania wartości pomiarów."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, dict):
        return {key: _normalize_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_json_value(item) for item in value]
    return value


def _normalized_payload(entry: ManualLogEntry) -> dict:
    return _normalize_json_value(entry.payload)


def _signature(entry: ManualLogEntry) -> tuple[str, str, str]:
    payload = json.dumps(_normalized_payload(entry), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return entry.kind, entry.text_original, payload


def _write_analytical_rows(session, entry: ManualLogEntry, logged_at: dt.datetime) -> None:
    if entry.kind == "weight":
        session.add(
            BodyComposition(
                source="manual",
                measured_at=logged_at,
                weight_kg=float(entry.payload["weight_kg"]),
                fat_pct=entry.payload.get("fat_pct"),
            )
        )
    elif entry.kind == "daily_calories":
        day = _event_date(entry)
        stmt = (
            pg_insert(DailyActivity)
            .values(source="manual", date=day, calories_total=float(entry.payload["calories_total"]))
            .on_conflict_do_update(
                index_elements=["source", "date"],
                set_={"calories_total": float(entry.payload["calories_total"])},
            )
        )
        session.execute(stmt)


def write_manual_entries(entries: list[ManualLogEntry]) -> list[int]:
    """Zapisz cały pakiet lub nic; retry zwraca id istniejących wpisów."""
    entries = _validate(entries)
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(seconds=_DEDUP_WINDOW_S)

    with get_session() as session:
        if session.get_bind().dialect.name != "postgresql":
            raise RuntimeError("Atomowy zapis ręczny wymaga PostgreSQL")
        session.execute(select(func.pg_advisory_xact_lock(_MANUAL_LOCK_KEY)))

        kinds = {entry.kind for entry in entries}
        texts = {entry.text_original for entry in entries}
        existing = session.execute(
            select(ManualLog)
            .where(
                ManualLog.kind.in_(kinds),
                ManualLog.text_original.in_(texts),
                ManualLog.logged_at >= cutoff,
            )
            .order_by(ManualLog.id)
        ).scalars().all()
        available: dict[tuple[str, str, str], list[int]] = defaultdict(list)
        for row in existing:
            candidate = ManualLogEntry(
                kind=row.kind,
                payload=row.payload_json or {},
                text_original=row.text_original or "",
            )
            available[_signature(candidate)].append(row.id)

        ids: list[int] = []
        for index, entry in enumerate(entries):
            signature = _signature(entry)
            if available[signature]:
                ids.append(available[signature].pop(0))
                continue
            logged_at = now + dt.timedelta(microseconds=index)
            row = ManualLog(
                logged_at=logged_at,
                kind=entry.kind,
                payload_json=entry.payload,
                text_original=entry.text_original,
            )
            session.add(row)
            session.flush()
            _write_analytical_rows(session, entry, logged_at)
            ids.append(row.id)
        return ids


def log_manual_entries(entries: list[ManualLogEntry]) -> str:
    """Output function orchestratora: zapisuje pakiet i potwierdza wszystkie wpisy."""
    validated = _validate(entries)
    ids = write_manual_entries(validated)
    confirmations = [
        _format_confirmation(entry, log_id).removeprefix("✅ ")
        for entry, log_id in zip(validated, ids)
    ]
    return "✅ " + "; ".join(confirmations)
