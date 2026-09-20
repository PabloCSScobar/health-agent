"""Bot Telegram - następca scripts/test_telegram_bot.py, podpięty pod
prawdziwego orchestratora zamiast echo.

Whitelist: tylko TELEGRAM_CHAT_ID z .env może rozmawiać z botem (patrz
plan, sekcja "Dane zdrowotne: ... bot ograniczony do jednego chat_id").
"""

from __future__ import annotations

import datetime as dt
import asyncio
import logging
import re

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    MessageReactionHandler,
    filters,
)

from health_agent.db.models import AgentRun, Conversation, Feedback
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


TELEGRAM_MAX = 4000  # limit to 4096, zostawiamy margines na markup
IMPORT_MIN_CHARS = 800  # dłuższy wklejony tekst = prawdopodobnie notatka do importu
_pending_import: dict[str, str] = {}  # chat_id -> tekst czekający na "tak" (jeden użytkownik, proces bota)


def _chunks(text: str) -> list[str]:
    """Podziel tekst bez pustych kawałków, także gdy jeden akapit przekracza limit."""
    if len(text) <= TELEGRAM_MAX:
        return [text]
    out: list[str] = []
    buf = ""
    for para in text.split("\n"):
        while len(para) > TELEGRAM_MAX:
            if buf:
                out.append(buf)
                buf = ""
            out.append(para[:TELEGRAM_MAX])
            para = para[TELEGRAM_MAX:]
        candidate = f"{buf}\n{para}" if buf else para
        if len(candidate) > TELEGRAM_MAX:
            if buf:
                out.append(buf)
            buf = para
        else:
            buf = candidate
    if buf:
        out.append(buf)
    return out


async def _reply(message: Message, text: str, on_sent=None, reply_markup=None) -> list[Message]:
    """Wyślij odpowiedź w kawałkach i zwróć wszystkie wiadomości Telegrama."""
    sent_messages = []
    for index, chunk in enumerate(_chunks(text)):
        markup = reply_markup if index == 0 else None
        try:
            sent = await message.reply_text(
                _to_telegram_markdown(chunk),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=markup,
            )
        except BadRequest:
            logger.warning("Markdown się nie sparsował, wysyłam zwykły tekst")
            sent = await message.reply_text(chunk, reply_markup=markup)
        sent_messages.append(sent)
        if on_sent is not None:
            on_sent(sent_messages)
    return sent_messages


def _create_assistant_conversation(chat_id: str, content: str, agent: str, root_run_id: int | None = None) -> int:
    with get_session() as session:
        row = Conversation(
            chat_id=chat_id,
            role="assistant",
            content=content,
            agent=agent,
            root_run_id=root_run_id,
        )
        session.add(row)
        session.flush()
        return row.id


def _update_conversation_messages(conversation_id: int, messages: list[Message] | list[int]) -> None:
    message_ids = [item if isinstance(item, int) else item.message_id for item in messages]
    with get_session() as session:
        row = session.get(Conversation, conversation_id)
        if row is None:
            raise LookupError(f"Brak rozmowy id={conversation_id}")
        row.telegram_message_id = message_ids[0] if message_ids else None
        row.telegram_message_ids = message_ids


def _delete_conversation_if_unsent(conversation_id: int) -> None:
    with get_session() as session:
        row = session.get(Conversation, conversation_id)
        if row is not None and not row.telegram_message_ids:
            session.delete(row)


async def _tracked_reply(
    message: Message, text: str, agent: str, root_run_id: int | None = None,
    reply_markup=None,
) -> list[Message]:
    conversation_id = _create_assistant_conversation(
        str(message.chat_id), text, agent, root_run_id
    )
    try:
        return await _reply(
            message,
            text,
            on_sent=lambda sent: _update_conversation_messages(conversation_id, sent),
            reply_markup=reply_markup,
        )
    except Exception:
        # Gdy choć jeden kawałek dotarł, zachowujemy jego ID, aby nadal dało
        # się wystawić feedback. Wiersz bez żadnej wysłanej wiadomości usuwamy.
        _delete_conversation_if_unsent(conversation_id)
        raise


def _is_authorized(update: Update) -> bool:
    if not settings.telegram_chat_id:
        logger.error("Brak TELEGRAM_CHAT_ID — odrzucam aktualizację Telegrama")
        return False
    if update.effective_chat is None:
        return False
    return str(update.effective_chat.id) == str(settings.telegram_chat_id)


