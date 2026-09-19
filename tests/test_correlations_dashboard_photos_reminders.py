from __future__ import annotations

import datetime as dt
import io
import unittest
from types import SimpleNamespace

from fastapi import HTTPException
from PIL import Image
from starlette.requests import Request

from health_agent.api.dashboard import _validate_mutation
from health_agent.channels.telegram import _photo_caption
from health_agent.tools.correlations import compute_correlations, spearman
from health_agent.tools.photos import _validated_image
from health_agent.tools.reminders import _scheduled_utc, _validate_rule


class CorrelationsTest(unittest.TestCase):
    def test_spearman_handles_ties_and_constant_series(self) -> None:
        self.assertAlmostEqual(spearman([1, 1, 2, 3], [10, 10, 20, 30]), 1.0)
        self.assertIsNone(spearman([1, 1, 1], [1, 2, 3]))

    def test_only_complete_pairs_count_and_threshold_is_explicit(self) -> None:
        start = dt.date(2026, 1, 1)
        rows = []
        for index in range(25):
            rows.append(
                {
                    "date": start + dt.timedelta(days=index),
                    "sleep_h": 5 + index / 10,
                    "sleep_score": None,
                    "hrv": 30 + index,
                    "wellbeing": None,
                    "steps": None,
                    "run_load": None,
                    "run_efficiency": None,
                    "run_start_hour": None,
                }
            )
        results = {item["pair_key"]: item for item in compute_correlations(rows)}
        self.assertEqual(results["sleep_hrv"]["n"], 25)
        self.assertEqual(results["sleep_hrv"]["status"], "qualifying")
        self.assertEqual(results["sleep_hrv"]["rho"], 1.0)
        self.assertEqual(results["sleep_wellbeing"]["status"], "insufficient_data")


class PhotoTest(unittest.TestCase):
    def test_validates_actual_image_not_declared_mime(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (8, 6), color="green").save(buffer, format="PNG")
        content_type, extension, width, height = _validated_image(buffer.getvalue())
        self.assertEqual((content_type, extension, width, height), ("image/png", ".png", 8, 6))
        with self.assertRaises(ValueError):
            _validated_image(b"not an image")

    def test_caption_parses_view_date_and_note(self) -> None:
        self.assertEqual(
            _photo_caption("przód 2026-09-18 po treningu"),
            ("front", dt.date(2026, 9, 18), "po treningu"),
        )


class ReminderAndAuthTest(unittest.TestCase):
    def test_rule_validation_requires_threshold(self) -> None:
        _validate_rule("text", "20:00", None, None)
        with self.assertRaises(ValueError):
            _validate_rule("text", "20:00", "steps_below", None)
        with self.assertRaises(ValueError):
            _validate_rule("text", "25:00", None, None)

    def test_nonexistent_dst_time_moves_to_next_valid_wall_time(self) -> None:
        rule = SimpleNamespace(local_time="02:30", timezone="Europe/Warsaw")
        scheduled = _scheduled_utc(rule, dt.date(2026, 3, 29))
        local = scheduled.astimezone(dt.timezone(dt.timedelta(hours=2)))
        self.assertEqual((local.hour, local.minute), (3, 30))

    def test_mutation_requires_csrf_and_same_origin(self) -> None:
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/dash/api/x",
                "headers": [
                    (b"host", b"health.example"),
                    (b"origin", b"https://health.example"),
                    (b"x-csrf-token", b"secret"),
                ],
            }
        )
        _validate_mutation(request, SimpleNamespace(csrf_token="secret"))
        with self.assertRaises(HTTPException):
            _validate_mutation(request, SimpleNamespace(csrf_token="wrong"))


if __name__ == "__main__":
    unittest.main()
