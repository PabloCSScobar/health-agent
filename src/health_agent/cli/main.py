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
    from health_agent.db.session import get_session
    from health_agent.ingest.intervals import ingest_range

    oldest = dt.date.fromisoformat(args.since)
    newest = dt.date.fromisoformat(args.until) if args.until else dt.date.today()

    with get_session() as session:
        summary = ingest_range(session, oldest, newest)

    print(json.dumps(summary, indent=2, ensure_ascii=False))


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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
