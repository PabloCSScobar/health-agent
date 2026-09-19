from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import delete, func, select

from health_agent.api.app import app
from health_agent.api.dashboard import COOKIE_NAME, _login_attempts, _token_hash
from health_agent.channels.telegram import handle_callback, handle_photo
from health_agent.db.models import (
    DashboardSession,
    NotificationOutbox,
    ProgressPhoto,
    ReminderOccurrence,
    ReminderRule,
)
from health_agent.db.session import get_session
from health_agent.settings import settings
from health_agent.tools.photos import reconcile_progress_photos, save_progress_photo
from health_agent.tools.reminders import (
    _claim_notification,
    activate_reminder_rule,
    dispatch_pending_notifications,
    evaluate_due_occurrences,
    materialize_due_occurrences,
    propose_reminder_rule,
    retry_unknown_notification,
)
from tests.postgres_guard import (
    is_isolated_test_database,
    require_isolated_test_database,
)


def _image_bytes(fmt: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 12), color="purple").save(buffer, format=fmt)
    return buffer.getvalue()


@unittest.skipUnless(
    is_isolated_test_database(), "wymaga jawnie potwierdzonej izolowanej bazy health_test"
)
class FeatureResiliencePostgresTest(unittest.TestCase):
    def tearDown(self) -> None:
        require_isolated_test_database()
        _login_attempts.clear()
        with get_session() as session:
            session.execute(delete(NotificationOutbox))
            session.execute(delete(ReminderOccurrence))
            session.execute(delete(ReminderRule))
            session.execute(delete(ProgressPhoto))
            session.execute(delete(DashboardSession))

    def _queue_text_reminder(self, now: dt.datetime) -> int:
        local_time = now.astimezone(ZoneInfo("Europe/Warsaw")).strftime("%H:%M")
        rule_id = int(
            propose_reminder_rule("Test odporności", local_time=local_time)
            .split("#", 1)[1]
            .split(":", 1)[0]
        )
        activate_reminder_rule(rule_id)
        self.assertEqual(materialize_due_occurrences(now), 1)
        self.assertEqual(evaluate_due_occurrences(now)["queued"], 1)
        with get_session() as session:
            return session.execute(
                select(NotificationOutbox.id)
                .join(ReminderOccurrence)
                .where(ReminderOccurrence.rule_id == rule_id)
            ).scalar_one()

    def test_two_dispatchers_send_one_message(self) -> None:
        now = dt.datetime(2026, 9, 19, 18, 0, tzinfo=dt.timezone.utc)
        outbox_id = self._queue_text_reminder(now)
        barrier = threading.Barrier(3)
        calls = 0
        calls_lock = threading.Lock()

        class FakeBot:
            def __init__(self, token: str):
                self.token = token

            async def send_message(self, **kwargs) -> None:
                nonlocal calls
                await asyncio.sleep(0.05)
                with calls_lock:
                    calls += 1

        def dispatch() -> dict:
            barrier.wait()
            return dispatch_pending_notifications(now)

        with (
            patch.object(settings, "telegram_bot_token", "test-token"),
            patch.object(settings, "telegram_chat_id", "123"),
            patch("telegram.Bot", FakeBot),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = [pool.submit(dispatch) for _ in range(2)]
            barrier.wait()
            results = [future.result(timeout=10) for future in futures]

        self.assertEqual(calls, 1)
        self.assertEqual(sum(result["sent"] for result in results), 1)
        with get_session() as session:
            outbox = session.get(NotificationOutbox, outbox_id)
            self.assertEqual(outbox.status, "sent")
            self.assertEqual(outbox.attempts, 1)

    def test_two_atomic_claims_for_same_outbox_have_one_winner(self) -> None:
        now = dt.datetime(2026, 9, 19, 18, 0, tzinfo=dt.timezone.utc)
        outbox_id = self._queue_text_reminder(now)
        barrier = threading.Barrier(3)

        def claim():
            barrier.wait()
            return _claim_notification(outbox_id, now)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim) for _ in range(2)]
            barrier.wait()
            claims = [future.result(timeout=10) for future in futures]

        self.assertEqual(sum(item is not None for item in claims), 1)
        with get_session() as session:
            outbox = session.get(NotificationOutbox, outbox_id)
            self.assertEqual(outbox.status, "sending")
            self.assertEqual(outbox.attempts, 1)

    def test_send_failure_is_quarantined_until_manual_retry(self) -> None:
        now = dt.datetime(2026, 9, 19, 18, 0, tzinfo=dt.timezone.utc)
        outbox_id = self._queue_text_reminder(now)

        class FailingBot:
            def __init__(self, token: str):
                self.token = token

            async def send_message(self, **kwargs) -> None:
                raise RuntimeError("synthetic uncertain delivery")

        class SuccessfulBot:
            calls = 0

            def __init__(self, token: str):
                self.token = token

            async def send_message(self, **kwargs) -> None:
                type(self).calls += 1

        with (
            patch.object(settings, "telegram_bot_token", "test-token"),
            patch.object(settings, "telegram_chat_id", "123"),
            patch("telegram.Bot", FailingBot),
        ):
            self.assertEqual(dispatch_pending_notifications(now)["unknown"], 1)

        with (
            patch.object(settings, "telegram_bot_token", "test-token"),
            patch.object(settings, "telegram_chat_id", "123"),
            patch("telegram.Bot", SuccessfulBot),
        ):
            self.assertEqual(dispatch_pending_notifications(now)["sent"], 0)
            self.assertEqual(SuccessfulBot.calls, 0)
            retry_unknown_notification(outbox_id)
            self.assertEqual(
                dispatch_pending_notifications(now + dt.timedelta(hours=1))["sent"], 1
            )

        self.assertEqual(SuccessfulBot.calls, 1)
        with get_session() as session:
            outbox = session.get(NotificationOutbox, outbox_id)
            self.assertEqual(outbox.status, "sent")
            self.assertEqual(outbox.attempts, 2)

    def test_stale_sending_claim_becomes_delivery_unknown(self) -> None:
        now = dt.datetime(2026, 9, 19, 18, 0, tzinfo=dt.timezone.utc)
        outbox_id = self._queue_text_reminder(now)
        with get_session() as session:
            outbox = session.get(NotificationOutbox, outbox_id)
            outbox.status = "sending"
            outbox.available_at = now - dt.timedelta(minutes=11)

        with (
            patch.object(settings, "telegram_bot_token", "test-token"),
            patch.object(settings, "telegram_chat_id", "123"),
        ):
            result = dispatch_pending_notifications(now)

        self.assertEqual(result["sent"], 0)
        with get_session() as session:
            outbox = session.get(NotificationOutbox, outbox_id)
            self.assertEqual(outbox.status, "delivery_unknown")
            self.assertIn("proces zakończył", outbox.last_error.lower())

    def test_concurrent_identical_photo_upload_keeps_one_file_and_row(self) -> None:
        data = _image_bytes()
        barrier = threading.Barrier(3)
        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            settings, "progress_photos_dir", tmp_dir
        ):
            def upload() -> dict:
                barrier.wait()
                return save_progress_photo(
                    data,
                    captured_date=dt.date(2026, 9, 19),
                    view="front",
                    source="test",
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(upload) for _ in range(2)]
                barrier.wait()
                results = [future.result(timeout=10) for future in futures]
            visible = [
                path for path in Path(tmp_dir).iterdir() if not path.name.startswith(".")
            ]

        self.assertEqual(sorted(item["deduplicated"] for item in results), [False, True])
        self.assertEqual(len(visible), 1)
        with get_session() as session:
            self.assertEqual(
                session.execute(select(func.count(ProgressPhoto.id))).scalar_one(), 1
            )

    def test_photo_write_failure_is_reconciled_without_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            settings, "progress_photos_dir", tmp_dir
        ):
            with patch(
                "health_agent.tools.photos.os.replace",
                side_effect=OSError("synthetic disk failure"),
            ):
                with self.assertRaises(OSError):
                    save_progress_photo(
                        _image_bytes(),
                        captured_date=dt.date(2026, 9, 19),
                        view="front",
                        source="test",
                    )
            result = reconcile_progress_photos()
            visible = [
                path for path in Path(tmp_dir).iterdir() if not path.name.startswith(".")
            ]
            temporary = list(Path(tmp_dir).glob(".*.tmp"))

        self.assertEqual(result["deleted_staging_rows"], 1)
        self.assertEqual(visible, [])
        self.assertEqual(temporary, [])
        with get_session() as session:
            self.assertEqual(
                session.execute(select(func.count(ProgressPhoto.id))).scalar_one(), 0
            )

    def test_expired_and_idle_sessions_are_revoked(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        cases = (
            ("expired", now, now - dt.timedelta(seconds=1)),
            ("idle", now - dt.timedelta(hours=13), now + dt.timedelta(days=1)),
        )
        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            settings, "progress_photos_dir", tmp_dir
        ):
            for label, last_seen, expires_at in cases:
                with self.subTest(label=label):
                    token = f"synthetic-{label}-token"
                    with get_session() as session:
                        row = DashboardSession(
                            token_hash=_token_hash(token),
                            csrf_token=f"csrf-{label}",
                            created_at=now - dt.timedelta(days=1),
                            last_seen_at=last_seen,
                            expires_at=expires_at,
                            revoked=False,
                        )
                        session.add(row)
                        session.flush()
                        session_id = row.id
                    with TestClient(app) as client:
                        client.cookies.set(COOKIE_NAME, token, path="/dash")
                        response = client.get("/dash/api/overview")
                    self.assertEqual(response.status_code, 401)
                    with get_session() as session:
                        self.assertTrue(session.get(DashboardSession, session_id).revoked)

    def test_login_cookie_rate_limit_and_security_headers(self) -> None:
        password = "synthetic-dashboard-password"
        _login_attempts.clear()
        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            patch.object(settings, "dashboard_password_hash", PasswordHasher().hash(password)),
            patch.object(settings, "dashboard_cookie_secure", True),
            patch.object(settings, "progress_photos_dir", tmp_dir),
            TestClient(app) as client,
        ):
            health = client.get("/health")
            self.assertEqual(health.headers["x-content-type-options"], "nosniff")
            self.assertEqual(health.headers["x-frame-options"], "DENY")
            self.assertIn("default-src 'self'", health.headers["content-security-policy"])
            for _ in range(5):
                self.assertEqual(
                    client.post("/dash/login", json={"password": "wrong"}).status_code,
                    401,
                )
            self.assertEqual(
                client.post("/dash/login", json={"password": "wrong"}).status_code,
                429,
            )
            _login_attempts.clear()
            response = client.post("/dash/login", json={"password": password})

        self.assertEqual(response.status_code, 200)
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("secure", cookie)
        self.assertIn("samesite=lax", cookie)
        self.assertIn("path=/dash", cookie)


