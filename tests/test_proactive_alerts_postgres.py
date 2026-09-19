from __future__ import annotations

import datetime as dt
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select

from health_agent.db.models import (
    AgentMemory, BodyComposition, DataFreshness, DataSyncRange, ManualLog,
    NotificationOutbox, NutritionDay, ProactiveAlertEvent, ProactiveAlertSetting,
    ReminderOccurrence, ReminderRule,
)
from health_agent.db.session import get_session
from health_agent.tools.proactive_alerts import (
    SYSTEM_KEY, _cooldown_active, evaluate_topics, mark_sync_range, update_alert_setting,
)
from health_agent.tools.reminders import (
    mark_data_freshness, materialize_due_occurrences, evaluate_due_occurrences,
)
from tests.postgres_guard import is_isolated_test_database, require_isolated_test_database


@unittest.skipUnless(
    is_isolated_test_database(), "wymaga jawnie potwierdzonej izolowanej bazy health_test"
)
class ProactiveAlertsPostgresTest(unittest.TestCase):
    def setUp(self) -> None:
        update_alert_setting("no_training", False)
        update_alert_setting("low_protein", False)
        update_alert_setting("weight_rising", False)

    def tearDown(self) -> None:
        require_isolated_test_database()
        with get_session() as session:
            session.execute(delete(ProactiveAlertEvent))
            session.execute(delete(NotificationOutbox))
            session.execute(delete(ReminderOccurrence))
            session.execute(delete(DataSyncRange))
            session.execute(delete(DataFreshness))
            session.execute(delete(ManualLog).where(ManualLog.kind == "strength"))
            session.execute(delete(BodyComposition).where(BodyComposition.source == "test"))
            session.execute(delete(NutritionDay))
            session.execute(delete(AgentMemory).where(AgentMemory.agent == "user_profile"))
            session.execute(delete(ReminderRule).where(ReminderRule.system_key == SYSTEM_KEY))
            session.execute(delete(ProactiveAlertSetting))

    @staticmethod
    def scheduled(day: dt.date) -> dt.datetime:
        return dt.datetime.combine(day, dt.time(20), tzinfo=ZoneInfo("Europe/Warsaw")).astimezone(dt.timezone.utc)

    def test_training_requires_full_fresh_range_and_includes_manual_boundary(self) -> None:
        now = self.scheduled(dt.date(2026, 9, 19))
        with get_session() as session:
            result = evaluate_topics(session, now, now, ["no_training"], cooldown=False)[0]
            self.assertEqual(result.status, "insufficient")
            mark_sync_range(session, "workouts", "intervals_icu", dt.date(2026, 9, 15), dt.date(2026, 9, 19), now)
            session.add(ManualLog(kind="strength", logged_at=now - dt.timedelta(hours=96), payload_json={}))
        with get_session() as session:
            result = evaluate_topics(session, now, now, ["no_training"], cooldown=False)[0]
            self.assertEqual(result.status, "clear")
            session.execute(delete(ManualLog).where(ManualLog.kind == "strength"))
            session.add(ManualLog(
                kind="strength", logged_at=now, payload_json={"date": "2026-09-14"}
            ))
        with get_session() as session:
            result = evaluate_topics(session, now, now, ["no_training"], cooldown=False)[0]
            self.assertEqual(result.status, "triggered")

    def test_unresolved_delivery_blocks_topic_even_after_seven_days(self) -> None:
        now = self.scheduled(dt.date(2026, 9, 19))
        update_alert_setting("no_training", True)
        with get_session() as session:
            mark_sync_range(session, "workouts", "intervals_icu", dt.date(2026, 9, 15), dt.date(2026, 9, 19), now)
        materialize_due_occurrences(now)
        evaluate_due_occurrences(now)
        with get_session() as session:
            event = session.execute(select(ProactiveAlertEvent)).scalar_one()
            outbox = session.execute(select(NotificationOutbox)).scalar_one()
            event.qualified_at = now - dt.timedelta(days=8)
            outbox.status = "delivery_unknown"
        with get_session() as session:
            self.assertTrue(_cooldown_active(session, "no_training", now))

    def test_missing_data_retries_only_until_22_and_never_backfills(self) -> None:
        now = self.scheduled(dt.date(2026, 9, 19))
        update_alert_setting("no_training", True)
        self.assertEqual(materialize_due_occurrences(now), 1)
        self.assertEqual(evaluate_due_occurrences(now)["retrying"], 1)
        deadline = now + dt.timedelta(hours=2)
        with get_session() as session:
            occurrence = session.execute(select(ReminderOccurrence)).scalar_one()
            self.assertLessEqual(occurrence.next_attempt_at, deadline)
        self.assertEqual(evaluate_due_occurrences(deadline)["skipped"], 1)
        with get_session() as session:
            self.assertEqual(session.execute(select(func.count(NotificationOutbox.id))).scalar_one(), 0)

    def test_at_22_missing_topic_is_omitted_but_triggered_topic_is_sent(self) -> None:
        day = dt.date(2026, 9, 19)
        now = self.scheduled(day)
        update_alert_setting("no_training", True)
        update_alert_setting("low_protein", True)
        with get_session() as session:
            session.add(AgentMemory(agent="user_profile", key="cel_bialko_g_dzien", value="120"))
            for offset in (3, 2, 1):
                session.add(NutritionDay(date=day - dt.timedelta(days=offset), protein_g=80.0))
            mark_data_freshness(session, "nutrition", "test", day - dt.timedelta(days=1), now)
        materialize_due_occurrences(now)
        self.assertEqual(evaluate_due_occurrences(now)["retrying"], 1)
        self.assertEqual(evaluate_due_occurrences(now + dt.timedelta(hours=2))["queued"], 1)
        with get_session() as session:
            payload = session.execute(select(NotificationOutbox.payload_json)).scalar_one()
            self.assertEqual(payload["topics"], ["low_protein"])

    def test_protein_queues_once_and_cooldown_blocks_next_day(self) -> None:
        day = dt.date(2026, 9, 19)
        now = self.scheduled(day)
        update_alert_setting("low_protein", True)
        with get_session() as session:
            session.add(AgentMemory(agent="user_profile", key="cel_bialko_g_dzien", value="120"))
            for offset, value in zip((3, 2, 1), (80.0, 90.0, 100.0)):
                session.add(NutritionDay(date=day - dt.timedelta(days=offset), protein_g=value))
            mark_data_freshness(session, "nutrition", "test", day - dt.timedelta(days=1), now)
        self.assertEqual(materialize_due_occurrences(now), 1)
        self.assertEqual(evaluate_due_occurrences(now)["queued"], 1)
        self.assertEqual(evaluate_due_occurrences(now)["queued"], 0)
        with get_session() as session:
            self.assertEqual(session.execute(select(func.count(NotificationOutbox.id))).scalar_one(), 1)
            self.assertEqual(session.execute(select(func.count(ProactiveAlertEvent.id))).scalar_one(), 1)

        tomorrow = now + dt.timedelta(days=1)
        with get_session() as session:
            session.add(NutritionDay(date=day, protein_g=95.0))
            mark_data_freshness(session, "nutrition", "test", day, tomorrow)
        self.assertEqual(materialize_due_occurrences(tomorrow), 1)
        self.assertEqual(evaluate_due_occurrences(tomorrow)["skipped"], 1)
        with get_session() as session:
            self.assertEqual(session.execute(select(func.count(NotificationOutbox.id))).scalar_one(), 1)

    def test_weight_uses_last_daily_measurement_and_requires_four_days_per_week(self) -> None:
        day = dt.date(2026, 9, 19)
        now = self.scheduled(day)
        zone = ZoneInfo("Europe/Warsaw")
        with get_session() as session:
            session.add(AgentMemory(agent="user_profile", key="cel_waga_kg", value="75"))
            for index in (0, 1, 2, 3):
                measured = dt.datetime.combine(day - dt.timedelta(days=14-index), dt.time(8), tzinfo=zone)
                session.add(BodyComposition(source="test", measured_at=measured, weight_kg=80.0))
            for index in (7, 8, 9, 13):
                measured = dt.datetime.combine(day - dt.timedelta(days=14-index), dt.time(8), tzinfo=zone)
                session.add(BodyComposition(source="test", measured_at=measured, weight_kg=80.2))
                session.add(BodyComposition(source="test", measured_at=measured + dt.timedelta(hours=1), weight_kg=80.3))
        with get_session() as session:
            result = evaluate_topics(session, now, now, ["weight_rising"], cooldown=False)[0]
            self.assertEqual(result.status, "triggered")
            self.assertAlmostEqual(result.details["change_kg"], 0.3)

    def test_disable_waiting_on_evaluation_cancels_new_outbox(self) -> None:
        now = self.scheduled(dt.date(2026, 9, 19))
        update_alert_setting("no_training", True)
        materialize_due_occurrences(now)
        started = threading.Event()

        def disable() -> None:
            started.set()
            update_alert_setting("no_training", False)

        with ThreadPoolExecutor(max_workers=1) as pool:
            with get_session() as evaluating:
                occurrence = evaluating.execute(
                    select(ReminderOccurrence).with_for_update()
                ).scalar_one()
                future = pool.submit(disable)
                started.wait(timeout=5)
                time.sleep(0.05)
                occurrence.status = "queued"
                evaluating.add(NotificationOutbox(
                    occurrence_id=occurrence.id,
                    payload_json={"text": "test", "kind": "proactive_alert"},
                    status="pending", attempts=0, available_at=now,
                ))
            future.result(timeout=5)
        with get_session() as session:
            self.assertEqual(session.execute(select(ReminderOccurrence.status)).scalar_one(), "cancelled")
            self.assertEqual(session.execute(select(NotificationOutbox.status)).scalar_one(), "cancelled")

    def test_parallel_evaluation_creates_one_message_and_disabling_cancels_it(self) -> None:
        day = dt.date(2026, 9, 19)
        now = self.scheduled(day)
        update_alert_setting("no_training", True)
        with get_session() as session:
            mark_sync_range(session, "workouts", "intervals_icu", dt.date(2026, 9, 15), day, now)
        self.assertEqual(materialize_due_occurrences(now), 1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: evaluate_due_occurrences(now), range(2)))
        self.assertEqual(sum(item["queued"] for item in results), 1)
        update_alert_setting("no_training", False)
        with get_session() as session:
            outbox = session.execute(select(NotificationOutbox)).scalar_one()
            occurrence = session.execute(select(ReminderOccurrence)).scalar_one()
            self.assertEqual(outbox.status, "cancelled")
            self.assertEqual(occurrence.status, "cancelled")


if __name__ == "__main__":
    unittest.main()
