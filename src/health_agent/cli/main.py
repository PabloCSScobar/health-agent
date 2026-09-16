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

    chat = sub.add_parser("chat", help="Zadaj pytanie orchestratorowi (bez Telegrama, do testów)")
    chat.add_argument("question")
    chat.set_defaults(func=cmd_chat)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
