from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from health_agent.channels import telegram
from health_agent.settings import Settings


def _update(text: str | None = "Ile miałem wczoraj kroków?"):
    message = MagicMock()
    message.text = text
    message.reply_to_message = None
    message.reply_text = AsyncMock()
    message.document = MagicMock()
    return SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=123))


class TelegramOpenbotModeTest(unittest.IsolatedAsyncioTestCase):
    def test_default_chat_mode_is_agents(self) -> None:
        self.assertEqual(Settings(_env_file=None).telegram_chat_mode, "agents")

    async def test_free_text_replies_without_calling_agent(self) -> None:
        update = _update()
        context = SimpleNamespace(bot=MagicMock(send_chat_action=AsyncMock()))
        with (
            patch.object(telegram.settings, "telegram_chat_mode", "openbot"),
            patch.object(telegram.settings, "telegram_chat_id", "123"),
            patch("health_agent.agents.registry.ask_orchestrator_async_tracked", new=AsyncMock()) as ask,
            patch.object(telegram, "get_session") as get_session,
        ):
            await telegram.handle_message(update, context)
        ask.assert_not_called()
        get_session.assert_not_called()
        update.message.reply_text.assert_awaited_once_with(telegram.OPENBOT_MOVED_TEXT)
        self.assertIn("OpenBot", telegram.OPENBOT_MOVED_TEXT)

    async def test_document_import_replies_without_llm(self) -> None:
        update = _update()
        with (
            patch.object(telegram.settings, "telegram_chat_mode", "openbot"),
            patch.object(telegram.settings, "telegram_chat_id", "123"),
            patch("health_agent.agents.importer.import_document", new=AsyncMock()) as importer,
        ):
            await telegram.handle_document(update, SimpleNamespace())
        importer.assert_not_called()
        update.message.reply_text.assert_awaited_once_with(telegram.OPENBOT_MOVED_TEXT)

    async def test_summary_commands_reply_without_llm(self) -> None:
        for command in (telegram.cmd_daily, telegram.cmd_weekly):
            update = _update()
            context = SimpleNamespace(bot=MagicMock(send_chat_action=AsyncMock()))
            with (
                patch.object(telegram.settings, "telegram_chat_mode", "openbot"),
                patch.object(telegram.settings, "telegram_chat_id", "123"),
                patch("health_agent.agents.summaries.build_daily_summary", new=AsyncMock()) as daily,
                patch("health_agent.agents.summaries.build_weekly_summary", new=AsyncMock()) as weekly,
            ):
                await command(update, context)
            daily.assert_not_called()
            weekly.assert_not_called()
            update.message.reply_text.assert_awaited_once_with(telegram.OPENBOT_MOVED_TEXT)
            context.bot.send_chat_action.assert_not_called()

    async def test_agents_mode_still_calls_agent(self) -> None:
        update = _update("Cześć")
        context = SimpleNamespace(bot=MagicMock(send_chat_action=AsyncMock()))
        with (
            patch.object(telegram.settings, "telegram_chat_mode", "agents"),
            patch.object(telegram.settings, "telegram_chat_id", "123"),
            patch("health_agent.agents.registry.ask_orchestrator_async_tracked", new=AsyncMock(return_value=("ok", 1))) as ask,
            patch.object(telegram, "get_session"),
            patch.object(telegram, "_tracked_reply", new=AsyncMock()) as reply,
        ):
            await telegram.handle_message(update, context)
        ask.assert_awaited_once()
        reply.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
