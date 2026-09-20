from __future__ import annotations

import datetime as dt
import io
import re
import unittest
from types import SimpleNamespace

from fastapi import HTTPException
from PIL import Image
from starlette.requests import Request

from health_agent.api import dashboard
from health_agent.api.dashboard import _validate_mutation
from health_agent.channels.telegram import _photo_caption
from health_agent.tools.correlations import compute_correlations, spearman
from health_agent.tools.overview import METRIC_SPECS, summarize_frame, summarize_metric
from health_agent.tools.photos import _validated_image
from health_agent.tools.reminders import _scheduled_utc, _validate_rule


def _frame(start: dt.date, values: list[float | None], key: str) -> list[dict]:
    rows = []
    for index, value in enumerate(values):
        row = {spec.key: None for spec in METRIC_SPECS}
        row["date"] = start + dt.timedelta(days=index)
        row[key] = value
        rows.append(row)
    return rows


class OverviewTest(unittest.TestCase):
    def test_missing_days_do_not_become_zero(self) -> None:
        start = dt.date(2026, 9, 1)
        current = _frame(start + dt.timedelta(days=7), [None, 76.4, None, 76.0, None, None, 75.8], "weight_kg")
        previous = _frame(start, [77.0, None, 77.4, None, None, None, None], "weight_kg")
        spec = next(item for item in METRIC_SPECS if item.key == "weight_kg")
        result = summarize_metric(current, previous, spec)
        self.assertEqual(result["latest"], 75.8)
        self.assertEqual(result["latest_date"], "2026-09-14")
        self.assertEqual(result["days_with_data"], 3)
        self.assertEqual(result["previous_days_with_data"], 2)
        self.assertTrue(result["comparison_available"])
        self.assertEqual(result["days_total"], 7)
        self.assertAlmostEqual(result["average"], 76.1, places=1)
        self.assertAlmostEqual(result["previous_average"], 77.2, places=1)
        self.assertAlmostEqual(result["delta"], -1.1, places=1)
        self.assertEqual((result["min"], result["max"]), (75.8, 76.4))

    def test_empty_metric_reports_none_everywhere(self) -> None:
        start = dt.date(2026, 9, 1)
        rows = _frame(start, [None] * 7, "steps")
        results = {item["key"]: item for item in summarize_frame(rows, rows)}
        steps = results["steps"]
        self.assertIsNone(steps["latest"])
        self.assertIsNone(steps["average"])
        self.assertIsNone(steps["delta"])
        self.assertEqual(steps["days_with_data"], 0)
        self.assertEqual(steps["previous_days_with_data"], 0)
        self.assertFalse(steps["comparison_available"])
        self.assertEqual({item["kind"] for item in results.values()}, {"level", "total"})

    def test_integer_metrics_round_to_int(self) -> None:
        start = dt.date(2026, 9, 1)
        rows = _frame(start, [8000, 12345, None], "steps")
        result = next(item for item in summarize_frame(rows, []) if item["key"] == "steps")
        self.assertEqual(result["latest"], 12345)
        self.assertIsInstance(result["average"], int)
        self.assertIsNone(result["previous_average"])

    def test_uneven_samples_do_not_claim_a_comparison(self) -> None:
        start = dt.date(2026, 9, 1)
        current = _frame(start + dt.timedelta(days=7), [None, None, 80, None, None, None, None], "weight_kg")
        previous = _frame(start, [75, 75, 75, 75, 75, 75, 75], "weight_kg")
        spec = next(item for item in METRIC_SPECS if item.key == "weight_kg")
        result = summarize_metric(current, previous, spec)
        self.assertEqual(result["average"], 80.0)
        self.assertEqual(result["previous_average"], 75.0)
        self.assertIsNone(result["delta"])
        self.assertFalse(result["comparison_available"])


class DashboardAssetsTest(unittest.TestCase):
    def test_pages_reference_versioned_assets_without_inline_scripts(self) -> None:
        for page in (dashboard.LOGIN_HTML, dashboard.DASHBOARD_HTML):
            self.assertIn("/dash/static/dashboard.css?v=" + dashboard.ASSET_VERSION, page)
            self.assertNotIn("__ASSET_VERSION__", page)
            self.assertNotIn("<script>", page)
            self.assertNotIn("onclick=", page)
            self.assertNotIn("http://", page)
            self.assertNotIn("https://", page)
        self.assertIn("/dash/static/dashboard.js?v=", dashboard.DASHBOARD_HTML)
        self.assertIn("/dash/static/login.js?v=", dashboard.LOGIN_HTML)

    def test_static_assets_exist_and_unknown_names_are_rejected(self) -> None:
        for name in dashboard.STATIC_ASSETS:
            self.assertTrue((dashboard.STATIC_DIR / name).is_file(), name)
        with self.assertRaises(HTTPException) as context:
            dashboard.static_asset("dashboard.html")
        self.assertEqual(context.exception.status_code, 404)
        with self.assertRaises(HTTPException):
            dashboard.static_asset("../settings.py")
        response = dashboard.static_asset("dashboard.css")
        self.assertEqual(response.media_type, "text/css; charset=utf-8")
        self.assertIn("private", response.headers["cache-control"])

    def test_frontend_uses_no_external_resources(self) -> None:
        for name in dashboard.STATIC_ASSETS:
            content = (dashboard.STATIC_DIR / name).read_text(encoding="utf-8")
            # Przestrzeń nazw SVG jest identyfikatorem, nie żądaniem sieciowym.
            content = content.replace("http://www.w3.org/2000/svg", "")
            self.assertNotIn("https://", content, name)
            self.assertNotIn("http://", content, name)
            self.assertNotIn("@import", content, name)
            self.assertNotIn("innerHTML", content, name)
            self.assertIsNone(re.search(r"(?<![\w.])(alert|confirm|prompt)\(", content), name)
        script = (dashboard.STATIC_DIR / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn("timeZone: state.timezone", script)


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
