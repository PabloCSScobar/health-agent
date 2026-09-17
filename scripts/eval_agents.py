"""Regresyjny zestaw testów agentów - koduje jako asercje realne błędy
złapane ręcznym testowaniem na żywo (patrz README.md pkt z 2026-09-16):
- orchestrator nie deleguje pytania do złego/żadnego specjalisty,
- specjalista OPISUJE że powinien wywołać ask_agent zamiast go wywołać,
- log_manual_entry wywołane dwa razy dla tej samej wypowiedzi (duplikat).

Sprawdza WYŁĄCZNIE rzeczy deterministyczne (drzewo delegacji w agent_runs,
efekty w bazie) - NIE ocenia jakości/poprawności merytorycznej tekstu
odpowiedzi, to nadal wymaga człowieka. Uruchom po każdej zmianie promptu w
registry.py albo modelu w config/agents.yaml:

    uv run python scripts/eval_agents.py            # core (routing, wpisy) - tanio
    uv run python scripts/eval_agents.py running    # RunningCoach: narzędzia + 6 pytań + sędzia LLM
    uv run python scripts/eval_agents.py all

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


def _tools_used_by(agent: str) -> list[str]:
    with get_session() as session:
        rows = session.execute(select(AgentRun).where(AgentRun.agent == agent).order_by(AgentRun.id)).scalars().all()
        return [t for r in rows for t in (r.tools_called or [])]


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



# ---------------------------------------------------------------------------
# RunningCoach (Faza 5b, pilot "wytrenowania" specjalisty - patrz
# prompts/running.md i tools/running.py)
# ---------------------------------------------------------------------------

def unit_test_running_tools() -> list[str]:
    """Deterministyczne inwarianty narzędzi biegowych na prawdziwej bazie -
    bez LLM, zero kosztu. Łapią regresje w logice (marsze wliczone do
    biegów, strefy w złej kolejności, procenty nie sumujące się itd.)."""
    from health_agent.tools import running as R

    errors: list[str] = []
    p = R.get_running_profile()
    zones = p["hr_zones_friel_7"]
    if len(zones) != 7 or any(zones[i]["to_hr"] >= zones[i + 1]["to_hr"] for i in range(6)):
        errors.append(f"profil: strefy nie są 7 rosnącymi progami: {zones}")
    if p["lthr"] and p["max_hr"] and not (100 < p["lthr"] < p["max_hr"] <= 230):
        errors.append(f"profil: lthr/max_hr poza sensem: {p['lthr']}/{p['max_hr']}")
    if p["base_last_4_full_weeks"]["weeks_counted"] > 4:
        errors.append("profil: policzono >4 pełne tygodnie")
    form = p["form_today"]
    if form.get("tsb_form") is not None and form["tsb_form"] != round(form["ctl_fitness"] - form["atl_fatigue"], 1):
        errors.append("profil: TSB != CTL - ATL")
    if p["last_run"] and p["last_run"]["is_walk"]:
        errors.append("profil: last_run to marsz")

    w = R.get_weekly_running_load(6)
    for wk in w["weeks"]:
        if wk["partial_current_week"] and (wk["km_change_pct"] is not None or wk["load_change_pct"] is not None):
            errors.append("weekly: niepełny tydzień ma % zmiany (mylące -100%)")
        if wk["runs"] == 0 and wk["km"] != 0:
            errors.append("weekly: km bez biegów")
    for key in ("acwr_running_only", "acwr_all_sports"):
        if w[key]["acwr"] is not None and w[key]["acwr"] < 0:
            errors.append(f"weekly: ujemny ACWR w {key}")

    i = R.get_intensity_distribution(28)
    tz = i["time_in_zones_pct"]
    if i["runs"] and abs(tz["easy_Z1_Z2"] + tz["moderate_Z3"] + tz["hard_Z4_plus"] - 100) > 2:
        errors.append(f"intensity: procenty czasu nie sumują się do 100: {tz}")
    if any(sess["km"] < R.MIN_ANALYSIS_KM for sess in i["sessions"]):
        errors.append("intensity: sesja <3 km w klasyfikacji")

    e = R.get_efficiency_trend(90)
    if any(r["ef"] <= 0 for r in e["runs"]):
        errors.append("efficiency: EF <= 0")
    if any(r["km"] < R.MIN_ANALYSIS_KM for r in e["runs"]):
        errors.append("efficiency: bieg <3 km w trendzie")

    a = R.analyze_run()  # domyślnie ostatni bieg
    if a is None:
        errors.append("analyze_run(): None mimo biegów w bazie")
    else:
        if a["is_walk"]:
            errors.append("analyze_run(): domyślny bieg to marsz")
        sa = a.get("streams_analysis", {})
        if sa.get("hr_drift_pct") is not None and not (-30 < sa["hr_drift_pct"] < 60):
            errors.append(f"analyze_run(): dryf poza sensem: {sa['hr_drift_pct']}")
        if sa.get("km_splits") and len(sa["km_splits"]) > round(a["km"]) + 1:
            errors.append("analyze_run(): więcej splitów niż km")

    c = R.find_comparable_runs()
    if c and any(r["trainer"] or r["is_walk"] for r in c["comparable"]):
        errors.append("comparable: bieżnia/marsz w porównywalnych")

    f = R.get_fitness_form(42)
    if f["current"] and f["current"].get("band") is None and f["current"].get("form_pct") is not None:
        errors.append("form: brak pasma mimo form_pct")

    with get_session() as session:
        from health_agent.db.models import Workout
        walks = session.execute(select(Workout).where(Workout.sport == "VirtualRun")).scalars().all()
        session.expunge_all()
    if walks and not all(R._is_walk(x) for x in walks if (x.avg_pace or 0) > R.WALK_PACE_THRESHOLD_S_KM):
        errors.append("_is_walk: VirtualRun z tempem >9:00/km nie rozpoznany jako marsz")
    return errors


def _expect_running_tools(*tools: str, consult: str | None = None):
    """running został wywołany, użył KAŻDEGO z podanych narzędzi (nie tylko
    wylistował je w tekście) i - opcjonalnie - sam skonsultował innego
    specjalistę (regresja na 'opisuje zamiast wywołać')."""

    def check(answer: str, routing: list[RoutingRow]) -> list[str]:
        errors = _expect_delegate("running")(answer, routing)
        errors += _no_described_not_executed_tool_calls(answer, routing)
        used = _tools_used_by("running")
        for t in tools:
            if t not in used:
                errors.append(f"running nie użył `{t}` (użył: {used})")
        if consult and consult not in _called(routing):
            errors.append(f"running nie skonsultował `{consult}` przez ask_agent (wywołani: {sorted(_called(routing))})")
        return errors

    return check


RUNNING_CASES: list[Case] = [
    Case("bieg: analiza ostatniego długiego (dryf, splity)",
         "Jak wyszedł mój ostatni długi bieg? Czy dobrze rozłożyłem siły?",
         _expect_running_tools("analyze_run")),
    Case("bieg: obciążenie tygodniowe / progresja",
         "Jak wygląda moje obciążenie biegowe w ostatnich tygodniach? Nie robię za dużo?",
         _expect_running_tools("get_weekly_running_load")),
    Case("bieg: rozkład intensywności (80/20)",
         "Czy biegam za mocno? Nie za dużo w szarej strefie?",
         _expect_running_tools("get_intensity_distribution")),
    Case("bieg: postępy (EF)",
         "Czy robię postępy w bieganiu? Biegam szybciej przy tym samym tętnie?",
         _expect_running_tools("get_efficiency_trend")),
    Case("bieg: gotowość na mocny trening -> forma + recovery",
         "Czy mogę jutro zrobić interwały?",
         _expect_running_tools("get_fitness_form", consult="recovery")),
    Case("bieg: plan na tydzień",
         "Ułóż mi plan biegowy na następny tydzień.",
         _expect_running_tools("get_running_profile")),
]

JUDGE_CRITERIA = (
    "1. LICZBY: odpowiedź podaje 2-4 konkretne liczby z datą/okresem (tempo, tętno, km, %, TSB itp.).\n"
    "2. WZGLĘDEM BAZY: interpretuje je względem bazy/trendu/stref tego biegacza (np. 'powyżej Twojej "
    "średniej', 'w Z2', 'vs poprzedni tydzień'), a nie tylko podaje wartości.\n"
    "3. JEDNA REKOMENDACJA: jest dokładnie jedna konkretna, wykonalna rekomendacja na najbliższe dni "
    "(dystans/tempo/strefa/dzień) - nie lista ogólników typu 'słuchaj organizmu'.\n"
    "4. PEWNOŚĆ: jest jawna ocena pewności (wysoka/średnia/niska) i czego brakuje.\n"
    "5. BEZ ZMYŚLANIA: nie ma sformułowań sugerujących dane, których agent nie może mieć (VO2max, "
    "Training Effect, nawodnienie, temperatura ciała, 'czułeś się' bez feel), ani porad medycznych."
)


async def judge_running_answer(question: str, answer: str) -> tuple[int, list[str]]:
    """Sędzia LLM (Haiku, tanio) - sprawdza KONTRAKT odpowiedzi RunningCoach
    z prompts/running.md, NIE poprawność liczb (tego bez dostępu do wyników
    narzędzi nie da się ocenić; liczby pilnują testy jednostkowe + zasada
    'tylko z narzędzi' w promptcie). Miękki sygnał: próg 4/5."""
    from pydantic import BaseModel
    from pydantic_ai import Agent

    from health_agent.agents.base import _build_anthropic_model

    class Verdict(BaseModel):
        scores: list[int]  # 5 x 0/1 w kolejności kryteriów
        failed_reasons: list[str] = []

    judge = Agent(
        _build_anthropic_model("anthropic:claude-haiku-4-5"),
        system_prompt="Jesteś surowym recenzentem odpowiedzi trenera biegowego. Oceń KAŻDE kryterium 0 albo 1. "
        "Odpowiedz wyłącznie strukturą. Kryteria:\n" + JUDGE_CRITERIA,
        output_type=Verdict,
    )
    r = await judge.run(f"PYTANIE UŻYTKOWNIKA:\n{question}\n\nODPOWIEDŹ TRENERA:\n{answer}")
    v = r.output
    return sum(v.scores[:5]), v.failed_reasons


async def run_running_suite() -> tuple[int, int, float]:
    print("=== RunningCoach: testy jednostkowe narzędzi (bez LLM) ===")
    errs = unit_test_running_tools()
    print(f"{'PASS' if not errs else 'FAIL'}: narzędzia biegowe" + "".join(f"\n    - {e}" for e in errs))
    failures = 1 if errs else 0
    total_cost = 0.0
    print()
    print("=== RunningCoach: pytania + sędzia LLM ===")
    for case in RUNNING_CASES:
        _clear_agent_runs()
        start = time.monotonic()
        answer = await ask_orchestrator_async(case.question)
        duration = time.monotonic() - start
        routing = _routing()
        errors = case.check(answer, routing)
        cost = _total_cost()
        score, reasons = await judge_running_answer(case.question, answer)
        if score < 4:
            errors.append(f"sędzia: {score}/5 - " + "; ".join(reasons))
        total_cost += cost
        status = "PASS" if not errors else "FAIL"
        print(f"{status}: {case.name}  ({duration:.1f}s, ${cost:.4f}, sędzia {score}/5)")
        for e in errors:
            print(f"    - {e}")
        if errors:
            failures += 1
    return failures, len(RUNNING_CASES) + 1, total_cost


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
    suite = sys.argv[1] if len(sys.argv) > 1 else "core"
    if suite == "running":
        failures, n, cost = await run_running_suite()
        print(f"\nRazem: {n} testów, {failures} nieudanych, koszt LLM: ${cost:.4f}")
        sys.exit(1 if failures else 0)

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

    if suite == "all":
        print()
        r_fail, r_n, r_cost = await run_running_suite()
        failures += r_fail
        total_cost += r_cost
        n_total = len(AGENT_CASES) + 1 + r_n
    else:
        n_total = len(AGENT_CASES) + 1

    print()
    print(f"Razem: {n_total} testów, {failures} nieudanych, koszt LLM: ${total_cost:.4f}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
