from __future__ import annotations

import datetime as dt
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from health_agent.tools import manual_batch, strength
from health_agent.tools.manual import ManualLogEntry

TODAY = dt.date(2026, 11, 12)
NOW = dt.datetime(2026, 11, 12, 18, 30, tzinfo=dt.timezone.utc)


def entry(kind: str, payload: dict) -> ManualLogEntry:
    return ManualLogEntry(kind=kind, payload=payload, text_original="test")


@patch.object(manual_batch, "local_today", lambda: TODAY)
class WeightBackdateTest(unittest.TestCase):
    def test_weight_without_date_uses_logged_at(self) -> None:
        self.assertEqual(manual_batch._measured_at(entry("weight", {"weight_kg": 82.0}), NOW), NOW)

    def test_weight_with_today_uses_logged_at(self) -> None:
        e = entry("weight", {"weight_kg": 82.0, "date": "2026-11-12"})
        self.assertEqual(manual_batch._measured_at(e, NOW), NOW)

    def test_weight_with_past_date_lands_on_that_local_day(self) -> None:
        e = entry("weight", {"weight_kg": 84.3, "date": "2026-06-15"})
        # 12:00 w Europe/Warsaw (CEST) = 10:00 UTC
        self.assertEqual(manual_batch._measured_at(e, NOW), dt.datetime(2026, 6, 15, 10, 0, tzinfo=dt.timezone.utc))

    def test_future_date_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "przyszłości"):
            manual_batch._validate([entry("weight", {"weight_kg": 82.0, "date": "2026-11-13"})])

    def test_bad_date_format_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            manual_batch._validate([entry("strength", {"cwiczenia": [], "date": "15.06.2026"})])


class StrengthBackdateTest(unittest.TestCase):
    def log(self, payload) -> SimpleNamespace:
        return SimpleNamespace(id=7, logged_at=NOW, payload_json=payload, text_original="test")

    def test_session_date_from_payload(self) -> None:
        s = strength._parse_log(self.log({"date": "2026-07-01", "cwiczenia": [{"nazwa": "dipy", "serie": 3, "powtorzenia": 12}]}))
        self.assertEqual(s["date"], "2026-07-01")

    def test_session_date_falls_back_to_local_logged_day(self) -> None:
        late = SimpleNamespace(id=8, logged_at=dt.datetime(2026, 11, 12, 23, 30, tzinfo=dt.timezone.utc),
                               payload_json={"cwiczenia": [{"nazwa": "dipy", "serie": 3, "powtorzenia": 12}]}, text_original="t")
        self.assertEqual(strength._parse_log(late)["date"], "2026-11-13")  # 00:30 w Warszawie


if __name__ == "__main__":
    unittest.main()
