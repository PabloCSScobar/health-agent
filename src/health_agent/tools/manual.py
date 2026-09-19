"""Narzędzia do ręcznych wpisów z chatu (trening siłowy, waga, notatki)."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from health_agent.db.models import BodyComposition, DailyActivity, ManualLog
from health_agent.db.session import get_session

_DEDUP_WINDOW_S = 30


class ManualLogEntry(BaseModel):
    kind: str  # strength | weight | daily_calories | note | wellbeing
    payload: dict
    text_original: str


def _write_manual_log(entry: ManualLogEntry) -> int:
    """Zapisuje ręczny wpis. Zawsze ląduje w manual_logs (audyt/historia), a
    dodatkowo dla wybranych `kind` zapisuje też do właściwej tabeli
    analitycznej, żeby narzędzia specjalistów (get_body_composition_*,
    get_recovery_range) widziały ręczne wpisy tak samo jak automatyczne:
    - kind="weight": payload={"weight_kg": ..., "fat_pct": ...} (fat_pct opcjonalnie)
    - kind="daily_calories": payload={"date": "YYYY-MM-DD" (domyślnie dziś),
      "calories_total": ...} - CAŁODNIOWY wydatek (nie da się pobrać
      automatycznie z żadnego źródła - patrz scripts/README.md).

    UWAGA: lokalny model (Ollama) czasem wywołuje to narzędzie DWA razy pod
    rząd dla tej samej wypowiedzi użytkownika (złapane na żywo: ~1/3
    powtórzeń tego samego "spaliłem dziś X kalorii" skutkowało dwoma
    wywołaniami z tym samym kind/text_original w odstępie ułamka sekundy).
    Dla weight/daily_calories to nieszkodliwe (upsert po kluczu naturalnym w
    analitycznej tabeli), ale dla kind="strength" nie ma żadnego upsertu -
    StrengthCoach widziałby podwójny trening w manual_logs. Dlatego: jeśli
    identyczny (kind, text_original) wpis już istnieje z ostatnich
    `_DEDUP_WINDOW_S` sekund, nie wstawiaj drugiego - zwróć id istniejącego."""
    with get_session() as session:
        recent_cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=_DEDUP_WINDOW_S)
        duplicate = session.execute(
            select(ManualLog)
            .where(
                ManualLog.kind == entry.kind,
                ManualLog.text_original == entry.text_original,
                ManualLog.logged_at >= recent_cutoff,
            )
            .order_by(ManualLog.logged_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if duplicate is not None:
            return duplicate.id

        log = ManualLog(kind=entry.kind, payload_json=entry.payload, text_original=entry.text_original)
        session.add(log)
        session.flush()

        if entry.kind == "weight" and "weight_kg" in entry.payload:
            now = dt.datetime.now(dt.timezone.utc)
            stmt = pg_insert(BodyComposition).values(
                source="manual", measured_at=now,
                weight_kg=entry.payload.get("weight_kg"),
                fat_pct=entry.payload.get("fat_pct"),
            )
            session.execute(stmt)

        if entry.kind == "daily_calories" and "calories_total" in entry.payload:
            date_str = entry.payload.get("date")
            date = dt.date.fromisoformat(date_str) if date_str else dt.date.today()
            # Zwykły upsert (nadpisanie), NIE MAX jak przy Health Connect -
            # to świadomy, jednorazowy wpis użytkownika (może być korekta
            # w dół, np. poprawka pomyłki), nie rosnący licznik z urządzenia.
            stmt = (
                pg_insert(DailyActivity)
                .values(source="manual", date=date, calories_total=entry.payload["calories_total"])
                .on_conflict_do_update(
                    index_elements=["source", "date"],
                    set_={"calories_total": entry.payload["calories_total"]},
                )
            )
            session.execute(stmt)

        return log.id


def _format_confirmation(entry: ManualLogEntry, log_id: int) -> str:
    p = entry.payload
    if entry.kind == "weight" and "weight_kg" in p:
        extra = f", tłuszcz {p['fat_pct']}%" if p.get("fat_pct") is not None else ""
        return f"✅ Zapisano: waga {p['weight_kg']} kg{extra}"
    if entry.kind == "daily_calories" and "calories_total" in p:
        date = p.get("date") or "dziś"
        return f"✅ Zapisano: {p['calories_total']} kcal spalonych ({date})"
    if entry.kind == "wellbeing":
        note = f" — {p['note']}" if p.get("note") else ""
        return f"✅ Zapisano samopoczucie: {p['score']}/5{note}"
    if entry.kind == "note":
        note = p.get("note") or p.get("text") or entry.text_original
        return f"✅ Zapisano notatkę: {note}"
    if entry.kind == "strength":
        n = len(p.get("ćwiczenia", p.get("cwiczenia", []))) or None
        suffix = f" ({n} ćwiczeń)" if n else ""
        return f"✅ Zapisano trening siłowy{suffix} (wpis #{log_id})"
    return f"✅ Zapisano wpis (#{log_id})"


def log_manual_entry(entry: ManualLogEntry) -> str:
    """Zapisuje ręczny wpis i zwraca GOTOWE potwierdzenie po polsku - patrz
    docstring `_write_manual_log` po opis `kind`/`payload` i dedupu.

    To jest "output function" orchestratora (patrz `output_type` w
    registry.py): jej zwrócony string staje się OD RAZU finalną odpowiedzią
    dla użytkownika, bez dodatkowego wywołania modelu na samo sformułowanie
    potwierdzenia. Dlatego formatuje je deterministycznie tutaj, zamiast
    zwracać samo `id` i liczyć na to, że model ładnie to opisze."""
    from health_agent.tools.manual_batch import log_manual_entries

    return log_manual_entries([entry])


def get_recent_manual_logs(kind: str | None = None, limit: int = 20) -> list[dict]:
    with get_session() as session:
        stmt = select(ManualLog).order_by(ManualLog.logged_at.desc()).limit(limit)
        if kind:
            stmt = stmt.where(ManualLog.kind == kind)
        rows = session.execute(stmt).scalars().all()
        return [
            {"logged_at": r.logged_at.isoformat(), "kind": r.kind, "payload": r.payload_json, "text": r.text_original}
            for r in rows
        ]
