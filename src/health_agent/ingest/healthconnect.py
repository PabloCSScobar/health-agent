"""Normalizacja payloadu z apki Health Connect Webhook (com.hcwebhook.app).

Kształt payloadu potwierdzony na prawdziwych danych (patrz scripts/README.md):
    {
      "timestamp": "...", "app_version": "...",
      "weight": [{"kilograms": 87.5, "time": "...", "metadata": {...}}],
      "body_fat": [{"percentage": 22.8, "time": "...", "metadata": {...}}],
      "lean_body_mass": [{"kilograms": ..., "time": "..."}],
      "bone_mass": [{"kilograms": ..., "time": "..."}],
      "nutrition": [{"calories": ..., "protein_grams": ..., "carbs_grams": ...,
                      "fat_grams": ..., "sugar_grams": ..., "sodium_grams": ...,
                      "dietary_fiber_grams": ..., "name": "...",
                      "start_time": "...", "end_time": "..."}],
      "steps": [{"count": ..., "start_time": "...", "end_time": "..."}],
      "active_calories": [{"calories": ..., "start_time": "...", "end_time": "..."}],
      "total_calories": [{"calories": ..., "start_time": "...", "end_time": "..."}]
    }
Klucze steps/active_calories/total_calories widziane na razie tylko w
testowym payloadzie apki (przycisk testowy, nie prawdziwy sync) - jeśli
Suunto faktycznie wypycha total_calories do Health Connect, to jedyna droga
na CAŁODNIOWY wydatek kaloryczny (BMR + aktywność), którego brakuje w
Intervals.icu - do potwierdzenia na prawdziwym sync z telefonu.
Klucze, których nie ma w danej paczce, po prostu nie występują.

Uwaga o pozycjach odżywiania: Health Connect (przez Fitatu) NIE nadaje
pozycjom stabilnego ID - `start_time`/`end_time` to cały lokalny dzień
(np. "2026-09-14T22:00:00Z" do "2026-09-15T21:59:59Z" = cała doba
2026-09-15 czasu polskiego), nie faktyczna godzina posiłku. Dlatego:
  - dzień pozycji wyliczamy z daty `end_time` (w UTC pokrywa się z lokalnym
    dniem polskim dzięki tej całodobowej konwencji),
  - external_id to hash treści (nazwa + makra + dzień), żeby ponowne
    przysłanie tej samej pozycji (lookback 48h w apce) nie tworzyło duplikatu.
"""

from __future__ import annotations

import datetime as dt
import hashlib

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from health_agent.db.models import BodyComposition, DailyActivity, NutritionDay, NutritionItem, RawPayload

SOURCE_BODY_COMPOSITION = "healthconnect"
SOURCE_NUTRITION = "healthconnect"
SOURCE_DAILY_ACTIVITY = "healthconnect"


def _parse_time(value: str) -> dt.datetime:
    # Health Connect payloady kończą się na "Z" (UTC) - fromisoformat w Pythonie
    # < 3.11 tego nie łyka, więc normalizujemy na "+00:00".
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _nutrition_external_id(
    day: dt.date, name: str | None, kcal: float | None, protein: float | None, occurrence: int
) -> str:
    # `occurrence` odróżnia dwie identyczne pozycje (np. dwa te same banany
    # tego samego dnia) w ramach JEDNEJ paczki danych - patrz komentarz przy
    # wywołaniu w ingest_payload(). Bez tego druga identyczna pozycja
    # nadpisałaby pierwszą zamiast dodać się obok.
    #
    # ZAOKRĄGLENIE kcal/protein do 1 miejsca po przecinku - złapane na
    # żywo: ten sam realny produkt (np. "Kajzerka") zwrócony w dwóch
    # osobnych synchronizacjach miał kcal=336 i kcal=336.00000000000006
    # (szum zmiennoprzecinkowy z przeliczeń w Fitatu/Health Connect, nie
    # różnica w rzeczywistej ilości) - bez zaokrąglenia hash wychodził
    # inny, więc ta sama pozycja duplikowała się przy każdym kolejnym
    # syncu zamiast się nadpisywać, zawyżając dzienną sumę kalorii.
    # float(...) PRZED round() jest konieczne - JSON bez kropki dziesiętnej
    # ("calories": 336) parsuje się w Pythonie jako int, a z kropką
    # ("calories": 336.00000000000006) jako float; round(336, 1) zwraca
    # int 336 (bez zmian), round(336.0..., 1) zwraca float 336.0 - w
    # f-stringu niżej to DWIE RÓŻNE reprezentacje tekstowe ("336" vs
    # "336.0"), więc bez wymuszenia float() hash nadal by się różnił mimo
    # zaokrąglenia. Złapane bezpośrednio w teście tej funkcji.
    kcal_r = round(float(kcal), 1) if kcal is not None else 0.0
    protein_r = round(float(protein), 1) if protein is not None else 0.0
    raw = f"{day.isoformat()}|{name or ''}|{kcal_r}|{protein_r}|{occurrence}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _upsert_body_composition(session: Session, measured_at: dt.datetime, fields: dict) -> None:
    stmt = (
        pg_insert(BodyComposition)
        .values(source=SOURCE_BODY_COMPOSITION, measured_at=measured_at, **fields)
        .on_conflict_do_update(
            index_elements=["source", "measured_at"],
            set_=fields,
        )
    )
    session.execute(stmt)


