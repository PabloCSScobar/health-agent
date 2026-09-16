"""Regresyjny zestaw testów agentów - koduje jako asercje realne błędy
złapane ręcznym testowaniem na żywo (patrz README.md pkt z 2026-09-16):
- orchestrator nie deleguje pytania do złego/żadnego specjalisty,
- specjalista OPISUJE że powinien wywołać ask_agent zamiast go wywołać,
- log_manual_entry wywołane dwa razy dla tej samej wypowiedzi (duplikat).

Sprawdza WYŁĄCZNIE rzeczy deterministyczne (drzewo delegacji w agent_runs,
efekty w bazie) - NIE ocenia jakości/poprawności merytorycznej tekstu
odpowiedzi, to nadal wymaga człowieka. Uruchom po każdej zmianie promptu w
registry.py albo modelu w config/agents.yaml:

    uv run python scripts/eval_agents.py

Testy 10-12 (wpisy ręczne) same sprzątają po sobie z bazy - bezpieczne do
uruchomienia na produkcyjnej bazie, ale i tak najlepiej robić to lokalnie/dev.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from sqlalchemy import delete, select

from health_agent.agents.registry import ask_orchestrator_async
from health_agent.db.models import AgentRun, BodyComposition, DailyActivity, ManualLog
from health_agent.db.session import get_session
from health_agent.tools.manual import ManualLogEntry, _write_manual_log

RoutingRow = tuple[str, int | None]


def _routing() -> list[RoutingRow]:
    with get_session() as session:
        rows = session.execute(select(AgentRun).order_by(AgentRun.id)).scalars().all()
        return [(r.agent, r.parent_run_id) for r in rows]


def _clear_agent_runs() -> None:
    with get_session() as session:
        session.execute(delete(AgentRun))


def _total_cost() -> float:
    with get_session() as session:
        rows = session.execute(select(AgentRun.cost_usd)).scalars().all()
        return sum(c for c in rows if c)


def _called(routing: list[RoutingRow]) -> set[str]:
    return {agent for agent, _ in routing}


def _children_of(routing: list[RoutingRow], parent_index: int) -> list[str]:
    """Agenci wywołani jako dzieci N-tego (0-indexed) wpisu w routing."""
    with get_session() as session:
        rows = session.execute(select(AgentRun).order_by(AgentRun.id)).scalars().all()
        parent_id = rows[parent_index].id
        return [r.agent for r in rows if r.parent_run_id == parent_id]


@dataclass
class Case:
    name: str
    question: str
    check: Callable[[str, list[RoutingRow]], list[str]]
    cleanup: Callable[[], None] | None = None


def _expect_delegate(specialist: str) -> Callable[[str, list[RoutingRow]], list[str]]:
    def check(answer: str, routing: list[RoutingRow]) -> list[str]:
        errors = []
        called = _called(routing)
        if "orchestrator" not in called:
            errors.append("orchestrator nie uruchomił się wcale")
        if specialist not in called:
            errors.append(f"oczekiwano delegacji do '{specialist}', wywołani agenci: {sorted(called)}")
        if not answer.strip():
            errors.append("pusta odpowiedź")
        return errors

    return check


def _expect_only_orchestrator(min_answer_len: int = 3) -> Callable[[str, list[RoutingRow]], list[str]]:
    def check(answer: str, routing: list[RoutingRow]) -> list[str]:
        errors = []
        called = _called(routing)
        if called != {"orchestrator"}:
            errors.append(f"oczekiwano tylko orchestratora (bez delegate) - wywołani: {sorted(called)}")
        if len(answer.strip()) < min_answer_len:
            errors.append("odpowiedź za krótka / pusta")
        return errors

    return check


def _no_described_not_executed_tool_calls(answer: str, routing: list[RoutingRow]) -> list[str]:
    """Regresja na konkretny błąd złapany na żywo: specjalista OPISUJE że
    powinien wywołać ask_agent/log_manual_entry zamiast to faktycznie zrobić
    (np. "Wywołaj ask_agent(recovery, ...)" jako tekst do użytkownika)."""
    errors = []
    lowered = answer.lower()
    for marker in ("ask_agent(", "wywołaj `ask_agent", "wywołaj ask_agent", "log_manual_entry("):
        if marker in lowered:
            errors.append(f"odpowiedź opisuje wywołanie narzędzia zamiast go wykonać (zawiera '{marker}')")
    return errors


def case_recovery_consults_nutrition_for_deficit() -> Case:
    """Regresja na konkretny bug z 16.09: recovery pytał użytkownika o zgodę
    na konsultację z nutrition zamiast po prostu wywołać ask_agent."""

    def check(answer: str, routing: list[RoutingRow]) -> list[str]:
        errors = _expect_delegate("recovery")(answer, routing)
        errors += _no_described_not_executed_tool_calls(answer, routing)
        called = _called(routing)
        lowered = answer.lower()
        asks_permission = any(
            p in lowered for p in ("chcesz, żebym zapytał", "czy mam zapytać", "chcesz żebym sprawdził nutrition")
        )
        if "nutrition" not in called and asks_permission:
            errors.append(
                "recovery zapytał użytkownika o zgodę na konsultację z nutrition zamiast ją po prostu wywołać"
            )
        return errors

    return Case(
        name="recovery konsultuje nutrition przy pytaniu o deficyt (bez pytania o zgodę)",
        question="Jaki miałem wczoraj deficyt kaloryczny?",
        check=check,
    )


def case_manual_strength() -> Case:
    marker = "EVAL-MARKER-STRENGTH-4x8-80kg-dipy-3x12"
    question = f"dziś klata: wyciskanie sztangi 4x8 80kg, dipy 3x12 [{marker}]"

    def check(answer: str, routing: list[RoutingRow]) -> list[str]:
        errors = _expect_only_orchestrator()(answer, routing)
        with get_session() as session:
            rows = session.execute(
                select(ManualLog).where(ManualLog.text_original == question)
            ).scalars().all()
        if len(rows) != 1:
            errors.append(f"oczekiwano dokładnie 1 wiersza w manual_logs, jest {len(rows)}")
        elif rows[0].kind != "strength":
            errors.append(f"zły kind: {rows[0].kind!r} (oczekiwano 'strength')")
        return errors

    def cleanup() -> None:
        with get_session() as session:
            session.execute(delete(ManualLog).where(ManualLog.text_original == question))

    return Case(name="wpis ręczny: trening siłowy", question=question, check=check, cleanup=cleanup)


def case_manual_weight() -> Case:
    marker_weight = 66.61  # nierealistyczna, rozpoznawalna wartość testowa
    question = f"waga {marker_weight}, tłuszcz 19.9%"

    def check(answer: str, routing: list[RoutingRow]) -> list[str]:
        errors = _expect_only_orchestrator()(answer, routing)
        with get_session() as session:
            rows = session.execute(
                select(BodyComposition).where(BodyComposition.weight_kg == marker_weight)
            ).scalars().all()
        if len(rows) != 1:
            errors.append(f"oczekiwano dokładnie 1 wiersza w body_composition z wagą {marker_weight}, jest {len(rows)}")
        return errors

    def cleanup() -> None:
        with get_session() as session:
            session.execute(delete(ManualLog).where(ManualLog.text_original == question))
            session.execute(delete(BodyComposition).where(BodyComposition.weight_kg == marker_weight))

    return Case(name="wpis ręczny: waga", question=question, check=check, cleanup=cleanup)


def case_manual_daily_calories() -> Case:
    marker_kcal = 2601  # nierealistyczna, rozpoznawalna wartość testowa
    question = f"spaliłem dziś {marker_kcal} kalorii według zegarka"

    def check(answer: str, routing: list[RoutingRow]) -> list[str]:
        errors = _expect_only_orchestrator()(answer, routing)
        with get_session() as session:
            rows = session.execute(
                select(DailyActivity).where(
                    DailyActivity.source == "manual", DailyActivity.calories_total == marker_kcal
                )
            ).scalars().all()
        if len(rows) != 1:
            errors.append(f"oczekiwano dokładnie 1 wiersza w daily_activity z {marker_kcal} kcal, jest {len(rows)}")
        return errors

    def cleanup() -> None:
        with get_session() as session:
            session.execute(delete(ManualLog).where(ManualLog.text_original == question))
            session.execute(
                delete(DailyActivity).where(
                    DailyActivity.source == "manual", DailyActivity.calories_total == marker_kcal
                )
            )

    return Case(name="wpis ręczny: kalorie z zegarka", question=question, check=check, cleanup=cleanup)


AGENT_CASES: list[Case] = [
    Case("ostatni trening", "Jaki był mój ostatni trening?", _expect_delegate("running")),
    Case("kroki wczoraj", "Ile kroków zrobiłem wczoraj?", _expect_delegate("recovery")),
    Case("białko dzisiaj", "Ile białka zjadłem dzisiaj?", _expect_delegate("nutrition")),
    Case("waga i trend", "Jaka jest moja waga i trend z ostatnich 30 dni?", _expect_delegate("body")),
    Case("sen i HRV", "Jak spałem tej nocy i jakie mam HRV?", _expect_delegate("recovery")),
    Case(
        "kalorie spalone dzisiaj -> recovery, NIE running",
        "Ile kalorii spaliłem dzisiaj?",
        _expect_delegate("recovery"),
    ),
    case_recovery_consults_nutrition_for_deficit(),
    Case("trening w konkretny dzień", "Jaki miałem trening w zeszły wtorek?", _expect_delegate("running")),
    Case(
        "brak danych - nie zmyślaj",
        "Ile kalorii spaliłem na treningu pływackim w marcu 2020?",
        _expect_delegate("running"),
    ),
    case_manual_strength(),
    case_manual_weight(),
    case_manual_daily_calories(),
]


def unit_test_manual_log_dedup() -> tuple[bool, str]:
    """Test jednostkowy (bez LLM, deterministyczny) dedupu z tools/manual.py -
    regresja na bug z 16.09: model czasem wołał log_manual_entry 2x dla tej
    samej wypowiedzi, co bez dedupu dawało duplikat w manual_logs (dla
    kind='strength' bez żadnej ochrony na poziomie analitycznej tabeli).

    Testuje `_write_manual_log` (zwraca id) bezpośrednio, nie publiczne
    `log_manual_entry` (zwraca już sformatowany string-potwierdzenie, patrz
    komentarz w registry.py o output functions) - to test logiki dedupu, nie
    formatowania."""
    marker = "EVAL-MARKER-DEDUP-UNIT-TEST-xyz123"
    entry = ManualLogEntry(kind="strength", payload={"test": True}, text_original=marker)
    try:
        id1 = _write_manual_log(entry)
        id2 = _write_manual_log(entry)
        if id1 != id2:
            return False, f"dedup NIE zadziałał: id1={id1}, id2={id2} (oczekiwano tego samego id)"
        with get_session() as session:
            count = len(
                session.execute(select(ManualLog).where(ManualLog.text_original == marker)).scalars().all()
            )
        if count != 1:
            return False, f"w manual_logs jest {count} wierszy zamiast 1"
        return True, ""
    finally:
        with get_session() as session:
            session.execute(delete(ManualLog).where(ManualLog.text_original == marker))


async def run_case(case: Case) -> tuple[bool, list[str], float, float]:
    _clear_agent_runs()
    start = time.monotonic()
    answer = await ask_orchestrator_async(case.question)
    duration = time.monotonic() - start
    routing = _routing()
    errors = case.check(answer, routing)
    cost = _total_cost()
    if case.cleanup:
        case.cleanup()
    return not errors, errors, duration, cost


async def main() -> None:
    print(f"=== Test jednostkowy: dedup log_manual_entry (bez LLM) ===")
    ok, msg = unit_test_manual_log_dedup()
    print(f"{'PASS' if ok else 'FAIL'}: dedup manual_logs" + (f" - {msg}" if msg else ""))
    print()

    failures = 0 if ok else 1
    total_cost = 0.0
    for case in AGENT_CASES:
        passed, errors, duration, cost = await run_case(case)
        total_cost += cost
        status = "PASS" if passed else "FAIL"
        print(f"{status}: {case.name}  ({duration:.1f}s, ${cost:.4f})")
        for e in errors:
            print(f"    - {e}")
        if not passed:
            failures += 1

    print()
    print(f"Razem: {len(AGENT_CASES) + 1} testów, {failures} nieudanych, koszt LLM: ${total_cost:.4f}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
