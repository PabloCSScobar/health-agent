"""Suplementy i trwałe przypomnienia oceniane deterministycznie."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from health_agent.db.models import (
    DailyActivity,
    DataFreshness,
    NotificationOutbox,
    NutritionDay,
    ReminderOccurrence,
    ReminderRule,
    Supplement,
    SupplementIntake,
    Workout,
)
from health_agent.db.session import get_session
from health_agent.settings import settings
from health_agent.tools.running import RUN_SPORTS

logger = logging.getLogger("health_agent.reminders")
RULE_KINDS = {"supplement", "run", "workout", "text"}
CONDITION_TYPES = {None, "steps_below", "protein_below", "no_run", "no_workout"}
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _supplement_row(row: Supplement) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "dose": row.dose,
        "notes": row.notes,
        "active": row.active,
    }


def add_supplement(name: str, dose: str | None = None, notes: str | None = None) -> str:
    """Dodaj lub uaktualnij suplement użytkownika. Nie twórz przypomnienia."""
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("Nazwa suplementu jest wymagana")
    with get_session() as session:
        existing = session.execute(
            select(Supplement).where(func.lower(Supplement.name) == cleaned.lower())
        ).scalar_one_or_none()
        if existing:
            existing.dose = dose.strip() if dose else existing.dose
            existing.notes = notes.strip() if notes else existing.notes
            existing.active = True
            supplement_id = existing.id
        else:
            row = Supplement(name=cleaned, dose=dose, notes=notes, active=True)
            session.add(row)
            session.flush()
            supplement_id = row.id
    return f"Suplement zapisany (#{supplement_id}): {cleaned}" + (f", {dose}" if dose else "")


def list_supplements(include_inactive: bool = False) -> list[dict]:
    """Lista suplementów i ostatnie odnotowane przyjęcie/pominięcie."""
    with get_session() as session:
        stmt = select(Supplement).order_by(Supplement.active.desc(), Supplement.name)
        if not include_inactive:
            stmt = stmt.where(Supplement.active.is_(True))
        rows = session.execute(stmt).scalars().all()
        result = []
        for row in rows:
            latest = session.execute(
                select(SupplementIntake)
                .where(SupplementIntake.supplement_id == row.id)
                .order_by(SupplementIntake.recorded_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            result.append(
                {
                    **_supplement_row(row),
                    "last_status": latest.status if latest else None,
                    "last_recorded_at": latest.recorded_at.isoformat() if latest else None,
                }
            )
        return result


def update_supplement(
    supplement_id: int,
    *,
    name: str | None = None,
    dose: str | None = None,
    notes: str | None = None,
    active: bool | None = None,
) -> dict:
    with get_session() as session:
        row = session.get(Supplement, supplement_id)
        if row is None:
            raise LookupError("Nieznany suplement")
        if name is not None:
            if not name.strip():
                raise ValueError("Nazwa suplementu jest wymagana")
            row.name = name.strip()
        if dose is not None:
            row.dose = dose.strip() or None
        if notes is not None:
            row.notes = notes.strip() or None
        if active is not None:
            row.active = active
        return _supplement_row(row)


def record_supplement_intake(
    supplement_id: int,
    status: str,
    *,
    scheduled_for: dt.datetime | None = None,
    source: str = "dashboard",
    note: str | None = None,
) -> int:
    if status not in {"taken", "skipped"}:
        raise ValueError("status musi być taken albo skipped")
    with get_session() as session:
        if session.get(Supplement, supplement_id) is None:
            raise LookupError("Nieznany suplement")
        values = {
            "supplement_id": supplement_id,
            "scheduled_for": scheduled_for,
            "recorded_at": dt.datetime.now(dt.timezone.utc),
            "status": status,
            "source": source,
            "note": note,
        }
        if scheduled_for is not None:
            row_id = session.execute(
                pg_insert(SupplementIntake)
                .values(**values)
                .on_conflict_do_update(
                    constraint="uq_supplement_intake_schedule",
                    set_={
                        "recorded_at": values["recorded_at"],
                        "status": status,
                        "source": source,
                        "note": note,
                    },
                )
                .returning(SupplementIntake.id)
            ).scalar_one()
            return row_id
        row = SupplementIntake(**values)
        session.add(row)
        session.flush()
        return row.id


def _validate_rule(
    kind: str,
    local_time: str,
    condition_type: str | None,
    condition_threshold: float | None,
) -> None:
    if kind not in RULE_KINDS:
        raise ValueError(f"kind musi być jednym z: {sorted(RULE_KINDS)}")
    if not _TIME_RE.fullmatch(local_time):
        raise ValueError("local_time musi mieć format HH:MM")
    if condition_type not in CONDITION_TYPES:
        raise ValueError("Nieobsługiwany warunek przypomnienia")
    if condition_type in {"steps_below", "protein_below"} and (
        condition_threshold is None or condition_threshold <= 0
    ):
        raise ValueError("Warunek wymaga dodatniego progu")


def propose_reminder_rule(
    title: str,
    kind: str = "text",
    local_time: str = "20:00",
    condition_type: str | None = None,
    condition_threshold: float | None = None,
    condition_window_hours: int | None = None,
    supplement_id: int | None = None,
) -> str:
    """Utwórz WYŁĄCZNIE szkic reguły do jawnego potwierdzenia użytkownika.

    Dostępne warunki: steps_below, protein_below, no_run, no_workout.
    Dla kroków i białka podaj jawny próg użytkownika. Domyślne "wieczorem"
    oznacza 20:00 Europe/Warsaw.
    """
    _validate_rule(kind, local_time, condition_type, condition_threshold)
    if not title.strip():
        raise ValueError("Treść przypomnienia jest wymagana")
    if condition_window_hours is not None and not 1 <= condition_window_hours <= 168:
        raise ValueError("condition_window_hours musi być w zakresie 1-168")
    with get_session() as session:
        if supplement_id is not None and session.get(Supplement, supplement_id) is None:
            raise LookupError("Nieznany suplement")
        row = ReminderRule(
            kind=kind,
            title=title.strip(),
            local_time=local_time,
            timezone=settings.summary_timezone,
            schedule_json={"weekdays": list(range(7))},
            condition_type=condition_type,
            condition_threshold=condition_threshold,
            condition_window_hours=condition_window_hours,
            payload_json={"supplement_id": supplement_id} if supplement_id else {},
            status="draft",
        )
        session.add(row)
        session.flush()
        rule_id = row.id
    condition = (
        f", warunek {condition_type} {condition_threshold or ''}".rstrip()
        if condition_type
        else ""
    )
    return (
        f"Propozycja przypomnienia #{rule_id}: {title.strip()} o {local_time}"
        f"{condition}. Potwierdź ją przyciskiem. [REMINDER_DRAFT:{rule_id}]"
    )


def activate_reminder_rule(rule_id: int) -> dict:
    with get_session() as session:
        row = session.get(ReminderRule, rule_id)
        if row is None:
            raise LookupError("Nieznana reguła")
        row.status = "active"
        return _rule_row(row)


def update_reminder_rule(rule_id: int, **changes) -> dict:
    allowed = {
        "title", "local_time", "timezone", "schedule_json", "condition_type",
        "condition_threshold", "condition_window_hours", "payload_json", "status",
    }
    if unknown := set(changes) - allowed:
        raise ValueError(f"Nieznane pola: {sorted(unknown)}")
    if "status" in changes and changes["status"] not in {"draft", "active", "paused"}:
        raise ValueError("Nieprawidłowy status reguły")
    if "title" in changes:
        title = changes["title"]
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Treść przypomnienia jest wymagana")
        changes["title"] = title.strip()
    if "local_time" in changes and not isinstance(changes["local_time"], str):
        raise ValueError("local_time musi mieć format HH:MM")
    if "timezone" in changes:
        timezone = changes["timezone"]
        if not isinstance(timezone, str) or not timezone:
            raise ValueError("timezone musi być niepustą nazwą strefy")
        try:
            ZoneInfo(timezone)
        except (KeyError, ValueError) as exc:
            raise ValueError("Nieznana strefa czasowa") from exc
    if "schedule_json" in changes:
        schedule = changes["schedule_json"]
        if not isinstance(schedule, dict):
            raise ValueError("schedule_json musi być obiektem")
        weekdays = schedule.get("weekdays")
        if (
            not isinstance(weekdays, list)
            or not weekdays
            or any(not isinstance(day, int) or isinstance(day, bool) or day not in range(7) for day in weekdays)
        ):
            raise ValueError("schedule_json.weekdays musi zawierać dni 0-6")
    if "condition_window_hours" in changes:
        window = changes["condition_window_hours"]
        if window is not None and (
            not isinstance(window, int) or isinstance(window, bool) or not 1 <= window <= 168
        ):
            raise ValueError("condition_window_hours musi być w zakresie 1-168")
    if "payload_json" in changes and not isinstance(changes["payload_json"], dict):
        raise ValueError("payload_json musi być obiektem")
    with get_session() as session:
        row = session.get(ReminderRule, rule_id)
        if row is None:
            raise LookupError("Nieznana reguła")
        supplement_id = (changes.get("payload_json") or {}).get("supplement_id")
        if supplement_id is not None and session.get(Supplement, supplement_id) is None:
            raise LookupError("Nieznany suplement")
        values = {
            "kind": row.kind,
            "local_time": changes.get("local_time", row.local_time),
            "condition_type": changes.get("condition_type", row.condition_type),
            "condition_threshold": changes.get("condition_threshold", row.condition_threshold),
        }
        _validate_rule(**values)
        for key, value in changes.items():
            setattr(row, key, value)
        return _rule_row(row)


def _rule_row(row: ReminderRule) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "title": row.title,
        "local_time": row.local_time,
        "timezone": row.timezone,
        "schedule": row.schedule_json,
        "condition_type": row.condition_type,
        "condition_threshold": row.condition_threshold,
        "condition_window_hours": row.condition_window_hours,
        "payload": row.payload_json,
        "status": row.status,
    }


def list_reminder_rules(include_drafts: bool = True) -> list[dict]:
    with get_session() as session:
        stmt = select(ReminderRule).order_by(ReminderRule.id)
        if not include_drafts:
            stmt = stmt.where(ReminderRule.status != "draft")
        return [_rule_row(row) for row in session.execute(stmt).scalars().all()]


def mark_data_freshness(
    session,
    metric: str,
    source: str,
    observed_through: dt.date,
    received_at: dt.datetime | None = None,
) -> None:
    received = received_at or dt.datetime.now(dt.timezone.utc)
    excluded = pg_insert(DataFreshness).excluded
    session.execute(
        pg_insert(DataFreshness)
        .values(
            metric=metric,
            source=source,
            observed_through=observed_through,
            received_at=received,
        )
        .on_conflict_do_update(
            constraint="uq_data_freshness_metric_source",
            set_={
                "observed_through": func.greatest(
                    DataFreshness.observed_through, excluded.observed_through
                ),
                "received_at": case(
                    (
                        excluded.observed_through > DataFreshness.observed_through,
                        excluded.received_at,
                    ),
                    (
                        excluded.observed_through == DataFreshness.observed_through,
                        func.greatest(DataFreshness.received_at, excluded.received_at),
                    ),
                    else_=DataFreshness.received_at,
                ),
            },
        )
    )


def _scheduled_utc(rule: ReminderRule, day: dt.date) -> dt.datetime:
    hour, minute = map(int, rule.local_time.split(":"))
    zone = ZoneInfo(rule.timezone)
    wall = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=zone).replace(fold=0)
    roundtrip = wall.astimezone(dt.timezone.utc).astimezone(zone)
    if (roundtrip.hour, roundtrip.minute) != (hour, minute):
        wall = roundtrip
    return wall.astimezone(dt.timezone.utc)


def _fresh(session, metric: str, day: dt.date, now: dt.datetime) -> bool:
    matching_id = session.execute(
        select(DataFreshness.id)
        .where(
            DataFreshness.metric == metric,
            DataFreshness.observed_through >= day,
            DataFreshness.received_at
            >= now - dt.timedelta(hours=settings.reminder_data_fresh_hours),
        )
        .limit(1)
    ).scalar_one_or_none()
    return matching_id is not None


def _condition_result(
    session,
    rule: ReminderRule,
    now: dt.datetime,
    scheduled_for: dt.datetime,
) -> bool | None:
    if rule.condition_type is None:
        return True
    zone = ZoneInfo(rule.timezone)
    day = scheduled_for.astimezone(zone).date()
    if rule.condition_type == "steps_below":
        if not _fresh(session, "steps", day, now):
            return None
        steps = session.execute(
            select(func.max(DailyActivity.steps)).where(DailyActivity.date == day)
        ).scalar_one()
        return None if steps is None else steps < rule.condition_threshold
    if rule.condition_type == "protein_below":
        if not _fresh(session, "nutrition", day, now):
            return None
        protein = session.execute(
            select(NutritionDay.protein_g).where(NutritionDay.date == day)
        ).scalar_one_or_none()
        return None if protein is None else protein < rule.condition_threshold
    metric = "workouts"
    if not _fresh(session, metric, day, now):
        return None
    hours = rule.condition_window_hours or 24
    stmt = select(func.count(Workout.id)).where(
        Workout.started_at >= scheduled_for - dt.timedelta(hours=hours),
        Workout.started_at <= scheduled_for,
    )
    if rule.condition_type == "no_run":
        stmt = stmt.where(Workout.sport.in_(RUN_SPORTS))
    return session.execute(stmt).scalar_one() == 0


def materialize_due_occurrences(now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now(dt.timezone.utc)
    created = 0
    with get_session() as session:
        rules = session.execute(
            select(ReminderRule).where(ReminderRule.status == "active")
        ).scalars().all()
        for rule in rules:
            local_today = now.astimezone(ZoneInfo(rule.timezone)).date()
            weekdays = (rule.schedule_json or {}).get("weekdays", list(range(7)))
            for local_day in (local_today - dt.timedelta(days=1), local_today):
                if local_day.weekday() not in weekdays:
                    continue
                scheduled = _scheduled_utc(rule, local_day)
                if scheduled > now:
                    continue
                if (
                    local_day < local_today
                    and now > scheduled + dt.timedelta(hours=2)
                ):
                    continue
                missed = now > scheduled + dt.timedelta(hours=2)
                occurrence_id = session.execute(
                    pg_insert(ReminderOccurrence)
                    .values(
                        rule_id=rule.id,
                        scheduled_for=scheduled,
                        status="skipped_misfire" if missed else "pending",
                        attempts=0,
                        next_attempt_at=None if missed else scheduled,
                    )
                    .on_conflict_do_nothing(constraint="uq_reminder_occurrence_schedule")
                    .returning(ReminderOccurrence.id)
                ).scalar_one_or_none()
                created += occurrence_id is not None
    return created


def evaluate_due_occurrences(now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    ready = retried = skipped = 0
    with get_session() as session:
        occurrences = session.execute(
            select(ReminderOccurrence)
            .where(
                ReminderOccurrence.status.in_(["pending", "retrying", "snoozed"]),
                ReminderOccurrence.next_attempt_at <= now,
            )
            .with_for_update(skip_locked=True)
        ).scalars().all()
        for occurrence in occurrences:
            rule = session.get(ReminderRule, occurrence.rule_id)
            if rule is None or rule.status != "active":
                occurrence.status = "cancelled"
                session.execute(
                    update(NotificationOutbox)
                    .where(NotificationOutbox.occurrence_id == occurrence.id)
                    .values(status="cancelled")
                )
                skipped += 1
                continue
            outcome = _condition_result(
                session, rule, now, occurrence.scheduled_for
            )
            occurrence.evaluated_at = now
            occurrence.attempts += 1
            if outcome is None:
                if now < occurrence.scheduled_for + dt.timedelta(hours=2):
                    occurrence.status = "retrying"
                    occurrence.next_attempt_at = now + dt.timedelta(minutes=15)
                    retried += 1
                else:
                    occurrence.status = "skipped_stale"
                    session.execute(
                        update(NotificationOutbox)
                        .where(NotificationOutbox.occurrence_id == occurrence.id)
                        .values(status="cancelled")
                    )
                    skipped += 1
                continue
            if not outcome:
                occurrence.status = "skipped_condition"
                session.execute(
                    update(NotificationOutbox)
                    .where(NotificationOutbox.occurrence_id == occurrence.id)
                    .values(status="cancelled")
                )
                skipped += 1
                continue
            occurrence.status = "queued"
            session.execute(
                pg_insert(NotificationOutbox)
                .values(
                    occurrence_id=occurrence.id,
                    payload_json={
                        "text": f"⏰ {rule.title}",
                        "kind": rule.kind,
                        "supplement_id": (rule.payload_json or {}).get("supplement_id"),
                    },
                    status="pending",
                    attempts=0,
                    available_at=now,
                )
                .on_conflict_do_update(
                    constraint="uq_notification_outbox_occurrence",
                    set_={
                        "payload_json": {
                            "text": f"⏰ {rule.title}",
                            "kind": rule.kind,
                            "supplement_id": (rule.payload_json or {}).get(
                                "supplement_id"
                            ),
                        },
                        "status": "pending",
                        "available_at": now,
                        "sent_at": None,
                        "last_error": None,
                    },
                )
            )
            ready += 1
    return {"queued": ready, "retrying": retried, "skipped": skipped}


def _claim_notification(outbox_id: int, now: dt.datetime) -> tuple[dict, int] | None:
    """Atomowo przejmij jeden pending outbox; najwyżej jeden proces wygrywa."""
    with get_session() as session:
        claimed = session.execute(
            update(NotificationOutbox)
            .where(
                NotificationOutbox.id == outbox_id,
                NotificationOutbox.status == "pending",
            )
            .values(
                status="sending",
                attempts=NotificationOutbox.attempts + 1,
                available_at=now,
            )
            .returning(
                NotificationOutbox.payload_json,
                NotificationOutbox.occurrence_id,
            )
        ).one_or_none()
        if claimed is None:
            return None
        return dict(claimed.payload_json), claimed.occurrence_id


def dispatch_pending_notifications(now: dt.datetime | None = None) -> dict:
    """Wyślij outbox.

    Błąd po rozpoczęciu wywołania Telegrama jest stanem delivery_unknown i
    nie jest ponawiany automatycznie, żeby nie dublować wiadomości.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return {"sent": 0, "unknown": 0, "disabled": True}
    with get_session() as session:
        stuck = session.execute(
            select(NotificationOutbox).where(
                NotificationOutbox.status == "sending",
                NotificationOutbox.available_at < now - dt.timedelta(minutes=10),
            )
        ).scalars().all()
        for item in stuck:
            item.status = "delivery_unknown"
            item.last_error = "Proces zakończył się podczas wysyłki"
        ids = list(
            session.execute(
                select(NotificationOutbox.id)
                .where(
                    NotificationOutbox.status == "pending",
                    NotificationOutbox.available_at <= now,
                )
                .order_by(NotificationOutbox.id)
                .limit(20)
                .with_for_update(skip_locked=True)
            ).scalars()
        )

    sent = unknown = 0
    for outbox_id in ids:
        claimed = _claim_notification(outbox_id, now)
        if claimed is None:
            continue
        payload, occurrence_id = claimed
        try:
            from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

            buttons = [
                InlineKeyboardButton(
                    "Wzięte" if payload.get("kind") == "supplement" else "Zrobione",
                    callback_data=f"reminder:{occurrence_id}:taken"
                    if payload.get("kind") == "supplement"
                    else f"reminder:{occurrence_id}:done",
                ),
                InlineKeyboardButton("Pomiń", callback_data=f"reminder:{occurrence_id}:skip"),
                InlineKeyboardButton("Za 30 min", callback_data=f"reminder:{occurrence_id}:snooze"),
            ]

            async def _send() -> None:
                bot = Bot(token=settings.telegram_bot_token)
                await bot.send_message(
                    chat_id=settings.telegram_chat_id,
                    text=payload["text"],
                    reply_markup=InlineKeyboardMarkup([buttons]),
                )

            asyncio.run(_send())
        except Exception as exc:
            logger.exception("Niepewny wynik wysyłki przypomnienia outbox=%s", outbox_id)
            with get_session() as session:
                item = session.get(NotificationOutbox, outbox_id)
                if item:
                    item.status = "delivery_unknown"
                    item.last_error = str(exc)[:1000]
                occurrence = session.get(ReminderOccurrence, occurrence_id)
                if occurrence:
                    occurrence.status = "delivery_unknown"
            unknown += 1
            continue
        with get_session() as session:
            item = session.get(NotificationOutbox, outbox_id)
            if item:
                item.status = "sent"
                item.sent_at = dt.datetime.now(dt.timezone.utc)
            occurrence = session.get(ReminderOccurrence, occurrence_id)
            if occurrence:
                occurrence.status = "delivered"
                occurrence.delivered_at = dt.datetime.now(dt.timezone.utc)
        sent += 1
    return {"sent": sent, "unknown": unknown, "disabled": False}


