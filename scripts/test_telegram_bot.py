"""
Smoke test: bot Telegram odpowiadający echo na wiadomości + wypisujący
chat_id (potrzebny później do whitelisty w prawdziwej apce).

Jak zdobyć token:
    1. W Telegramie napisz do @BotFather -> /newbot -> podaj nazwę.
    2. Skopiuj token (wygląda jak 123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx)
       do .env jako TELEGRAM_BOT_TOKEN.

Użycie:
    uv run scripts/test_telegram_bot.py
Potem napisz cokolwiek do swojego bota w Telegramie - powinien odpowiedzieć
echem i wypisać w konsoli Twój chat_id.

Zatrzymanie: Ctrl+C.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

load_dotenv()


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    text = update.message.text if update.message else None
    print(f"[wiadomość] chat_id={chat_id} tekst={text!r}")
    if update.message:
        await update.message.reply_text(
            f"Echo: {text}\n\n(Twój chat_id to {chat_id} - zapisz go do .env jako TELEGRAM_CHAT_ID)"
        )


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print(
            "⚠️  Brak TELEGRAM_BOT_TOKEN w .env - załóż bota u @BotFather "
            "(patrz instrukcja w docstringu tego skryptu)."
        )
        return

    app = ApplicationBuilder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    print("Bot działa (long polling). Napisz coś do niego w Telegramie. Ctrl+C żeby zatrzymać.")
    app.run_polling()


if __name__ == "__main__":
    main()
