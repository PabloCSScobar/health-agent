"""Deterministyczne, zbiorcze alerty o wzorcach w zapisanych danych."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from health_agent.db.models import (
    AgentMemory,
    BodyComposition,
    DataFreshness,
    DataSyncRange,
    ManualLog,
    NotificationOutbox,
    NutritionDay,
    ProactiveAlertEvent,
    ProactiveAlertSetting,
    ReminderOccurrence,
    ReminderRule,
    Workout,
)
from health_agent.db.session import get_session
from health_agent.settings import settings
from health_agent.tools.profile import PROFILE_AGENT

SYSTEM_KEY = "proactive_alerts"
TOPICS = ("no_training", "low_protein", "weight_rising")
TOPIC_LABELS = {
    "no_training": "Brak treningu przez 96 godzin",
    "low_protein": "Białko poniżej celu przez 3 dni",
    "weight_rising": "Wzrost średniej wagi między tygodniami",
}
UNRESOLVED_OUTBOX_STATUSES = ("pending", "sending", "delivery_unknown")


@dataclass(frozen=True)
class TopicResult:
    topic: str
    status: str  # triggered | clear | insufficient | cooldown
    reason: str
    message: str | None = None
    details: dict | None = None


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(str(value).replace(",", ".").split()[0])
    except (ValueError, IndexError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def ensure_config(session) -> ReminderRule:
    now = dt.datetime.now(dt.timezone.utc)
    for topic in TOPICS:
        session.execute(
            pg_insert(ProactiveAlertSetting).values(
                topic=topic, enabled=False, updated_at=now
            ).on_conflict_do_nothing(index_elements=["topic"])
        )
    session.execute(
        pg_insert(ReminderRule).values(
            system_key=SYSTEM_KEY, kind="proactive_alert", title="Proaktywne alerty",
            local_time="20:00", timezone=settings.summary_timezone,
            schedule_json={"weekdays": list(range(7))}, condition_type=None,
            condition_threshold=None, condition_window_hours=None, payload_json={},
            status="paused", created_at=now, updated_at=now,
        ).on_conflict_do_nothing(index_elements=["system_key"])
    )
    return session.execute(
        select(ReminderRule).where(ReminderRule.system_key == SYSTEM_KEY)
    ).scalar_one()


def mark_sync_range(
    session, metric: str, source: str, range_start: dt.date, range_end: dt.date,
    received_at: dt.datetime | None = None,
) -> None:
    if range_start > range_end:
        raise ValueError("Początek zakresu synchronizacji jest po jego końcu")
    received = received_at or dt.datetime.now(dt.timezone.utc)
    session.execute(
        pg_insert(DataSyncRange)
        .values(
            metric=metric, source=source, range_start=range_start,
            range_end=range_end, received_at=received,
        )
        .on_conflict_do_update(
            constraint="uq_data_sync_range", set_={"received_at": received}
        )
    )


def _is_fresh(session, metric: str, through: dt.date, now: dt.datetime) -> bool:
    return session.execute(
        select(DataFreshness.id).where(
            DataFreshness.metric == metric,
            DataFreshness.observed_through >= through,
            DataFreshness.received_at >= now - dt.timedelta(hours=settings.reminder_data_fresh_hours),
        ).limit(1)
    ).scalar_one_or_none() is not None


def _cooldown_active(session, topic: str, now: dt.datetime) -> bool:
    return session.execute(
        select(ProactiveAlertEvent.id)
        .join(ReminderOccurrence, ReminderOccurrence.id == ProactiveAlertEvent.occurrence_id)
        .join(NotificationOutbox, NotificationOutbox.occurrence_id == ReminderOccurrence.id)
        .where(
            ProactiveAlertEvent.topic == topic,
            or_(
                NotificationOutbox.status.in_(UNRESOLVED_OUTBOX_STATUSES),
                and_(
                    NotificationOutbox.status == "sent",
                    func.coalesce(
                        NotificationOutbox.sent_at, ProactiveAlertEvent.qualified_at
                    ) >= now - dt.timedelta(days=7),
                ),
            ),
        ).limit(1)
    ).scalar_one_or_none() is not None


def _no_training(session, scheduled_for: dt.datetime, now: dt.datetime) -> TopicResult:
    zone = ZoneInfo(settings.summary_timezone)
    start = scheduled_for - dt.timedelta(hours=96)
    start_day = start.astimezone(zone).date()
    end_day = scheduled_for.astimezone(zone).date()
    coverage = session.execute(
        select(DataSyncRange.id).where(
            DataSyncRange.metric == "workouts",
            DataSyncRange.source == "intervals_icu",
            DataSyncRange.range_start <= start_day,
            DataSyncRange.range_end >= end_day,
            DataSyncRange.received_at >= now - dt.timedelta(hours=settings.reminder_data_fresh_hours),
        ).limit(1)
    ).scalar_one_or_none()
    if coverage is None:
        return TopicResult("no_training", "insufficient", "Brak świeżej synchronizacji całego okna 96 godzin")
    workout_count = session.execute(
        select(func.count(Workout.id)).where(
            Workout.started_at >= start, Workout.started_at <= scheduled_for
        )
    ).scalar_one()
    dated_manual = ManualLog.payload_json["date"].as_string()
    manual_count = session.execute(
        select(func.count(ManualLog.id)).where(
            ManualLog.kind == "strength",
            or_(
                and_(
                    dated_manual.is_(None),
                    ManualLog.logged_at >= start,
                    ManualLog.logged_at <= scheduled_for,
                ),
                and_(
                    dated_manual >= start_day.isoformat(),
                    dated_manual <= end_day.isoformat(),
                ),
            ),
        )
    ).scalar_one()
    if workout_count + manual_count:
        return TopicResult("no_training", "clear", "W oknie 96 godzin jest zapisany trening")
    return TopicResult(
        "no_training", "triggered",
        f"Brak zapisanego treningu od {start.astimezone(zone):%d.%m %H:%M}",
        f"• W zapisanych danych nie ma treningu od 96 godzin (od {start.astimezone(zone):%d.%m %H:%M}).",
        {"window_start": start.isoformat(), "window_end": scheduled_for.isoformat()},
    )


def _profile_goal(session, key: str) -> float | None:
    value = session.execute(
        select(AgentMemory.value).where(
            AgentMemory.agent == PROFILE_AGENT, AgentMemory.key == key
        )
    ).scalar_one_or_none()
    return _number(value)


def _low_protein(session, scheduled_for: dt.datetime, now: dt.datetime) -> TopicResult:
    zone = ZoneInfo(settings.summary_timezone)
    day = scheduled_for.astimezone(zone).date()
    days = [day - dt.timedelta(days=offset) for offset in (3, 2, 1)]
    goal = _profile_goal(session, "cel_bialko_g_dzien")
    if goal is None:
        return TopicResult("low_protein", "insufficient", "Brak poprawnego dodatniego celu białka")
    if not _is_fresh(session, "nutrition", days[-1], now):
        return TopicResult("low_protein", "insufficient", "Dane żywieniowe nie są świeże")
    rows = dict(session.execute(
        select(NutritionDay.date, NutritionDay.protein_g).where(NutritionDay.date.in_(days))
    ).all())
    if len(rows) != 3 or any(rows.get(item) is None for item in days):
        return TopicResult("low_protein", "insufficient", "Brakuje wartości białka dla jednego z 3 zakończonych dni")
    values = [float(rows[item]) for item in days]
    if not all(math.isfinite(value) and value < goal for value in values):
        return TopicResult("low_protein", "clear", "Nie wszystkie 3 dni są poniżej celu")
    rendered = ", ".join(f"{item:%d.%m}: {value:.0f} g" for item, value in zip(days, values))
    return TopicResult(
        "low_protein", "triggered", f"Zapisane białko poniżej {goal:.0f} g przez 3 dni",
        f"• Zapisane białko było poniżej celu {goal:.0f} g przez 3 dni ({rendered}).",
        {"days": [item.isoformat() for item in days], "values_g": values, "goal_g": goal},
    )


def _weight_rising(session, scheduled_for: dt.datetime, now: dt.datetime) -> TopicResult:
    zone = ZoneInfo(settings.summary_timezone)
    end_day = scheduled_for.astimezone(zone).date() - dt.timedelta(days=1)
    start_day = end_day - dt.timedelta(days=13)
    goal = _profile_goal(session, "cel_waga_kg")
    if goal is None:
        return TopicResult("weight_rising", "insufficient", "Brak poprawnego dodatniego celu wagowego")
    start_local = dt.datetime.combine(start_day, dt.time.min, tzinfo=zone).astimezone(dt.timezone.utc)
    end_local = dt.datetime.combine(end_day + dt.timedelta(days=1), dt.time.min, tzinfo=zone).astimezone(dt.timezone.utc)
    rows = session.execute(
        select(BodyComposition.measured_at, BodyComposition.weight_kg)
        .where(
            BodyComposition.measured_at >= start_local,
            BodyComposition.measured_at < end_local,
            BodyComposition.weight_kg.is_not(None),
        ).order_by(BodyComposition.measured_at)
    ).all()
    per_day: dict[dt.date, float] = {}
    for measured_at, weight in rows:
        value = float(weight)
        if math.isfinite(value) and value > 0:
            per_day[measured_at.astimezone(zone).date()] = value
    first_days = [start_day + dt.timedelta(days=index) for index in range(7)]
    second_days = [start_day + dt.timedelta(days=index) for index in range(7, 14)]
    first = [per_day[item] for item in first_days if item in per_day]
    second = [per_day[item] for item in second_days if item in per_day]
    if len(first) < 4 or len(second) < 4:
        return TopicResult("weight_rising", "insufficient", "Potrzeba pomiarów z co najmniej 4 dni w każdym tygodniu")
    older = sum(first) / len(first)
    newer = sum(second) / len(second)
    change = newer - older
    if not (change >= 0.3 - 1e-9 and goal < older and goal < newer):
        return TopicResult("weight_rising", "clear", "Zmiana średniej nie spełnia progu lub nie jest przeciwna celowi redukcji")
    return TopicResult(
        "weight_rising", "triggered", f"Średnia wzrosła o {change:.1f} kg",
        f"• Średnia zapisana waga wzrosła z {older:.1f} do {newer:.1f} kg między kolejnymi tygodniami (cel {goal:.1f} kg).",
        {"period_start": start_day.isoformat(), "period_end": end_day.isoformat(),
         "older_avg_kg": round(older, 2), "newer_avg_kg": round(newer, 2),
         "change_kg": round(change, 2), "goal_kg": goal,
         "older_days": len(first), "newer_days": len(second)},
    )


_EVALUATORS = {
    "no_training": _no_training,
    "low_protein": _low_protein,
    "weight_rising": _weight_rising,
}


def evaluate_topics(session, scheduled_for: dt.datetime, now: dt.datetime,
                    topics: list[str], *, cooldown: bool = True) -> list[TopicResult]:
    results = []
    for topic in topics:
        if topic not in _EVALUATORS:
            raise ValueError("Nieznany temat proaktywnego alertu")
        result = _EVALUATORS[topic](session, scheduled_for, now)
        if result.status == "triggered" and cooldown and _cooldown_active(session, topic, now):
            result = TopicResult(topic, "cooldown", "Temat był już zakwalifikowany w ostatnich 7 dniach")
        results.append(result)
    return results


def build_payload(results: list[TopicResult]) -> dict:
    triggered = [result for result in results if result.status == "triggered"]
    text = "🔎 Obserwacje z zapisanych danych:\n" + "\n".join(result.message or "" for result in triggered)
    return {"text": text, "kind": "proactive_alert", "topics": [result.topic for result in triggered]}


def list_alert_settings(now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    zone = ZoneInfo(settings.summary_timezone)
    scheduled = dt.datetime.combine(now.astimezone(zone).date(), dt.time(20), tzinfo=zone).astimezone(dt.timezone.utc)
    with get_session() as session:
        ensure_config(session)
        settings_rows = session.execute(
            select(ProactiveAlertSetting).order_by(ProactiveAlertSetting.topic)
        ).scalars().all()
        results = {item.topic: item for item in evaluate_topics(session, scheduled, now, list(TOPICS))}
        history_rows = session.execute(
            select(ProactiveAlertEvent, NotificationOutbox)
            .join(ReminderOccurrence, ReminderOccurrence.id == ProactiveAlertEvent.occurrence_id)
            .join(NotificationOutbox, NotificationOutbox.occurrence_id == ReminderOccurrence.id)
            .order_by(ProactiveAlertEvent.qualified_at.desc()).limit(30)
        ).all()
        return {
            "settings": [
                {"topic": row.topic, "label": TOPIC_LABELS[row.topic], "enabled": row.enabled,
                 "evaluation": asdict(results[row.topic])}
                for row in settings_rows
            ],
            "history": [
                {"topic": event.topic, "label": TOPIC_LABELS[event.topic],
                 "qualified_at": event.qualified_at.isoformat(), "delivery_status": outbox.status,
                 "details": event.details_json}
                for event, outbox in history_rows
            ],
        }


def update_alert_setting(topic: str, enabled: bool) -> dict:
    if topic not in TOPICS:
        raise LookupError("Nieznany temat proaktywnego alertu")
    with get_session() as session:
        ensure_config(session)
        row = session.get(ProactiveAlertSetting, topic)
        if row is None:
            raise LookupError("Brak ustawienia po migracji bazy")
        row.enabled = enabled
        rule = session.execute(
            select(ReminderRule).where(ReminderRule.system_key == SYSTEM_KEY).with_for_update()
        ).scalar_one()
        any_enabled = session.execute(
            select(func.count(ProactiveAlertSetting.topic)).where(ProactiveAlertSetting.enabled.is_(True))
        ).scalar_one() > 0
        rule.status = "active" if any_enabled else "paused"
        if not enabled:
            cancelled_ids = list(session.execute(
                update(ReminderOccurrence).where(
                    ReminderOccurrence.rule_id == rule.id,
                    ReminderOccurrence.status.in_(("pending", "retrying", "queued", "snoozed")),
                ).values(status="cancelled").returning(ReminderOccurrence.id)
            ).scalars())
            if cancelled_ids:
                session.execute(
                    update(NotificationOutbox).where(
                        NotificationOutbox.occurrence_id.in_(cancelled_ids),
                        NotificationOutbox.status.in_(("pending", "held")),
                    ).values(status="cancelled")
                )
        return {"topic": topic, "enabled": enabled}
