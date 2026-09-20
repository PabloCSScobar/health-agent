from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.requests import Request

from health_agent.api import app as app_module
from health_agent.channels import telegram
from health_agent.settings import Settings


class RuntimeEnvironmentTest(unittest.IsolatedAsyncioTestCase):
    def test_settings_default_to_safe_development_mode(self) -> None:
        settings = Settings(_env_file=None)

        self.assertEqual(settings.app_env, "development")
        self.assertFalse(settings.scheduler_enabled)
        self.assertEqual(settings.dashboard_access_mode, "local")

    def test_settings_reject_invalid_dashboard_network(self) -> None:
        with self.assertRaises(ValueError):
            Settings(_env_file=None, dashboard_allowed_ips="not-an-ip")

    async def test_lifespan_does_not_build_disabled_scheduler(self) -> None:
        with (
            patch.object(app_module.settings, "app_env", "development"),
            patch.object(app_module.settings, "scheduler_enabled", False),
            patch.object(app_module, "build_scheduler") as build_scheduler,
            patch(
                "health_agent.tools.photos.reconcile_progress_photos",
                return_value={"fixed": 0, "deleted_staging_rows": 0},
            ),
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
            patch(
                "health_agent.tools.photos.reconcile_progress_photos",
                return_value={"fixed": 0, "deleted_staging_rows": 0},
            ),
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

    def test_dashboard_message_describes_tailscale_requirement(self) -> None:
        with (
            patch.object(telegram.settings, "dashboard_access_mode", "tailscale"),
            patch.object(
                telegram.settings,
                "dashboard_url",
                "https://health-agent-vps.example.ts.net/dash",
            ),
            patch.object(telegram.settings, "dashboard_allowed_ips", ""),
        ):
            message = telegram._dashboard_message()

        self.assertIn("https://health-agent-vps.example.ts.net/dash", message)
        self.assertIn("musi być połączone", message)
        self.assertIn("nie jest otwarty na świat", message)

    def test_dashboard_message_describes_public_and_allowlist(self) -> None:
        with (
            patch.object(telegram.settings, "dashboard_access_mode", "public"),
            patch.object(telegram.settings, "dashboard_url", "https://example.test/dash"),
            patch.object(telegram.settings, "dashboard_allowed_ips", "192.0.2.4/32"),
        ):
            message = telegram._dashboard_message()

        self.assertIn("Tailscale nie jest wymagany", message)
        self.assertIn("allowlista", message)

    async def test_dashboard_command_is_authorized(self) -> None:
        message = SimpleNamespace()
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=123),
            message=message,
        )
        with (
            patch.object(telegram.settings, "telegram_chat_id", "123"),
            patch.object(telegram.settings, "dashboard_access_mode", "disabled"),
            patch.object(telegram, "_tracked_reply", AsyncMock()) as reply,
        ):
            await telegram.cmd_dashboard(update, SimpleNamespace())

        reply.assert_awaited_once_with(message, "**Dashboard:** wyłączony.", "dashboard")

    async def test_dashboard_command_does_not_disclose_url_to_other_chat(self) -> None:
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=999),
            message=SimpleNamespace(),
        )
        with (
            patch.object(telegram.settings, "telegram_chat_id", "123"),
            patch.object(telegram, "_tracked_reply", AsyncMock()) as reply,
        ):
            await telegram.cmd_dashboard(update, SimpleNamespace())

        reply.assert_not_awaited()

    def test_dashboard_ip_uses_forwarded_only_from_trusted_proxy(self) -> None:
        from health_agent.api import dashboard

        def request(peer: str, forwarded: bytes = b"100.64.0.10") -> Request:
            return Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/dash",
                    "headers": [(b"x-forwarded-for", forwarded)],
                    "client": (peer, 12345),
                    "server": ("testserver", 80),
                    "scheme": "http",
                }
            )

        with patch.object(dashboard.settings, "dashboard_trusted_proxies", "127.0.0.1/32"):
            self.assertEqual(str(dashboard._request_ip(request("127.0.0.1"))), "100.64.0.10")
            self.assertEqual(str(dashboard._request_ip(request("192.0.2.10"))), "192.0.2.10")
            self.assertEqual(
                str(dashboard._request_ip(request("127.0.0.1", b"203.0.113.9, 100.64.0.10"))),
                "100.64.0.10",
            )

            duplicated = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/dash",
                    "headers": [
                        (b"x-forwarded-for", b"198.51.100.7"),
                        (b"x-forwarded-for", b"203.0.113.8"),
                    ],
                    "client": ("127.0.0.1", 12345),
                    "server": ("testserver", 80),
                    "scheme": "http",
                }
            )
            self.assertEqual(str(dashboard._request_ip(duplicated)), "203.0.113.8")

    def test_dashboard_allowlist_rejects_ip_outside_network(self) -> None:
        from fastapi import HTTPException
        from health_agent.api import dashboard

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/dash",
                "headers": [],
                "client": ("192.0.2.20", 12345),
                "server": ("testserver", 80),
                "scheme": "http",
            }
        )
        with (
            patch.object(dashboard.settings, "dashboard_access_mode", "public"),
            patch.object(dashboard.settings, "dashboard_allowed_ips", "198.51.100.0/24"),
            self.assertRaises(HTTPException) as caught,
        ):
            dashboard._enforce_dashboard_access(request)

        self.assertEqual(caught.exception.status_code, 403)

    def test_dashboard_allowlist_accepts_ip_inside_network(self) -> None:
        from health_agent.api import dashboard

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/dash",
                "headers": [],
                "client": ("198.51.100.25", 12345),
                "server": ("testserver", 80),
                "scheme": "http",
            }
        )
        with (
            patch.object(dashboard.settings, "dashboard_access_mode", "public"),
            patch.object(dashboard.settings, "dashboard_allowed_ips", "198.51.100.0/24"),
        ):
            dashboard._enforce_dashboard_access(request)


if __name__ == "__main__":
    unittest.main()
