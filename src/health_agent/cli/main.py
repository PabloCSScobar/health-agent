"""CLI do testowania ingestorów bez telefonu/prawdziwych źródeł.

Użycie:
    uv run health-agent ingest healthconnect --file dump.json
    uv run health-agent ingest intervals --since 2026-08-01 [--until 2026-09-16]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path


def cmd_ingest_healthconnect(args: argparse.Namespace) -> None:
    from health_agent.db.session import get_session
    from health_agent.ingest.healthconnect import ingest_payload

    with open(args.file, encoding="utf-8") as f:
        payload = json.load(f)

    with get_session() as session:
        summary = ingest_payload(session, payload)

    print(json.dumps(summary, indent=2, ensure_ascii=False))


def cmd_chat(args: argparse.Namespace) -> None:
    from health_agent.agents.registry import ask_orchestrator

    answer = ask_orchestrator(args.question)
    print(answer)


def cmd_import(args: argparse.Namespace) -> None:
    import asyncio

    from health_agent.agents.importer import import_document

    with open(args.file, encoding="utf-8") as f:
        text = f.read()
    doc_date = dt.date.fromisoformat(args.date) if args.date else None
    title = args.title or Path(args.file).stem
    print(asyncio.run(import_document(text, title=title, source="cli", doc_date=doc_date)))


def cmd_undo_import(args: argparse.Namespace) -> None:
    from health_agent.agents.importer import undo_import

    print(undo_import(args.document_id))


def cmd_knowledge(args: argparse.Namespace) -> None:
    from health_agent.tools.knowledge import get_knowledge, list_documents

    print("Dokumenty:")
    for d in list_documents():
        print(f"  #{d['id']} {d['title']} ({d['doc_date'] or '-'}, {d['chars']} zn., {d['domains']}) - {d['summary']}")
    print("\nWiedza (aktywna):")
    for k in get_knowledge(domain=args.domain, include_inactive=args.all, limit=200):
        flag = "" if k["active"] else " [nieaktywny]"
        print(f"  #{k['id']} [{k['domain']}/{k['kind']}{', ' + k['event_date'] if k['event_date'] else ''}] {k['content']}{flag}")


def cmd_ingest_intervals(args: argparse.Namespace) -> None:
    from health_agent.ingest.sync import sync_intervals
    from health_agent.time_utils import local_today

    oldest = dt.date.fromisoformat(args.since)
    newest = dt.date.fromisoformat(args.until) if args.until else local_today()
    result = sync_intervals(oldest, newest)
    print(json.dumps(result.__dict__, indent=2, ensure_ascii=False, default=str))


def cmd_correlations(args: argparse.Namespace) -> None:
    from health_agent.tools.correlations import get_correlations, publish_correlations

    results = publish_correlations() if args.publish else get_correlations(args.days)
    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))


def cmd_hash_password(args: argparse.Namespace) -> None:
    import getpass

    from argon2 import PasswordHasher

    password = getpass.getpass("Hasło dashboardu: ")
    confirmation = getpass.getpass("Powtórz hasło: ")
    if password != confirmation:
        raise SystemExit("Hasła się różnią")
    if len(password) < 12:
        raise SystemExit("Hasło musi mieć co najmniej 12 znaków")
    print(PasswordHasher().hash(password))



def cmd_summary(args: argparse.Namespace) -> None:
    import asyncio

    from health_agent.agents.summaries import build_daily_summary, build_weekly_summary

    if args.command == "daily":
        day = dt.date.fromisoformat(args.date) if args.date else None
        result = asyncio.run(build_daily_summary(day))
    else:
        result = asyncio.run(build_weekly_summary())
    print(result.text)


def _agent_run_tree(session, root_run_id: int | None) -> dict:
    from sqlalchemy import select

    from health_agent.db.models import AgentRun

    if root_run_id is None:
        return {"agents": [], "models": [], "tools": [], "cost_usd": None}
    pending = [root_run_id]
    rows = []
    while pending:
        batch = session.execute(select(AgentRun).where(AgentRun.id.in_(pending))).scalars().all()
        rows.extend(batch)
        parent_ids = [row.id for row in batch]
        pending = list(
            session.execute(select(AgentRun.id).where(AgentRun.parent_run_id.in_(parent_ids))).scalars()
        )
    costs = [row.cost_usd for row in rows if row.cost_usd is not None]
    return {
        "agents": list(dict.fromkeys(row.agent for row in rows)),
        "models": list(dict.fromkeys(row.model for row in rows)),
        "tools": [tool for row in rows for tool in (row.tools_called or [])],
        "cost_usd": sum(costs) if costs else None,
    }


def cmd_feedback(args: argparse.Namespace) -> None:
    from sqlalchemy import select

    from health_agent.db.models import Conversation, Feedback
    from health_agent.db.session import get_session

    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
    with get_session() as session:
        stmt = (
            select(Feedback, Conversation)
            .join(Conversation, Conversation.id == Feedback.conversation_id)
            .where(Feedback.updated_at >= since)
            .order_by(Feedback.updated_at.desc())
        )
        if args.bad:
            stmt = stmt.where(Feedback.rating == -1)
        rows = session.execute(stmt).all()
        output = []
        for feedback, answer in rows:
            tree = _agent_run_tree(session, feedback.agent_run_id)
            question = None if answer.agent in {"daily", "weekly"} else session.execute(
                select(Conversation.content)
                .where(
                    Conversation.chat_id == answer.chat_id,
                    Conversation.role == "user",
                    Conversation.created_at <= answer.created_at,
                )
                .order_by(Conversation.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            output.append(
                "\n".join(
                    [
                        f"## {'👍' if feedback.rating > 0 else '👎'} {feedback.updated_at.isoformat()}",
                        f"- agenci: {', '.join(tree['agents']) or answer.agent or '-'}",
                        f"- modele: {', '.join(tree['models']) or '-'}",
                        f"- narzędzia: {', '.join(tree['tools']) or '-'}",
                        f"- koszt drzewa: ${tree['cost_usd']:.4f}" if tree["cost_usd"] is not None else "- koszt drzewa: -",
                        f"- pytanie: {question or '-'}",
                        f"- odpowiedź: {answer.content}",
                        f"- komentarz: {feedback.comment or '-'}",
                    ]
                )
            )

    text = "\n\n".join(output) if output else "Brak feedbacku dla wybranych filtrów."
    if args.export:
        Path(args.export).write_text(text + "\n", encoding="utf-8")
        print(f"Zapisano {len(output)} ocen: {args.export}")
    else:
        print(text)


def main() -> None:
    parser = argparse.ArgumentParser(prog="health-agent")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="Ręczne wywołanie ingestora (bez czekania na prawdziwe źródło)")
    ingest_sub = ingest.add_subparsers(dest="ingest_source", required=True)

    hc = ingest_sub.add_parser("healthconnect", help="Zaimportuj zapisany payload z Health Connect Webhook")
    hc.add_argument("--file", required=True, help="Ścieżka do pliku JSON z payloadem")
    hc.set_defaults(func=cmd_ingest_healthconnect)

    intervals = ingest_sub.add_parser("intervals", help="Pobierz treningi i wellness z Intervals.icu")
    intervals.add_argument("--since", required=True, help="Data od (YYYY-MM-DD)")
    intervals.add_argument("--until", required=False, help="Data do (YYYY-MM-DD), domyślnie dziś")
    intervals.set_defaults(func=cmd_ingest_intervals)

    imp = sub.add_parser("import", help="Zaimportuj notatkę/streszczenie (.txt/.md) do wiedzy agentów")
    imp.add_argument("file")
    imp.add_argument("--title", required=False)
    imp.add_argument("--date", required=False, help="Data, której dotyczy treść (YYYY-MM-DD)")
    imp.set_defaults(func=cmd_import)

    undo = sub.add_parser("undo-import", help="Cofnij import dokumentu o podanym id")
    undo.add_argument("document_id", type=int)
    undo.set_defaults(func=cmd_undo_import)

    kn = sub.add_parser("knowledge", help="Pokaż dokumenty i aktywną wiedzę")
    kn.add_argument("--domain", required=False)
    kn.add_argument("--all", action="store_true", help="także nieaktywne")
    kn.set_defaults(func=cmd_knowledge)

    chat = sub.add_parser("chat", help="Zadaj pytanie orchestratorowi (bez Telegrama, do testów)")
    chat.add_argument("question")
    chat.set_defaults(func=cmd_chat)

    daily = sub.add_parser("daily", help="Wygeneruj podsumowanie dnia (bez wysyłki)")
    daily.add_argument("--date", required=False, help="Dzień YYYY-MM-DD, domyślnie dziś")
    daily.set_defaults(func=cmd_summary)

    weekly = sub.add_parser("weekly", help="Wygeneruj podsumowanie bieżącego tygodnia (bez wysyłki)")
    weekly.set_defaults(func=cmd_summary)

    feedback = sub.add_parser("feedback", help="Pokaż oceny odpowiedzi z Telegrama")
    feedback.add_argument("--days", type=int, default=30)
    feedback.add_argument("--bad", action="store_true", help="tylko oceny 👎")
    feedback.add_argument("--export", required=False, help="zapisz raport Markdown do pliku")
    feedback.set_defaults(func=cmd_feedback)

    correlations = sub.add_parser(
        "correlations", help="Policz ustalone korelacje zdrowotne"
    )
    correlations.add_argument("--days", type=int, default=84)
    correlations.add_argument(
        "--publish", action="store_true",
        help="zapisz wersjonowane wyniki i kwalifikujące obserwacje w knowledge",
    )
    correlations.set_defaults(func=cmd_correlations)

    password = sub.add_parser(
        "hash-password", help="Wygeneruj Argon2id hash hasła dashboardu"
    )
    password.set_defaults(func=cmd_hash_password)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
