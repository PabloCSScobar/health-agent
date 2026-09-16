"""Bot Telegram - następca scripts/test_telegram_bot.py, podpięty pod
prawdziwego orchestratora zamiast echo.

Whitelist: tylko TELEGRAM_CHAT_ID z .env może rozmawiać z botem (patrz
plan, sekcja "Dane zdrowotne: ... bot ograniczony do jednego chat_id").
"""

from __future__ import annotations

import datetime as dt
import logging
import re

from sqlalchemy import select
from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from health_agent.db.models import AgentRun, Conversation
from health_agent.db.session import get_session
from health_agent.settings import settings

logger = logging.getLogger("health_agent.telegram")


def _to_telegram_markdown(text: str) -> str:
    """Model generuje standardowy Markdown (`**pogrubienie**`), ale Telegram
    (w obu trybach - legacy i V2) oczekuje POJEDYNCZEJ gwiazdki dla
    pogrubienia. Sprawdzone bezpośrednio na żywo: z `**x**` Telegram po
    prostu wycina gwiazdki bez pogrubienia (nie błąd, po cichu ignoruje),
    z `*x*` poprawnie tworzy encję "bold". Stąd ta konwersja."""
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)


async def _reply(message: Message, text: str) -> None:
    """Wysyła odpowiedź z Markdownem. Legacy Markdown, nie MarkdownV2 - dużo
    bardziej wyrozumiały dla nieuciekanionych znaków specjalnych, które LLM
    naturalnie generuje (kropki, nawiasy itp.). Jeśli model i tak wygeneruje
    niezbalansowany markup, Telegram odrzuci wiadomość - łapiemy to i
    wysyłamy zwykły tekst zamiast w ogóle nie odpowiadać."""
    try:
        await message.reply_text(_to_telegram_markdown(text), parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        logger.warning("Markdown się nie sparsował, wysyłam zwykły tekst")
        await message.reply_text(text)


def _is_authorized(update: Update) -> bool:
    if not settings.telegram_chat_id:
        # Brak ustawionej whitelisty - w praktyce nie powinno się zdarzyć
        # poza pierwszym uruchomieniem (patrz instrukcja w skrypcie testowym),
        # ale nie blokujemy na twardo, żeby dało się w ogóle poznać chat_id.
        return True
    return str(update.effective_chat.id) == str(settings.telegram_chat_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    chat_id = str(update.effective_chat.id)

    if not _is_authorized(update):
        logger.warning("Odrzucono wiadomość z nieautoryzowanego chat_id=%s", chat_id)
        await update.message.reply_text("Nieautoryzowany dostęp.")
        return

    text = update.message.text

    # Historia PRZED dopisaniem bieżącej wiadomości - orchestrator dostaje
    # ostatnie kilka wymian jako kontekst (patrz _with_history w registry.py -
    # bez tego każde pytanie leciało kompletnie bez kontekstu poprzednich).
    HISTORY_TURNS = 6
    with get_session() as session:
        recent = session.execute(
            select(Conversation.role, Conversation.content)
            .where(Conversation.chat_id == chat_id)
            .order_by(Conversation.created_at.desc())
            .limit(HISTORY_TURNS)
        ).all()
    history = [(role, content) for role, content in reversed(recent)]

    with get_session() as session:
        session.add(Conversation(chat_id=chat_id, role="user", content=text))

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    from health_agent.agents.registry import ask_orchestrator_async

    try:
        answer = await ask_orchestrator_async(text, history=history)
    except Exception:
        logger.exception("Błąd podczas odpowiadania na wiadomość")
        answer = "Coś poszło nie tak przy próbie odpowiedzi - spróbuj ponownie za chwilę."

    with get_session() as session:
        session.add(Conversation(chat_id=chat_id, role="assistant", content=answer, agent="orchestrator"))

    await _reply(update.message, answer)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    from health_agent.db.models import BodyComposition, NutritionDay, Workout

    with get_session() as session:
        last_workout = session.execute(select(Workout).order_by(Workout.started_at.desc()).limit(1)).scalar_one_or_none()
        last_body = session.execute(select(BodyComposition).order_by(BodyComposition.measured_at.desc()).limit(1)).scalar_one_or_none()
        last_nutrition = session.execute(select(NutritionDay).order_by(NutritionDay.date.desc()).limit(1)).scalar_one_or_none()

    lines = ["**Status synchronizacji:**"]
    lines.append(f"- Ostatni trening: {last_workout.started_at if last_workout else 'brak'}")
    lines.append(f"- Ostatnia waga: {last_body.measured_at if last_body else 'brak'}")
    lines.append(f"- Ostatni dzień odżywiania: {last_nutrition.date if last_nutrition else 'brak'}")
    await _reply(update.message, "\n".join(lines))


async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    with get_session() as session:
        runs = session.execute(select(AgentRun).where(AgentRun.created_at >= since)).scalars().all()

    total_cost = sum(r.cost_usd for r in runs if r.cost_usd) or 0.0
    total_calls = len(runs)
    await update.message.reply_text(f"Ostatnie 24h: {total_calls} wywołań agentów, koszt: ${total_cost:.4f}")


def build_bot():
    if not settings.telegram_bot_token:
        raise RuntimeError("Brak TELEGRAM_BOT_TOKEN w .env")

    app = ApplicationBuilder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app = build_bot()
    print("Bot Telegram działa (long polling). Ctrl+C żeby zatrzymać.")
    app.run_polling()


if __name__ == "__main__":
    main()