def _find_feedback_target(chat_id: str, message_id: int) -> Conversation | None:
    with get_session() as session:
        direct = session.execute(
            select(Conversation).where(
                Conversation.chat_id == chat_id,
                Conversation.telegram_message_id == message_id,
                Conversation.role == "assistant",
            )
        ).scalar_one_or_none()
        if direct is not None:
            return direct
        candidates = session.execute(
            select(Conversation).where(
                Conversation.chat_id == chat_id,
                Conversation.role == "assistant",
                Conversation.telegram_message_ids.isnot(None),
            )
        ).scalars().all()
        return next((row for row in candidates if message_id in (row.telegram_message_ids or [])), None)

def _store_feedback(conversation_id: int, agent_run_id: int | None, rating: int, comment: str | None = None) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    values = {
        "conversation_id": conversation_id,
        "agent_run_id": agent_run_id,
        "rating": rating,
        "comment": comment,
        "created_at": now,
        "updated_at": now,
    }
    updates = {"agent_run_id": agent_run_id, "rating": rating, "updated_at": now}
    if comment is not None:
        updates["comment"] = comment
    stmt = pg_insert(Feedback).values(**values).on_conflict_do_update(
        constraint="uq_feedback_conversation",
        set_=updates,
    )
    with get_session() as session:
        session.execute(stmt)

async def handle_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reaction = update.message_reaction
    if reaction is None or not _is_authorized(update):
        return
    chat_id = str(reaction.chat.id)
    target = _find_feedback_target(chat_id, reaction.message_id)
    if target is None:
        logger.info("Brak rozmowy dla reakcji chat_id=%s message_id=%s", chat_id, reaction.message_id)
        return

    emojis = {getattr(item, "emoji", None) for item in reaction.new_reaction}
    rating = -1 if "👎" in emojis else 1 if "👍" in emojis else None
    with get_session() as session:
        existing = session.execute(
            select(Feedback).where(Feedback.conversation_id == target.id)
        ).scalar_one_or_none()
        if rating is None:
            if existing is not None:
                session.delete(existing)
            return
    _store_feedback(target.id, target.root_run_id, rating)