@unittest.skipUnless(
    is_isolated_test_database(), "wymaga jawnie potwierdzonej izolowanej bazy health_test"
)
class TelegramHandlersPostgresTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        require_isolated_test_database()
        with get_session() as session:
            session.execute(delete(NotificationOutbox))
            session.execute(delete(ReminderOccurrence))
            session.execute(delete(ReminderRule))
            session.execute(delete(ProgressPhoto))

    async def test_photo_handler_uses_downloaded_bytes_without_real_telegram(self) -> None:
        data = _image_bytes("JPEG")
        telegram_file = SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(data))
        )
        photo = SimpleNamespace(
            file_size=len(data),
            get_file=AsyncMock(return_value=telegram_file),
        )
        message = SimpleNamespace(
            photo=[photo],
            caption="przód 2026-09-19 test syntetyczny",
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            message=message,
            effective_chat=SimpleNamespace(id=123),
        )
        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            patch.object(settings, "telegram_chat_id", "123"),
            patch.object(settings, "progress_photos_dir", tmp_dir),
        ):
            await handle_photo(update, SimpleNamespace())
            files = [
                path for path in Path(tmp_dir).iterdir() if not path.name.startswith(".")
            ]

        self.assertEqual(len(files), 1)
        message.reply_text.assert_awaited_once()
        with get_session() as session:
            row = session.execute(select(ProgressPhoto)).scalar_one()
            self.assertEqual(row.view, "front")
            self.assertEqual(row.note, "test syntetyczny")
            self.assertEqual(row.sha256, hashlib.sha256(data).hexdigest())

    async def test_callback_activates_draft_without_real_telegram(self) -> None:
        rule_id = int(
            propose_reminder_rule("Callback test", local_time="20:00")
            .split("#", 1)[1]
            .split(":", 1)[0]
        )
        query = SimpleNamespace(
            data=f"rule:{rule_id}:activate",
            answer=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_chat=SimpleNamespace(id=123),
        )
        with patch.object(settings, "telegram_chat_id", "123"):
            await handle_callback(update, SimpleNamespace())

        query.answer.assert_awaited_once()
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        with get_session() as session:
            self.assertEqual(session.get(ReminderRule, rule_id).status, "active")


if __name__ == "__main__":
    unittest.main()