def retry_unknown_notification(outbox_id: int) -> None:
    with get_session() as session:
        item = session.get(NotificationOutbox, outbox_id)
        if item is None or item.status != "delivery_unknown":
            raise LookupError("Brak niepewnej wysyłki o tym numerze")
        item.status = "pending"
        item.available_at = dt.datetime.now(dt.timezone.utc)
        item.last_error = None
        occurrence = session.get(ReminderOccurrence, item.occurrence_id)
        if occurrence:
            occurrence.status = "queued"


def list_unknown_notifications() -> list[dict]:
    with get_session() as session:
        rows = session.execute(
            select(NotificationOutbox, ReminderOccurrence, ReminderRule)
            .join(ReminderOccurrence, ReminderOccurrence.id == NotificationOutbox.occurrence_id)
            .join(ReminderRule, ReminderRule.id == ReminderOccurrence.rule_id)
            .where(NotificationOutbox.status == "delivery_unknown")
            .order_by(NotificationOutbox.id.desc())
        ).all()
        return [
            {
                "id": outbox.id,
                "occurrence_id": occurrence.id,
                "title": rule.title,
                "scheduled_for": occurrence.scheduled_for.isoformat(),
                "attempts": outbox.attempts,
                "last_error": outbox.last_error,
            }
            for outbox, occurrence, rule in rows
        ]