def _feedback_comment(text: str) -> tuple[int | None, str] | None:
    match = re.match(r"^\s*(👍|👎|feedback:)\s*(.*)$", text, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    marker, comment = match.groups()
    rating = 1 if marker == "👍" else -1 if marker == "👎" else None
    return rating, comment.strip()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    chat_id = str(update.effective_chat.id)

    if not _is_authorized(update):
        logger.warning("Odrzucono wiadomość z nieautoryzowanego chat_id=%s", chat_id)
        await update.message.reply_text("Nieautoryzowany dostęp.")
        return

    text = update.message.text

    if update.message.reply_to_message and (parsed := _feedback_comment(text)) is not None:
        target = _find_feedback_target(chat_id, update.message.reply_to_message.message_id)
        if target is None:
            await update.message.reply_text("Nie znalazłem ocenianej odpowiedzi.")
            return
        rating, comment = parsed
        if rating is None:
            with get_session() as session:
                existing = session.execute(select(Feedback).where(Feedback.conversation_id == target.id)).scalar_one_or_none()
                if existing is None:
                    await update.message.reply_text("Najpierw oceń odpowiedź reakcją 👍 lub 👎.")
                    return
                rating = existing.rating
        _store_feedback(target.id, target.root_run_id, rating, comment or None)
        await update.message.reply_text("Feedback zapisany. Dzięki!")
        return

    # Import wiedzy z wklejonego tekstu: długa wiadomość może być notatką
    # ALBO długim pytaniem - pytamy raz, nie zgadujemy. Odpowiedź "tak"
    # importuje; cokolwiek innego = normalna rozmowa.
    if chat_id in _pending_import:
        pending = _pending_import.pop(chat_id)
        if text.strip().lower() in ("tak", "tak.", "importuj", "zaimportuj", "yes"):
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
            from health_agent.agents.importer import import_document

            report = await import_document(pending, title=f"notatka z Telegrama {dt.date.today().isoformat()}", source="telegram_text")
            await _tracked_reply(update.message, report, "importer")
            return
    elif len(text) >= IMPORT_MIN_CHARS and "?" not in text[-200:]:
        _pending_import[chat_id] = text
        await update.message.reply_text(
            f"To wygląda na notatkę ({len(text)} znaków). Zaimportować ją do wiedzy agentów? "
            "Odpowiedz \"tak\" - albo zadaj pytanie normalnie, a wtedy potraktuję to jako zwykłą wiadomość."
        )
        return

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

    from health_agent.agents.registry import ask_orchestrator_async_tracked

    root_run_id = None
    try:
        answer, root_run_id = await ask_orchestrator_async_tracked(text, history=history)
    except Exception:
        logger.exception("Błąd podczas odpowiadania na wiadomość")
        answer = "Coś poszło nie tak przy próbie odpowiedzi - spróbuj ponownie za chwilę."

    draft_match = re.search(r"\[REMINDER_DRAFT:(\d+)\]", answer)
    reply_markup = None
    if draft_match:
        rule_id = int(draft_match.group(1))
        answer = re.sub(r"\s*\[REMINDER_DRAFT:\d+\]\s*", "", answer).strip()
        reply_markup = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "Aktywuj", callback_data=f"rule:{rule_id}:activate"
                ),
                InlineKeyboardButton(
                    "Anuluj", callback_data=f"rule:{rule_id}:cancel"
                ),
            ]]
        )
    await _tracked_reply(
        update.message, answer, "orchestrator", root_run_id, reply_markup=reply_markup
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Plik .txt/.md wysłany na czat = import notatki (omija limit 4096
    znaków wiadomości). Tytuł = nazwa pliku, data = z treści (importer) albo
    z podpisu pod plikiem w formacie YYYY-MM-DD."""
    if not _is_authorized(update) or not update.message or not update.message.document:
        return
    doc = update.message.document
    name = doc.file_name or "notatka"
    if not (name.lower().endswith((".txt", ".md")) or (doc.mime_type or "").startswith("text/")):
        await update.message.reply_text("Importuję tylko pliki tekstowe (.txt, .md).")
        return
    if (doc.file_size or 0) > 200_000:
        await update.message.reply_text("Plik za duży (limit 200 KB) - podziel na części.")
        return
    tg_file = await doc.get_file()
    raw = await tg_file.download_as_bytearray()
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        text = bytes(raw).decode("cp1250", errors="replace")
    doc_date = None
    caption = (update.message.caption or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", caption):
        doc_date = dt.date.fromisoformat(caption)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    from health_agent.agents.importer import import_document

    report = await import_document(text, title=name.rsplit(".", 1)[0], source="telegram_file", doc_date=doc_date)
    chat_id = str(update.effective_chat.id)
    with get_session() as session:
        session.add(Conversation(chat_id=chat_id, role="user", content=f"[plik: {name}]"))
    await _tracked_reply(update.message, report, "importer")


def _runtime_status_lines() -> list[str]:
    return [
        "**Status aplikacji:**",
        f"- Środowisko: {settings.app_env}",
        f"- Scheduler: {'włączony' if settings.scheduler_enabled else 'wyłączony'}",
        "",
        "**Status synchronizacji:**",
    ]


def _dashboard_message() -> str:
    mode = settings.dashboard_access_mode
    if mode == "disabled":
        return "**Dashboard:** wyłączony."

    url = (settings.dashboard_url or "").strip()
    if mode == "local":
        lines = [
            "**Dashboard:** dostęp lokalny.",
            "Uruchom tunel SSH: `ssh -N -L 8000:127.0.0.1:8000 deploy@ADRES_VPS`",
            "Następnie otwórz http://127.0.0.1:8000/dash.",
        ]
    elif not url:
        return (
            "**Dashboard:** tryb dostępu jest ustawiony, ale brakuje "
            "`DASHBOARD_URL` w konfiguracji."
        )
    elif mode == "tailscale":
        lines = [
            "**Dashboard:** " + url,
            "Dostęp jest prywatny przez Tailscale. Urządzenie musi być "
            "połączone z Twoim tailnetem; adres nie jest otwarty na świat.",
        ]
    else:
        lines = [
            "**Dashboard:** " + url,
            "Dostęp jest publiczny przez HTTPS — Tailscale nie jest wymagany. "
            "Nadal obowiązuje logowanie do dashboardu.",
        ]

    if settings.dashboard_allowed_ips.strip():
        lines.append("Dodatkowo obowiązuje allowlista adresów IP.")
    return "\n".join(lines)


async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update) or not update.message:
        return
    await _tracked_reply(update.message, _dashboard_message(), "dashboard")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    from health_agent.db.models import BodyComposition, NutritionDay, Workout

    with get_session() as session:
        last_workout = session.execute(select(Workout).order_by(Workout.started_at.desc()).limit(1)).scalar_one_or_none()
        last_body = session.execute(select(BodyComposition).order_by(BodyComposition.measured_at.desc()).limit(1)).scalar_one_or_none()
        last_nutrition = session.execute(select(NutritionDay).order_by(NutritionDay.date.desc()).limit(1)).scalar_one_or_none()

    lines = _runtime_status_lines()
    lines.append(f"- Ostatni trening: {last_workout.started_at if last_workout else 'brak'}")
    lines.append(f"- Ostatnia waga: {last_body.measured_at if last_body else 'brak'}")
    lines.append(f"- Ostatni dzień odżywiania: {last_nutrition.date if last_nutrition else 'brak'}")
    await _tracked_reply(update.message, "\n".join(lines), "status")


async def cmd_profil(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Podgląd profilu + wywiad o brakujące fakty (bez LLM). Odpowiedź
    użytkownika na wywiad idzie zwykłą wiadomością - orchestrator ją parsuje
    (ZASADA 3), bo wywiad ląduje w historii rozmowy jako tura asystenta."""
    if not _is_authorized(update):
        return
    from health_agent.tools.profile import PROFILE_KEYS, get_user_profile, onboarding_message

    facts = get_user_profile()
    lines = ["**Profil:**"] + ([f"- {k}: {v}" for k, v in sorted(facts.items())] or ["- (pusty)"])
    lines.append("\nZmiana: napisz zwykłą wiadomością, np. \"cel biegowy: półmaraton w marcu\" albo \"kontuzje: brak\".")
    lines.append("Klucze: " + ", ".join(PROFILE_KEYS))
    text = "\n".join(lines)
    onboarding = onboarding_message()
    if onboarding:
        text += "\n\n" + onboarding
    await _tracked_reply(update.message, text, "profil")


async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update):
        return
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    with get_session() as session:
        runs = session.execute(select(AgentRun).where(AgentRun.created_at >= since)).scalars().all()

    total_cost = sum(r.cost_usd for r in runs if r.cost_usd) or 0.0
    total_calls = len(runs)
    await _tracked_reply(
        update.message,
        f"Ostatnie 24h: {total_calls} wywołań agentów, koszt: ${total_cost:.4f}",
        "cost",
    )



