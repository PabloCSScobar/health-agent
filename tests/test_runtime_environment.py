from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from health_agent.api import app as app_module
from health_agent.channels import telegram
from health_agent.settings import Settings


class RuntimeEnvironmentTest(unittest.IsolatedAsyncioTestCase):
    def test_settings_default_to_safe_development_mode(self) -> None:
        settings = Settings(_env_file=None)

        self.assertEqual(settings.app_env, "development")
        self.assertFalse(settings.scheduler_enabled)

    async def test_lifespan_does_not_build_disabled_scheduler(self) -> None:
        with (
            patch.object(app_module.settings, "app_env", "development"),
            patch.object(app_module.settings, "scheduler_enabled", False),
            patch.object(app_module, "build_scheduler") as build_scheduler,
        ):
            async with app_module.lifespan(app_module.app):
                pass

        build_scheduler.assert_not_called()

    async def test_lifespan_starts_and_stops_enabled_scheduler(self) -> None:
        scheduler = MagicMock()
        with (
            patch.object(app_module.settings, "app_env", "production"),
            patch.object(app_module.settings, "scheduler_enabled", True),
            patch.object(app_module, "build_scheduler", return_value=scheduler),
        ):
            async with app_module.lifespan(app_module.app):
                scheduler.start.assert_called_once_with()

        scheduler.shutdown.assert_called_once_with(wait=False)

    def test_status_identifies_environment_and_scheduler(self) -> None:
        with (
            patch.object(telegram.settings, "app_env", "development"),
            patch.object(telegram.settings, "scheduler_enabled", False),
        ):
            lines = telegram._runtime_status_lines()

        self.assertIn("- Środowisko: development", lines)
        self.assertIn("- Scheduler: wyłączony", lines)


if __name__ == "__main__":
    unittest.main()