def complete_occurrence(occurrence_id: int, action: str) -> str:
    if action not in {"done", "skip", "snooze", "taken"}:
        raise ValueError("Nieznana akcja przypomnienia")
    now = dt.datetime.now(dt.timezone.utc)
    with get_session() as session:
        occurrence = session.get(ReminderOccurrence, occurrence_id)
        if occurrence is None:
            raise LookupError("Nieznane przypomnienie")
        rule = session.get(ReminderRule, occurrence.rule_id)
        if action == "snooze":
            occurrence.status = "snoozed"
            occurrence.next_attempt_at = now + dt.timedelta(minutes=30)
            outbox = session.execute(
                select(NotificationOutbox)
                .where(NotificationOutbox.occurrence_id == occurrence.id)
            ).scalar_one_or_none()
            if outbox:
                outbox.status = "held"
                outbox.available_at = occurrence.next_attempt_at
                outbox.sent_at = None
                outbox.last_error = None
            return "Przypomnę ponownie za 30 minut."
        occurrence.status = "completed" if action in {"done", "taken"} else "skipped"
        occurrence.completed_at = now
        if rule and rule.kind == "supplement":
            supplement_id = (rule.payload_json or {}).get("supplement_id")
            scheduled_for = occurrence.scheduled_for
        else:
            supplement_id = None
            scheduled_for = None
    if supplement_id:
        record_supplement_intake(
            supplement_id,
            "taken" if action in {"done", "taken"} else "skipped",
            scheduled_for=scheduled_for,
            source="telegram",
        )
    return "Zapisane." if action != "skip" else "Oznaczone jako pominięte."