async def _cmd_summary(update: Update, period: str) -> None:
    if not _is_authorized(update) or not update.message:
        return
    from zoneinfo import ZoneInfo

    from health_agent.agents.summaries import build_daily_summary, build_weekly_summary

    today = dt.datetime.now(ZoneInfo(settings.summary_timezone)).date()
    result = await (build_daily_summary(today) if period == "daily" else build_weekly_summary(today))
    await _tracked_reply(update.message, result.text, period, result.root_run_id)


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update) or not update.message:
        return
    if update.effective_chat:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    await _cmd_summary(update, "daily")


async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update) or not update.message:
        return
    if update.effective_chat:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    await _cmd_summary(update, "weekly")


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update) or not update.message:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    from health_agent.ingest.sync import sync_intervals

    try:
        result = await asyncio.to_thread(sync_intervals)
        if result.status == "busy":
            text = "Synchronizacja Intervals.icu już trwa. Spróbuj ponownie za chwilę."
        else:
            text = (
                f"✅ Intervals.icu zsynchronizowane za {result.oldest}–{result.newest}: "
                f"{result.workouts} treningów i {result.wellness_days} dni wellness. "
                "Żywienie jest odświeżane osobno przez Health Connect."
            )
            if result.truncated:
                text += (
                    " Zakres został ograniczony do 60 dni; starszą lukę trzeba "
                    "uzupełnić ręcznie przez CLI."
                )
    except Exception:
        logger.exception("Ręczna synchronizacja Intervals.icu nieudana")
        text = "Nie udało się zsynchronizować Intervals.icu. Spróbuj ponownie za chwilę."
    await _tracked_reply(update.message, text, "sync")


_PHOTO_VIEW_ALIASES = {
    "przód": "front",
    "przod": "front",
    "front": "front",
    "bok": "side",
    "side": "side",
    "tył": "back",
    "tyl": "back",
    "back": "back",
    "inne": "other",
    "other": "other",
}