def _upsert_nutrition_item(session: Session, external_id: str, day: dt.date, fields: dict) -> None:
    stmt = (
        pg_insert(NutritionItem)
        .values(source=SOURCE_NUTRITION, external_id=external_id, day=day, **fields)
        .on_conflict_do_update(
            index_elements=["source", "external_id"],
            set_=fields,
        )
    )
    session.execute(stmt)


def _upsert_daily_activity_max(session: Session, date: dt.date, field: str, value: float | int | None) -> None:
    """Upsert biorący MAX z nowej i istniejącej wartości, nie nadpisujący na
    ślepo. `total_calories`/`steps`/`active_calories` z Health Connect to
    zwykle rosnący w ciągu dnia licznik (kolejne synchronizacje w ciągu dnia
    raportują coraz większą wartość "do tej pory") - MAX unika
    "cofnięcia" dziennej sumy przy kolejnym, węższym oknie synchronizacji.
    Heurystyka do zweryfikowania na realnych danych z Suunto - nie wiemy
    jeszcze na pewno jak Suunto konkretnie wypełnia te pola."""
    if value is None:
        return
    stmt = (
        pg_insert(DailyActivity)
        .values(source=SOURCE_DAILY_ACTIVITY, date=date, **{field: value})
        .on_conflict_do_update(
            index_elements=["source", "date"],
            set_={field: func.greatest(DailyActivity.__table__.c[field], value)},
        )
    )
    session.execute(stmt)


def _ensure_nutrition_day(session: Session, day: dt.date) -> None:
    """Tworzy pusty wiersz nutrition_days, jeśli jeszcze nie istnieje.

    Musi polecieć PRZED wstawieniem nutrition_items (klucz obcy day ->
    nutrition_days.date) - właściwe sumy dolicza `_recompute_nutrition_day`
    po wstawieniu wszystkich pozycji danego dnia.
    """
    stmt = pg_insert(NutritionDay).values(date=day).on_conflict_do_nothing(index_elements=["date"])
    session.execute(stmt)


def _recompute_nutrition_day(session: Session, day: dt.date) -> None:
    totals = session.execute(
        select(
            func.sum(NutritionItem.kcal),
            func.sum(NutritionItem.protein_g),
            func.sum(NutritionItem.fat_g),
            func.sum(NutritionItem.carbs_g),
            func.sum(NutritionItem.fiber_g),
            func.sum(NutritionItem.sugar_g),
            func.sum(NutritionItem.salt_g),
        ).where(NutritionItem.day == day)
    ).one()
    kcal, protein_g, fat_g, carbs_g, fiber_g, sugar_g, salt_g = totals
    stmt = (
        pg_insert(NutritionDay)
        .values(
            date=day, kcal=kcal, protein_g=protein_g, fat_g=fat_g,
            carbs_g=carbs_g, fiber_g=fiber_g, sugar_g=sugar_g, salt_g=salt_g,
        )
        .on_conflict_do_update(
            index_elements=["date"],
            set_={
                "kcal": kcal, "protein_g": protein_g, "fat_g": fat_g,
                "carbs_g": carbs_g, "fiber_g": fiber_g, "sugar_g": sugar_g, "salt_g": salt_g,
            },
        )
    )
    session.execute(stmt)


