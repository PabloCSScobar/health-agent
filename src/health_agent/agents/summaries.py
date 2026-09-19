"""Dzienne i tygodniowe podsumowania tworzone równolegle przez specjalistów."""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass

from health_agent.agents.base import agent_run_group, run_agent
from health_agent.agents.registry import build_leaf_agent
from health_agent.time_utils import local_today


SUMMARY_AGENTS = ("running", "recovery", "nutrition", "body")
_SUMMARY_SYSTEM_SUFFIX = (
    "\n\nTRYB RAPORTU: nie zapisuj nowych faktów ani wniosków, nie zadawaj pytań "
    "oznaczonych ❓ i nie proponuj dalszej analizy. Brak profilu lub danych "
    "po prostu wskaż w sekcji raportu."
)
_LABELS = {
    "running": "🏃 Trening",
    "recovery": "😴 Regeneracja",
    "nutrition": "🥗 Odżywianie",
    "body": "⚖️ Waga i skład ciała",
}


@dataclass(frozen=True)
class SummaryResult:
    text: str
    root_run_id: int


def _prompt(agent_name: str, period_name: str, start: dt.date, end: dt.date) -> str:
    period = start.isoformat() if start == end else f"{start.isoformat()}–{end.isoformat()}"
    task = {
        "running": (
            "Sprawdź treningi z podanego okresu. Podaj 2–4 najważniejsze liczby, "
            "krótką ocenę obciążenia względem dostępnej bazy i jedną rekomendację."
        ),
        "recovery": (
            "Sprawdź sen, HRV, tętno spoczynkowe, kroki i readiness. Podaj 2–4 "
            "najważniejsze liczby względem bazy i jedną rekomendację."
        ),
        "nutrition": (
            "Sprawdź kalorie, białko i bilans energetyczny. Podaj 2–4 najważniejsze "
            "liczby, zaznacz kompletność danych i jedną rekomendację."
        ),
        "body": (
            "Sprawdź wagę i trend składu ciała. Uwzględnij pomiar tylko wtedy, gdy "
            "należy do okresu; podaj maksymalnie 3 liczby i jedną rekomendację."
        ),
    }[agent_name]
    return (
        f"Tryb raportu {period_name}. Okres: {period}. {task} "
        "Najpierw użyj właściwych narzędzi. Brak danych nie oznacza zera: napisz "
        "wprost, czego brakuje, i nie wymyślaj pomiarów. Odpowiedź ma mieć najwyżej "
        "700 znaków, bez nagłówka, bez pytań oznaczonych ❓ i bez propozycji dalszej analizy."
    )


async def _build_summary(period_name: str, start: dt.date, end: dt.date) -> SummaryResult:
    async with agent_run_group(f"{period_name}_summary", list(SUMMARY_AGENTS)) as root_run_id:
        tasks = [
            run_agent(build_leaf_agent(name, system_suffix=_SUMMARY_SYSTEM_SUFFIX, allow_memory=False), name, _prompt(name, period_name, start, end))
            for name in SUMMARY_AGENTS
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    sections = []
    for name, result in zip(SUMMARY_AGENTS, results):
        if isinstance(result, BaseException):
            content = "Sekcja chwilowo niedostępna z powodu błędu generowania."
        else:
            content = str(result).strip()
        sections.append(f"{_LABELS[name]}\n{content}")

    title = "📋 Podsumowanie dnia" if period_name == "daily" else "📊 Podsumowanie tygodnia"
    shown_period = start.isoformat() if start == end else f"{start.isoformat()}–{end.isoformat()}"
    return SummaryResult(text=f"{title} ({shown_period})\n\n" + "\n\n".join(sections), root_run_id=root_run_id)


async def build_daily_summary(day: dt.date | None = None) -> SummaryResult:
    day = day or local_today()
    return await _build_summary("daily", day, day)


async def build_weekly_summary(end_date: dt.date | None = None) -> SummaryResult:
    end = end_date or local_today()
    start = end - dt.timedelta(days=end.weekday())
    return await _build_summary("weekly", start, end)
