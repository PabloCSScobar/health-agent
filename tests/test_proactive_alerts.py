from __future__ import annotations

import unittest

from health_agent.tools.proactive_alerts import TopicResult, _number, build_payload


class ProactiveAlertUnitTest(unittest.TestCase):
    def test_profile_number_requires_finite_positive_value(self) -> None:
        self.assertEqual(_number("120,5 g"), 120.5)
        for value in (None, "", "zero", "0", "-1", "nan", "inf"):
            with self.subTest(value=value):
                self.assertIsNone(_number(value))

    def test_payload_contains_only_triggered_topics_without_actions(self) -> None:
        payload = build_payload([
            TopicResult("no_training", "triggered", "reason", "• Brak treningu.", {}),
            TopicResult("low_protein", "clear", "ok"),
        ])
        self.assertEqual(payload["kind"], "proactive_alert")
        self.assertEqual(payload["topics"], ["no_training"])
        self.assertIn("Brak treningu", payload["text"])


if __name__ == "__main__":
    unittest.main()