def _photo_caption(caption: str | None) -> tuple[str, dt.date, str | None]:
    from health_agent.time_utils import local_today

    parts = (caption or "").strip().split()
    view = _PHOTO_VIEW_ALIASES.get(parts[0].lower(), "other") if parts else "other"
    if parts and parts[0].lower() in _PHOTO_VIEW_ALIASES:
        parts.pop(0)
    day = local_today()
    if parts:
        try:
            day = dt.date.fromisoformat(parts[0])
            parts.pop(0)
        except ValueError:
            pass
    return view, day, " ".join(parts) or None


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update) or not update.message or not update.message.photo:
        return
    photo = update.message.photo[-1]
    if (photo.file_size or 0) > settings.progress_photo_max_bytes:
        await update.message.reply_text("Zdjęcie jest za duże (limit 15 MB).")
        return
    tg_file = await photo.get_file()
    raw = bytes(await tg_file.download_as_bytearray())
    view, captured_date, note = _photo_caption(update.message.caption)
    from health_agent.tools.photos import save_progress_photo

    try:
        result = await asyncio.to_thread(
            save_progress_photo,
            raw,
            captured_date=captured_date,
            view=view,
            note=note,
            source="telegram",
        )
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    status = "już było w archiwum" if result["deduplicated"] else "zapisane"
    await update.message.reply_text(
        f"📷 Zdjęcie {status}: {result['captured_date']}, {result['view']}."
    )


async def cmd_foto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update) or not update.message:
        return
    requested = context.args[0].lower() if context.args else None
    view = _PHOTO_VIEW_ALIASES.get(requested) if requested else None
    if requested and view is None:
        await update.message.reply_text("Widok: przód, bok, tył albo inne.")
        return
    from health_agent.tools.photos import list_progress_photos, progress_photo_path

    photos = await asyncio.to_thread(list_progress_photos, view, 6)
    if not photos:
        await update.message.reply_text("Brak zdjęć w tym widoku.")
        return
    for item in reversed(photos):
        found = progress_photo_path(item["id"])
        if found is None:
            continue
        path, _ = found
        caption = (
            f"{item['captured_date']} · {item['view']} · "
            f"{item['weight_kg'] if item['weight_kg'] is not None else '—'} kg · "
            f"{item['fat_pct'] if item['fat_pct'] is not None else '—'}% "
            f"{item['note'] or ''}"
        ).strip()
        with path.open("rb") as image:
            await update.message.reply_photo(photo=image, caption=caption)


async def cmd_suple(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update) or not update.message:
        return
    from health_agent.tools.reminders import list_supplements

    rows = await asyncio.to_thread(list_supplements)
    lines = ["**Suplementy:**"]
    lines.extend(
        f"- #{row['id']} {row['name']}"
        + (f" — {row['dose']}" if row["dose"] else "")
        + (f" (ostatnio: {row['last_status']})" if row["last_status"] else "")
        for row in rows
    )
    if not rows:
        lines.append("- brak")
    await _tracked_reply(update.message, "\n".join(lines), "supplements")


async def cmd_przypomnienia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update) or not update.message:
        return
    from health_agent.tools.reminders import list_reminder_rules

    rows = await asyncio.to_thread(list_reminder_rules)
    lines = ["**Przypomnienia:**"]
    lines.extend(
        f"- #{row['id']} [{row['status']}] {row['local_time']} — {row['title']}"
        for row in rows
    )
    if not rows:
        lines.append("- brak")
    await _tracked_reply(update.message, "\n".join(lines), "reminders")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not _is_authorized(update):
        return
    await query.answer()
    data = query.data or ""
    try:
        namespace, raw_id, action = data.split(":", 2)
        item_id = int(raw_id)
        if namespace == "rule":
            from health_agent.tools.reminders import (
                activate_reminder_rule,
                update_reminder_rule,
            )

            if action == "activate":
                await asyncio.to_thread(activate_reminder_rule, item_id)
                text = "Przypomnienie aktywowane."
            elif action == "cancel":
                await asyncio.to_thread(update_reminder_rule, item_id, status="paused")
                text = "Szkic anulowany."
            else:
                raise ValueError("Nieznana akcja")
        elif namespace == "reminder":
            from health_agent.tools.reminders import complete_occurrence

            text = await asyncio.to_thread(complete_occurrence, item_id, action)
        else:
            raise ValueError("Nieznany callback")
    except (ValueError, LookupError):
        logger.exception("Nieprawidłowy callback Telegrama: %s", data)
        text = "Nie udało się zastosować tej akcji."
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(text)


def build_bot():
    if not settings.telegram_bot_token:
        raise RuntimeError("Brak TELEGRAM_BOT_TOKEN w .env")

    app = ApplicationBuilder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("profil", cmd_profil))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("weekly", cmd_weekly))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("foto", cmd_foto))
    app.add_handler(CommandHandler("suple", cmd_suple))
    app.add_handler(CommandHandler("przypomnienia", cmd_przypomnienia))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^(?:rule|reminder):"))
    app.add_handler(MessageReactionHandler(handle_reaction))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    app = build_bot()
    print(f"Bot Telegram działa (środowisko={settings.app_env}, long polling). Ctrl+C żeby zatrzymać.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
