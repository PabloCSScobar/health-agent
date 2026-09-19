from __future__ import annotations

import datetime as dt
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from PIL import Image
from sqlalchemy import delete, func, select
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from health_agent.api.app import app
from health_agent.db.models import (
    CorrelationResult,
    DailyActivity,
    DataFreshness,
    DashboardSession,
    Knowledge,
    NotificationOutbox,
    ProgressPhoto,
    ReminderOccurrence,
    ReminderRule,
    Supplement,
    SupplementIntake,
)
from health_agent.db.session import get_session
from health_agent.settings import settings
from health_agent.tools import correlations
from health_agent.tools.photos import delete_progress_photo, save_progress_photo
from health_agent.tools.reminders import (
    activate_reminder_rule,
    complete_occurrence,
    evaluate_due_occurrences,
    materialize_due_occurrences,
    mark_data_freshness,
    propose_reminder_rule,
    update_reminder_rule,
)


@unittest.skipUnless(settings.app_env == "test", "wymaga APP_ENV=test i osobnej bazy")
class FeatureStackPostgresTest(unittest.TestCase):
    def tearDown(self) -> None:
        if settings.app_env != "test":
            raise RuntimeError("Odmowa czyszczenia poza APP_ENV=test")
        with get_session() as session:
            session.execute(delete(NotificationOutbox))
            session.execute(delete(ReminderOccurrence))
            session.execute(delete(ReminderRule))
            session.execute(delete(SupplementIntake))
            session.execute(delete(Supplement))
            session.execute(delete(CorrelationResult))
            session.execute(delete(DataFreshness).where(DataFreshness.source == "test"))
            session.execute(delete(DailyActivity).where(DailyActivity.source == "test"))
            session.execute(
                delete(Knowledge).where(Knowledge.source_agent == "correlations")
            )
            session.execute(delete(ProgressPhoto))
            session.execute(delete(DashboardSession))

    def test_correlation_publish_is_idempotent_for_period(self) -> None:
        start = dt.date(2026, 1, 1)
        rows = [
            {
                "date": start + dt.timedelta(days=index),
                "sleep_h": 5 + index / 10,
                "sleep_score": None,
                "hrv": 40 + index,
                "wellbeing": None,
                "steps": None,
                "run_load": None,
                "run_efficiency": None,
                "run_start_hour": None,
            }
            for index in range(25)
        ]
        end = start + dt.timedelta(days=83)
        with patch.object(correlations, "daily_frame", return_value=rows):
            first = correlations.publish_correlations(end)
            second = correlations.publish_correlations(end)
        self.assertEqual(
            [item["knowledge_id"] for item in first],
            [item["knowledge_id"] for item in second],
        )
        with get_session() as session:
            self.assertEqual(
                session.execute(select(func.count(CorrelationResult.id))).scalar_one(),
                5,
            )
            self.assertEqual(
                session.execute(
                    select(func.count(Knowledge.id)).where(
                        Knowledge.source_agent == "correlations"
                    )
                ).scalar_one(),
                1,
            )

    def test_photo_exact_byte_dedup_keeps_one_ready_row(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (12, 10), color="blue").save(buffer, format="JPEG")
        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            settings, "progress_photos_dir", tmp_dir
        ):
            first = save_progress_photo(
                buffer.getvalue(),
                captured_date=dt.date(2026, 9, 19),
                view="front",
                source="test",
            )
            second = save_progress_photo(
                buffer.getvalue(),
                captured_date=dt.date(2026, 9, 19),
                view="front",
                source="test",
            )
            self.assertFalse(first["deduplicated"])
            self.assertTrue(second["deduplicated"])
            self.assertEqual(
                len([path for path in Path(tmp_dir).iterdir() if not path.name.startswith(".")]),
                1,
            )
        with get_session() as session:
            self.assertEqual(
                session.execute(select(func.count(ProgressPhoto.id))).scalar_one(),
                1,
            )

    def test_deleted_photo_can_be_uploaded_again_without_orphan(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (9, 7), color="red").save(buffer, format="PNG")
        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            settings, "progress_photos_dir", tmp_dir
        ):
            first = save_progress_photo(
                buffer.getvalue(),
                captured_date=dt.date(2026, 9, 18),
                view="front",
                source="test",
            )
            self.assertTrue(delete_progress_photo(first["id"]))
            second = save_progress_photo(
                buffer.getvalue(),
                captured_date=dt.date(2026, 9, 19),
                view="side",
                source="test",
            )
            self.assertFalse(second["deduplicated"])
            self.assertEqual(second["captured_date"], "2026-09-19")
            self.assertEqual(second["view"], "side")
            files = [
                path.name for path in Path(tmp_dir).iterdir()
                if not path.name.startswith(".")
            ]
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].startswith("2026-09-19_side_"))

    def test_active_rule_materializes_once_and_queues_once(self) -> None:
        rule_id = int(
            propose_reminder_rule("Test", local_time="20:00")
            .split("#", 1)[1]
            .split(":", 1)[0]
        )
        activate_reminder_rule(rule_id)
        local = dt.datetime(2026, 9, 19, 20, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
        now = local.astimezone(dt.timezone.utc)
        self.assertEqual(materialize_due_occurrences(now), 1)
        self.assertEqual(materialize_due_occurrences(now), 0)
        self.assertEqual(evaluate_due_occurrences(now)["queued"], 1)
        self.assertEqual(evaluate_due_occurrences(now)["queued"], 0)
        with get_session() as session:
            self.assertEqual(
                session.execute(select(func.count(ReminderOccurrence.id))).scalar_one(),
                1,
            )
            self.assertEqual(
                session.execute(select(func.count(NotificationOutbox.id))).scalar_one(),
                1,
            )

    def test_rule_update_rejects_null_required_fields(self) -> None:
        rule_id = int(
            propose_reminder_rule("Test", local_time="20:00")
            .split("#", 1)[1]
            .split(":", 1)[0]
        )
        for field in ("title", "local_time", "timezone", "schedule_json", "status"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                update_reminder_rule(rule_id, **{field: None})

    def test_midnight_restart_uses_scheduled_day_for_condition(self) -> None:
        rule_id = int(
            propose_reminder_rule(
                "Wieczorne kroki",
                local_time="23:30",
                condition_type="steps_below",
                condition_threshold=1000,
            )
            .split("#", 1)[1]
            .split(":", 1)[0]
        )
        activate_reminder_rule(rule_id)
        local_now = dt.datetime(
            2026, 9, 20, 0, 30, tzinfo=ZoneInfo("Europe/Warsaw")
        )
        now = local_now.astimezone(dt.timezone.utc)
        scheduled_day = dt.date(2026, 9, 19)
        with get_session() as session:
            session.add(DailyActivity(source="test", date=scheduled_day, steps=500))
            mark_data_freshness(session, "steps", "test", scheduled_day, now)
        self.assertEqual(materialize_due_occurrences(now), 1)
        self.assertEqual(evaluate_due_occurrences(now)["queued"], 1)

    def test_freshness_backfill_does_not_regress_current_marker(self) -> None:
        current_day = dt.date(2026, 9, 20)
        first_received = dt.datetime(2026, 9, 20, 8, tzinfo=dt.timezone.utc)
        later_backfill = first_received + dt.timedelta(hours=1)
        with get_session() as session:
            mark_data_freshness(
                session, "steps", "test", current_day, first_received
            )
            mark_data_freshness(
                session,
                "steps",
                "test",
                current_day - dt.timedelta(days=3),
                later_backfill,
            )
        with get_session() as session:
            row = session.execute(
                select(DataFreshness).where(
                    DataFreshness.metric == "steps",
                    DataFreshness.source == "test",
                )
            ).scalar_one()
            self.assertEqual(row.observed_through, current_day)
            self.assertEqual(row.received_at, first_received)

    def test_snooze_reevaluates_condition_before_requeue(self) -> None:
        rule_id = int(
            propose_reminder_rule(
                "Kroki",
                local_time="20:00",
                condition_type="steps_below",
                condition_threshold=1000,
            )
            .split("#", 1)[1]
            .split(":", 1)[0]
        )
        activate_reminder_rule(rule_id)
        day = dt.date(2026, 9, 19)
        now = dt.datetime(2026, 9, 19, 20, 0, tzinfo=ZoneInfo("Europe/Warsaw")).astimezone(
            dt.timezone.utc
        )
        with get_session() as session:
            session.add(DailyActivity(source="test", date=day, steps=500))
            mark_data_freshness(session, "steps", "test", day, now)
        self.assertEqual(materialize_due_occurrences(now), 1)
        self.assertEqual(evaluate_due_occurrences(now)["queued"], 1)
        with get_session() as session:
            occurrence_id = session.execute(
                select(ReminderOccurrence.id).where(ReminderOccurrence.rule_id == rule_id)
            ).scalar_one()
        complete_occurrence(occurrence_id, "snooze")
        later = now + dt.timedelta(minutes=31)
        with get_session() as session:
            activity = session.execute(
                select(DailyActivity).where(
                    DailyActivity.source == "test", DailyActivity.date == day
                )
            ).scalar_one()
            activity.steps = 1500
            mark_data_freshness(session, "steps", "test", day, later)
        self.assertEqual(evaluate_due_occurrences(later)["skipped"], 1)
        with get_session() as session:
            outbox_status = session.execute(
                select(NotificationOutbox.status).where(
                    NotificationOutbox.occurrence_id == occurrence_id
                )
            ).scalar_one()
            self.assertEqual(outbox_status, "cancelled")

    def test_dashboard_login_session_and_csrf(self) -> None:
        password = "synthetic-dashboard-password"
        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            patch.object(settings, "dashboard_password_hash", PasswordHasher().hash(password)),
            patch.object(settings, "dashboard_cookie_secure", False),
            patch.object(settings, "progress_photos_dir", tmp_dir),
            TestClient(app) as client,
        ):
            self.assertEqual(
                client.post("/dash/login", json={"password": "wrong"}).status_code,
                401,
            )
            self.assertEqual(
                client.post("/dash/login", json={"password": password}).status_code,
                200,
            )
            overview = client.get("/dash/api/overview?days=7")
            self.assertEqual(overview.status_code, 200)
            csrf = overview.json()["csrf"]
            payload = {"name": "Test", "dose": "1"}
            self.assertEqual(
                client.post("/dash/api/supplements", json=payload).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    "/dash/api/supplements",
                    json=payload,
                    headers={
                        "X-CSRF-Token": csrf,
                        "Origin": "http://testserver",
                    },
                ).status_code,
                200,
            )


if __name__ == "__main__":
    unittest.main()