def ingest_payload(session: Session, payload: dict) -> dict:
    """Zapisuje raw payload, normalizuje wagę/skład ciała i odżywianie.

    Zwraca krótkie podsumowanie tego co zostało zapisane - do logów/testów.
    """
    session.add(RawPayload(source="healthconnect", payload_json=payload))

    # --- Waga i skład ciała: łączymy po dokładnym czasie pomiaru, bo apka
    # przysyła wagę/tłuszcz/masę mięśniową/masę kości jako osobne tablice,
    # ale ze wspólnym `time` gdy pochodzą z tego samego pomiaru. ---
    by_time: dict[dt.datetime, dict] = {}

    def _collect(key: str, field: str, value_key: str) -> None:
        for rec in payload.get(key) or []:
            t = _parse_time(rec["time"])
            by_time.setdefault(t, {})[field] = rec.get(value_key)

    _collect("weight", "weight_kg", "kilograms")
    _collect("body_fat", "fat_pct", "percentage")
    _collect("lean_body_mass", "muscle_kg", "kilograms")
    _collect("bone_mass", "bone_kg", "kilograms")

    body_composition_count = 0
    for measured_at, fields in by_time.items():
        _upsert_body_composition(session, measured_at, fields)
        body_composition_count += 1

    # --- Odżywianie ---
    # Health Connect (przez Fitatu) NIE daje pozycjom stabilnego ID ani
    # informacji o posiłku (śniadanie/obiad) - sprawdzone na prawdziwych
    # danych, `start_time`/`end_time` to cała doba, nie godzina posiłku.
    # Dwie identyczne pozycje (ten sam produkt, te same kalorie/białko) tego
    # samego dnia odróżniamy licznikiem wystąpień W RAMACH TEJ PACZKI -
    # zakłada to, że kolejność pozycji jest stabilna między kolejnymi
    # synchronizacjami tego samego dnia (typowe dla zapytań do Health
    # Connect, ale nie 100% gwarantowane).
    affected_days: set[dt.date] = set()
    nutrition_count = 0
    occurrence_counts: dict[tuple, int] = {}
    for rec in payload.get("nutrition") or []:
        end_time = rec.get("end_time") or rec.get("start_time")
        if not end_time:
            continue
        day = _parse_time(end_time).date()
        _ensure_nutrition_day(session, day)
        name = rec.get("name")
        kcal = rec.get("calories")
        protein_g = rec.get("protein_grams")
        dedup_key = (day, name, kcal, protein_g)
        occurrence = occurrence_counts.get(dedup_key, 0)
        occurrence_counts[dedup_key] = occurrence + 1
        external_id = _nutrition_external_id(day, name, kcal, protein_g, occurrence)
        fields = {
            "meal": None,
            "product": name,
            "qty": None,
            "kcal": kcal,
            "protein_g": protein_g,
            "fat_g": rec.get("fat_grams"),
            "carbs_g": rec.get("carbs_grams"),
            "sugar_g": rec.get("sugar_grams"),
            "fiber_g": rec.get("dietary_fiber_grams"),
            "salt_g": rec.get("sodium_grams"),
            "raw_json": rec,
        }
        _upsert_nutrition_item(session, external_id, day, fields)
        nutrition_count += 1
        affected_days.add(day)

    for day in affected_days:
        _recompute_nutrition_day(session, day)

    # --- Aktywność dzienna: kroki, kalorie aktywne, kalorie CAŁODNIOWE ---
    # (total_calories = BMR + aktywność - dana, której brakuje z Intervals.icu,
    # patrz scripts/README.md). Bucketowane po dniu z end_time, MAX per dzień
    # (patrz komentarz w _upsert_daily_activity_max).
    daily_activity_days: set[dt.date] = set()

    def _collect_daily(key: str, field: str, value_key: str) -> None:
        for rec in payload.get(key) or []:
            end_time = rec.get("end_time") or rec.get("start_time")
            if not end_time:
                continue
            day = _parse_time(end_time).date()
            _upsert_daily_activity_max(session, day, field, rec.get(value_key))
            daily_activity_days.add(day)

    _collect_daily("steps", "steps", "count")
    _collect_daily("active_calories", "calories_active", "calories")
    _collect_daily("total_calories", "calories_total", "calories")

    from health_agent.tools.reminders import mark_data_freshness

    now = dt.datetime.now(dt.timezone.utc)
    if affected_days:
        mark_data_freshness(session, "nutrition", SOURCE_NUTRITION, max(affected_days), now)
    if payload.get("steps") and daily_activity_days:
        step_days = {
            _parse_time(rec.get("end_time") or rec.get("start_time")).date()
            for rec in payload["steps"]
            if rec.get("end_time") or rec.get("start_time")
        }
        if step_days:
            mark_data_freshness(session, "steps", SOURCE_DAILY_ACTIVITY, max(step_days), now)
    if by_time:
        from health_agent.time_utils import local_date

        mark_data_freshness(
            session,
            "body",
            SOURCE_BODY_COMPOSITION,
            max(local_date(value) for value in by_time),
            now,
        )

    return {
        "body_composition_rows": body_composition_count,
        "nutrition_item_rows": nutrition_count,
        "nutrition_days_recomputed": len(affected_days),
        "daily_activity_days": len(daily_activity_days),
    }
