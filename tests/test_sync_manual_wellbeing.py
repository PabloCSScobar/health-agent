from __future__ import annotations

import asyncio
import datetime as dt
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from health_agent.agents.registry import SPECIALISTS
from health_agent.channels import telegram
from health_agent.ingest.sync import POLL_LOOKBACK_DAYS, POLL_MAX_BACKFILL_DAYS, _automatic_window
from health_agent.tools import recovery
from health_agent.tools.manual import ManualLogEntry, _format_confirmation
from health_agent.tools.manual_batch import _signature, _validate


class _WindowSession:
    def __init__(self, last_synced_date: dt.date | None):
        self.row = (
            SimpleNamespace(last_synced_date=last_synced_date)
            if last_synced_date is not None
            else None
        )

    def get(self, model, key):
        return self.row


class _RowsResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _RowsSession:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, statement):
        return _RowsResult(self.rows)


class SyncManualWellbeingTest(unittest.TestCase):
    def test_automatic_window_uses_overlap_and_reports_cap(self) -> None:
        today = dt.date(2026, 9, 19)
        oldest, truncated = _automatic_window(_WindowSession(None), today)
        self.assertEqual(oldest, today - dt.timedelta(days=POLL_LOOKBACK_DAYS))
        self.assertFalse(truncated)

        oldest, truncated = _automatic_window(_WindowSession(dt.date(2026, 1, 1)), today)
        self.assertEqual(oldest, today - dt.timedelta(days=POLL_MAX_BACKFILL_DAYS))
        self.assertTrue(truncated)

    def test_manual_batch_validation_and_canonical_payload_order(self) -> None:
        first = ManualLogEntry(kind="weight", payload={"fat_pct": 18, "weight_kg": 82}, text_original="waga")
        second = ManualLogEntry(kind="weight", payload={"weight_kg": 82, "fat_pct": 18}, text_original="waga")
        self.assertEqual(_signature(first), _signature(second))
        integer_weight = ManualLogEntry(kind="weight", payload={"weight_kg": 82}, text_original="waga")
        float_weight = ManualLogEntry(kind="weight", payload={"weight_kg": 82.0}, text_original="waga")
        self.assertEqual(_signature(integer_weight), _signature(float_weight))
        integer_strength = ManualLogEntry(
            kind="strength",
            payload={"cwiczenia": [{"serie": 4, "powtorzenia": 8, "ciezar_kg": 80}]},
            text_original="trening",
        )
        float_strength = ManualLogEntry(
            kind="strength",
            payload={"cwiczenia": [{"serie": 4.0, "powtorzenia": 8.0, "ciezar_kg": 80.0}]},
            text_original="trening",
        )
        self.assertEqual(_signature(integer_strength), _signature(float_strength))

        _validate([ManualLogEntry(kind="wellbeing", payload={"score": 1}, text_original="słabo")])
        _validate([ManualLogEntry(kind="wellbeing", payload={"score": 5}, text_original="świetnie")])
        for invalid in (0, 6, True, 2.5, "3"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                _validate([ManualLogEntry(kind="wellbeing", payload={"score": invalid}, text_original="x")])
        for invalid_date in (0, False, "", "20260919", "19-09-2026"):
            with self.subTest(invalid_date=invalid_date), self.assertRaises((TypeError, ValueError)):
                _validate(
                    [
                        ManualLogEntry(
                            kind="daily_calories",
                            payload={"calories_total": 2000, "date": invalid_date},
                            text_original="x",
                        )
                    ]
                )

    def test_wellbeing_and_note_confirmations_are_specific(self) -> None:
        wellbeing = ManualLogEntry(
            kind="wellbeing",
            payload={"score": 4, "note": "dobry sen"},
            text_original="czuję się dobrze",
        )
        note = ManualLogEntry(kind="note", payload={}, text_original="boli mnie łydka")
        self.assertEqual(_format_confirmation(wellbeing, 1), "✅ Zapisano samopoczucie: 4/5 — dobry sen")
        self.assertEqual(_format_confirmation(note, 2), "✅ Zapisano notatkę: boli mnie łydka")

    def test_wellbeing_history_uses_last_score_per_day_and_keeps_legacy_text(self) -> None:
        rows = [
            SimpleNamespace(
                kind="wellbeing", payload_json={"score": 2, "date": "2026-09-18", "note": "rano"},
                text_original="rano słabo", logged_at=dt.datetime(2026, 9, 18, 6, tzinfo=dt.timezone.utc),
            ),
            SimpleNamespace(
                kind="wellbeing", payload_json={"score": 4, "date": "2026-09-18"},
                text_original="wieczorem lepiej", logged_at=dt.datetime(2026, 9, 18, 18, tzinfo=dt.timezone.utc),
            ),
            SimpleNamespace(
                kind="note", payload_json={}, text_original="starsza notatka bez oceny",
                logged_at=dt.datetime(2026, 9, 19, 8, tzinfo=dt.timezone.utc),
            ),
        ]

        @contextmanager
        def fake_session():
            yield _RowsSession(rows)

        with patch.object(recovery, "get_session", fake_session):
            result = recovery.get_wellbeing_history(2, dt.date(2026, 9, 19))

        self.assertEqual(result["latest_score_by_day"], {"2026-09-18": 4})
        self.assertEqual(result["avg_score"], 4.0)
        self.assertEqual(result["entries"][-1]["score"], None)
        self.assertEqual(result["entries"][-1]["note"], "starsza notatka bez oceny")

    def test_recovery_agent_has_wellbeing_tool(self) -> None:
        tool_names = {tool.__name__ for tool in SPECIALISTS["recovery"][1]}
        self.assertIn("get_wellbeing_history", tool_names)

    def test_telegram_authorization_is_fail_closed(self) -> None:
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=123))
        with patch.object(telegram.settings, "telegram_chat_id", None):
            self.assertFalse(telegram._is_authorized(update))
        with patch.object(telegram.settings, "telegram_chat_id", "123"):
            self.assertTrue(telegram._is_authorized(update))
        with patch.object(telegram.settings, "telegram_chat_id", "456"):
            self.assertFalse(telegram._is_authorized(update))
        with patch.object(telegram.settings, "telegram_chat_id", "123"):
            self.assertFalse(telegram._is_authorized(SimpleNamespace(effective_chat=None)))

    def test_summary_commands_authorize_before_typing(self) -> None:
        bot = SimpleNamespace(send_chat_action=AsyncMock())
        context = SimpleNamespace(bot=bot)
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=999),
            message=SimpleNamespace(),
        )
        with patch.object(telegram.settings, "telegram_chat_id", "123"):
            asyncio.run(telegram.cmd_daily(update, context))
            asyncio.run(telegram.cmd_weekly(update, context))
        bot.send_chat_action.assert_not_awaited()




if __name__ == "__main__":
    unittest.main()
