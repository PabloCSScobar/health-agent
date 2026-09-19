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
    uv run python scripts/eval_agents.py coaches    # recovery+nutrition+body+strength (albo jedna nazwa)
    uv run python scripts/eval_agents.py import     # import wiedzy: ekstrakcja, rekoncyliacja, undo, użycie
    uv run python scripts/eval_agents.py all

Skrypt czyści całą tabelę `agent_runs` i wykonuje zapisy. Wymaga
`APP_ENV=test` oraz osobnej bazy testowej; NIGDY nie uruchamiaj go na produkcji.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from sqlalchemy import delete, select

from health_agent.agents.registry import ask_orchestrator_async
from health_agent.db.models import AgentRun, BodyComposition, DailyActivity, ManualLog
from health_agent.db.session import get_session
from health_agent.tools.manual import ManualLogEntry, _write_manual_log
from health_agent.settings import settings

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
    for marker in (
        "ask_agent(",
        "wywołaj `ask_agent",
        "wywołaj ask_agent",
        "log_manual_entry(",
        "log_manual_entries(",
    ):
        if marker in lowered:
            errors.append(f"odpowiedź opisuje wywołanie narzędzia zamiast go wykonać (zawiera '{marker}')")
    return errors


def case_recovery_consults_nutrition_for_deficit() -> Case:
    """Regresja na konkretny bug z 16.09: recovery pytał użytkownika o zgodę
    na konsultację z nutrition zamiast po prostu wywołać ask_agent."""

    def check(answer: str, routing: list[RoutingRow]) -> list[str]:
        # Od 2026-09-17 deficyt liczy nutrition JEDNYM narzędziem
        # (get_energy_balance: zjedzone - wydatek); wcześniej recovery musiało
        # konsultować nutrition przez ask_agent. Wspólny inwariant obu wersji:
        # nikt nie pyta użytkownika o zgodę na sprawdzenie i nie opisuje
        # wywołania narzędzia zamiast je wykonać.
        errors = _expect_delegate("nutrition")(answer, routing)
        errors += _no_described_not_executed_tool_calls(answer, routing)
        if "get_energy_balance" not in _tools_used_by("nutrition"):
            errors.append(f"nutrition nie użył get_energy_balance (użył: {_tools_used_by('nutrition')})")
        lowered = answer.lower()
        if any(p in lowered for p in ("chcesz, żebym zapytał", "czy mam zapytać", "chcesz żebym sprawdził")):
            errors.append("agent pyta użytkownika o zgodę na sprawdzenie danych zamiast je sprawdzić")
        return errors

    return Case(
        name="deficyt -> nutrition liczy bilans samo (bez pytania o zgodę)",
        question="Jaki miałem wczoraj deficyt kaloryczny?",
        check=check,
    )


def case_manual_strength() -> Case:
    # Nietypowe ćwiczenie w treści = marker; model potrafi uciąć doklejony
    # znacznik z text_original (złapane na żywo), więc dopasowujemy prefiksem.
    prefix = "dziś klata: wyciskanie sztangi 4x8 80kg, dipy 3x12, EVAL-ćwiczenie-testowe 2x5"
    question = prefix

    def check(answer: str, routing: list[RoutingRow]) -> list[str]:
        errors = _expect_only_orchestrator()(answer, routing)
        with get_session() as session:
            rows = session.execute(
                select(ManualLog).where(ManualLog.text_original.like(prefix[:40] + "%"))
            ).scalars().all()
        if len(rows) != 1:
            errors.append(f"oczekiwano dokładnie 1 wiersza w manual_logs, jest {len(rows)}")
        else:
            if rows[0].kind != "strength":
                errors.append(f"zły kind: {rows[0].kind!r} (oczekiwano 'strength')")
            exs = (rows[0].payload_json or {}).get("cwiczenia")
            if not isinstance(exs, list) or len(exs) < 2 or "nazwa" not in exs[0] or "serie" not in exs[0]:
                errors.append(f"payload nie w kanonicznym schemacie (cwiczenia/nazwa/serie/powtorzenia/ciezar_kg): {rows[0].payload_json}")
        return errors

    def cleanup() -> None:
        with get_session() as session:
            session.execute(delete(ManualLog).where(ManualLog.text_original.like(prefix[:40] + "%")))

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


def _expect_tools(agent: str, *tools: str, consult: str | None = None):
    """`agent` został wywołany, użył KAŻDEGO z podanych narzędzi (nie tylko
    wylistował je w tekście) i - opcjonalnie - sam skonsultował innego
    specjalistę (regresja na 'opisuje zamiast wywołać')."""

    def check(answer: str, routing: list[RoutingRow]) -> list[str]:
        errors = _expect_delegate(agent)(answer, routing)
        errors += _no_described_not_executed_tool_calls(answer, routing)
        used = _tools_used_by(agent)
        for t in tools:
            if t not in used:
                errors.append(f"{agent} nie użył `{t}` (użył: {used})")
        if consult and consult not in _called(routing):
            errors.append(f"{agent} nie skonsultował `{consult}` przez ask_agent (wywołani: {sorted(_called(routing))})")
        return errors

    return check


def _expect_running_tools(*tools: str, consult: str | None = None):
    return _expect_tools("running", *tools, consult=consult)


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

JUDGE_CRITERIA_ANALYSIS = (
    "1. LICZBY: odpowiedź podaje 2-4 konkretne liczby z datą/okresem.\n"
    "2. WZGLĘDEM BAZY: interpretuje je względem bazy/trendu/celu/zakresu TEGO użytkownika (np. 'powyżej Twojej "
    "średniej', 'w zakresie 1.6-2.2 g/kg', 'vs poprzedni tydzień'), a nie tylko podaje wartości.\n"
    "3. JEDNA REKOMENDACJA: dokładnie jedna konkretna, wykonalna rekomendacja W DOMENIE agenta: running - dystans/"
    "tempo/strefa/dzień; recovery - dzień lekki/normalny/mocny lub konkretne działanie (godzina snu); nutrition - "
    "produkt/ilość/pora/zamiana; body - co zmienić (lub nic) i kiedy sprawdzić ponownie; strength - ciężar/zakres/"
    "ćwiczenie/dzień. 'Słuchaj organizmu' albo lista 3 rad = 0.\n"
    "4. PEWNOŚĆ: jawna ocena pewności (wysoka/średnia/niska) i czego brakuje.\n"
    "5. BEZ ZMYŚLANIA: brak danych, których agent nie może mieć (VO2max, Training Effect, nawodnienie, samopoczucie "
    "bez wpisu), brak DIAGNOZ medycznych (odesłanie do lekarza/fizjo przy objawach jest OK i pożądane).\n"
    "WYJĄTEK: jeśli narzędzia nie miały danych i odpowiedź mówi to WPROST oraz podaje, co zrobić, żeby dane były "
    "(np. format wpisu) - kryteria 1-3 uznaj za spełnione (1). Zmyślenie liczb przy braku danych = 0 w 5."
)
JUDGE_CRITERIA_FACT = (
    "To pytanie o FAKT (np. 'ile ważę', 'ile białka wczoraj') - poprawna odpowiedź jest KRÓTKA (1-3 zdania) i NIE "
    "musi zawierać rekomendacji ani oceny pewności.\n"
    "1. LICZBY: podaje liczbę(y) z datą/dniem, o które pytano.\n"
    "2. ODNIESIENIE: jedno krótkie odniesienie do bazy/celu/zakresu (np. '1,75 g/kg', 'baza 71±11', 'do celu 3,4 kg') "
    "- jeśli danych do odniesienia obiektywnie brak (pusty profil, 1 pomiar), brak odniesienia jest OK (1).\n"
    "3. ZWIĘZŁOŚĆ: nie rozpisuje pełnej analizy ani listy rad; krótka oferta pogłębienia jest OK.\n"
    "4. BEZ UNIKU: nie odpowiada pytaniem na pytanie zamiast podać liczbę.\n"
    "5. BEZ ZMYŚLANIA: tylko liczby z danych; brak = powiedziane wprost."
)


async def judge_running_answer(question: str, answer: str, mode: str = "analysis") -> tuple[int, list[str]]:
    """Sędzia LLM (Haiku, tanio) - sprawdza KONTRAKT odpowiedzi specjalisty
    z prompts/<agent>.md, NIE poprawność liczb (tego bez dostępu do wyników
    narzędzi nie da się ocenić; liczby pilnują testy jednostkowe + zasada
    'tylko z narzędzi' w promptcie). `mode`: "analysis" (pełny format) albo
    "fact" (krótka odpowiedź faktograficzna). Miękki sygnał: próg 4/5.
    Kalibracja 2026-09-17: pierwsza wersja karała pytania o fakt za brak
    rekomendacji, strength za 'zero liczb' przy braku danych, recovery za
    brak tempa/dystansu (kryterium było biegowe) i nutrition za odesłanie
    do lekarza - wszystko zgodne z promptami, więc to sędzia był źle
    skalibrowany, nie coache."""
    from pydantic import BaseModel
    from pydantic_ai import Agent

    from health_agent.agents.base import _build_anthropic_model

    class Verdict(BaseModel):
        scores: list[int]  # 5 x 0/1 w kolejności kryteriów
        failed_reasons: list[str] = []

    criteria = JUDGE_CRITERIA_FACT if mode == "fact" else JUDGE_CRITERIA_ANALYSIS
    judge = Agent(
        _build_anthropic_model("anthropic:claude-haiku-4-5"),
        system_prompt="Jesteś surowym, ale sprawiedliwym recenzentem odpowiedzi trenera/analityka zdrowia. Oceń KAŻDE "
        "kryterium 0 albo 1, zgodnie z jego definicją (nie dokładaj własnych wymagań). Odpowiedz wyłącznie strukturą. "
        "Kryteria:\n" + criteria,
        output_type=Verdict,
    )
    r = await judge.run(f"PYTANIE UŻYTKOWNIKA:\n{question}\n\nODPOWIEDŹ:\n{answer}")
    v = r.output
    return sum(v.scores[:5]), v.failed_reasons



# ---------------------------------------------------------------------------
# Pozostali coache (recovery / nutrition / body / strength) + profil - ten sam
# wzorzec co running: inwarianty narzędzi bez LLM, pytania z asercjami na
# narzędzia, sędzia LLM na kontrakt odpowiedzi.
# ---------------------------------------------------------------------------

def unit_test_coach_tools() -> list[str]:
    import datetime as dt
    import statistics

    from sqlalchemy import delete as _delete

    from health_agent.db.models import AgentMemory, ManualLog
    from health_agent.tools import body as B
    from health_agent.tools import nutrition as N
    from health_agent.tools import profile as P
    from health_agent.tools import recovery as Rc
    from health_agent.tools import strength as S
    from health_agent.tools.energy import estimate_daily_expenditure

    errors: list[str] = []

    rb = Rc.get_recovery_baseline(28)
    if "metrics" in rb:
        for name, m in rb["metrics"].items():
            if m["baseline_sd"] is not None and m["baseline_sd"] < 0:
                errors.append(f"recovery: ujemne SD dla {name}")
            if m["latest"] is not None and m["z_latest_vs_baseline"] is not None and m["baseline_sd"]:
                if (m["latest"] - m["baseline_mean"]) * m["z_latest_vs_baseline"] < 0:
                    errors.append(f"recovery: znak z-score niezgodny z odchyleniem dla {name}")
        sh = rb["sleep_hours"]
        if sh["last_7_days"] and sh["debt_7d_h_vs_target"] is not None:
            expected = round(sum(sh["target_h"] - d["h"] for d in sh["last_7_days"] if d["h"] is not None), 1)
            if abs(expected - sh["debt_7d_h_vs_target"]) > 0.15:
                errors.append(f"recovery: dług snu {sh['debt_7d_h_vs_target']} != {expected}")
        if rb["readiness"]["label"] is None and rb["readiness"]["composite_z"] is not None:
            errors.append("recovery: readiness bez etykiety")

    ns = N.get_nutrition_summary(30)
    if ns["days_with_data"]:
        if ns["avg"]["kcal"] != round(statistics.mean(d["kcal"] for d in ns["days"])):
            errors.append("nutrition: średnia kcal nie zgadza się z dniami")
        w = ns["protein_target"]["weight_kg_used"]
        if w and ns["protein_g_per_kg"] is not None and abs(ns["protein_g_per_kg"] - ns["avg"]["protein_g"] / w) > 0.02:
            errors.append("nutrition: białko g/kg != białko/waga")
        if ns["days_with_data"] != len(ns["days"]):
            errors.append("nutrition: days_with_data != len(days)")
    yesterday = dt.date.today() - dt.timedelta(days=1)
    eb = N.get_energy_balance(7, end_date=yesterday)
    if eb["period"]["to"] != yesterday.isoformat():
        errors.append("nutrition: end_date nie respektowane w get_energy_balance")
    for d in eb["days"]:
        if d["balance_kcal"] is not None and d["balance_kcal"] != round(d["intake_kcal"] - d["expenditure_kcal"]):
            errors.append(f"nutrition: bilans {d['date']} != intake - expenditure")
    ff = N.find_foods(None, 30, "protein_g")
    if len(ff["items"]) > 1 and any(ff["items"][i]["total_protein_g"] < ff["items"][i + 1]["total_protein_g"] for i in range(len(ff["items"]) - 1)):
        errors.append("nutrition: find_foods nie posortowane malejąco po białku")

    exp = estimate_daily_expenditure(dt.date(2026, 9, 16))
    if exp["method"] == "healthconnect" and exp["total_kcal"] < 1200:
        errors.append("energy: wartość HC < 1200 kcal uznana za całodniowy wydatek")

    bt = B.get_body_trend(30)
    if bt.get("points", 0) and bt["points"] < B.MIN_POINTS_FOR_TREND and bt["slope_kg_per_week"] is not None:
        errors.append("body: nachylenie policzone z <3 pomiarów")
    if bt.get("points", 0) and "za mało" not in bt["data_sufficiency"] and bt["slope_kg_per_week"] is None:
        errors.append("body: wystarczające dane, a brak nachylenia")

    variants = [
        ({"cwiczenia": [{"nazwa": "wyciskanie sztangi", "serie": 4, "powtorzenia": 8, "ciezar_kg": 80}]}, "klatka", 4, 101.3),
        ({"ćwiczenia": [{"nazwa": "Przysiad", "serie": [{"powtórzenia": 5, "ciężar_kg": 100}, {"powtórzenia": 5, "ciężar_kg": 105}]}]}, "nogi", 2, 122.5),
        ({"exercises": [{"name": "Martwy ciąg", "sets": 3, "reps": 5, "kg": 120}]}, "plecy", 3, 140.0),
        ({"cwiczenia": [{"nazwa": "dipy", "serie": 3, "powtorzenia": 12}]}, "klatka", 3, None),
    ]
    for payload, muscle, sets, e1rm in variants:
        lg = ManualLog(kind="strength", payload_json=payload, text_original="t", logged_at=dt.datetime.now(dt.timezone.utc))
        lg.id = 0
        ex = S._parse_log(lg)["exercises"][0]
        if ex["muscle"] != muscle or ex["sets"] != sets or ex["e1rm_kg"] != e1rm:
            errors.append(f"strength: parser {payload} -> {ex['muscle']}/{ex['sets']}/{ex['e1rm_kg']} (oczekiwano {muscle}/{sets}/{e1rm})")

    marker_key, marker_val = "inne", "EVAL-MARKER-PROFILE-xyz"
    before = P.get_user_profile().get(marker_key)
    try:
        P.set_user_profile_facts({marker_key: marker_val, "nieznany klucz": "x"})
        prof = P.get_user_profile()
        if prof.get("inne") not in (marker_val, "x"):
            errors.append("profile: zapis/odczyt nie działa")
        if "nieznany_klucz" in prof:
            errors.append("profile: nieznany klucz nie został zmapowany na 'inne'")
    finally:
        with get_session() as session:
            if before is None:
                session.execute(_delete(AgentMemory).where(AgentMemory.agent == P.PROFILE_AGENT, AgentMemory.key == marker_key))
            else:
                P.remember(P.PROFILE_AGENT, marker_key, before)
    return errors


COACH_CASES: dict[str, list[Case]] = {
    "recovery": [
        Case("recovery: fakt (HRV wczoraj)", "Jakie miałem wczoraj HRV?", _expect_tools("recovery", "get_recovery_day")),
        Case("recovery: ocena tygodnia", "Jak się regeneruję w tym tygodniu?", _expect_tools("recovery", "get_recovery_baseline")),
        Case("recovery: zmęczenie / gotowość", "Czy jestem zmęczony? Mogę dziś mocno trenować?", _expect_tools("recovery", "get_recovery_baseline")),
    ],
    "nutrition": [
        Case("nutrition: fakt (białko wczoraj)", "Ile białka zjadłem wczoraj?", _expect_tools("nutrition", "get_nutrition_day")),
        Case("nutrition: fakt (deficyt wczoraj) -> bez pytania recovery", "Jaki miałem wczoraj deficyt kaloryczny?", _expect_tools("nutrition", "get_energy_balance")),
        Case("nutrition: ocena diety", "Jak wygląda moja dieta w ostatnich dniach? Co poprawić?", _expect_tools("nutrition", "get_nutrition_summary")),
        Case("nutrition: skąd makro", "Skąd mam tyle tłuszczu w diecie?", _expect_tools("nutrition", "find_foods")),
    ],
    "body": [
        Case("body: fakt (waga)", "Ile ważę?", _expect_tools("body")),
        Case("body: trend / cel", "Czy chudnę? Kiedy dojdę do celu?", _expect_tools("body", "get_body_trend")),
    ],
    "strength": [
        Case("strength: fakt (sesje)", "Co ostatnio robiłem na siłowni?", _expect_tools("strength", "get_strength_sessions")),
        Case("strength: progres ćwiczenia", "Czy robię postępy w wyciskaniu?", _expect_tools("strength", "get_exercise_progress")),
    ],
}


async def run_coach_suite(names: list[str]) -> tuple[int, int, float]:
    print("=== Coache: testy jednostkowe narzędzi + profil (bez LLM) ===")
    errs = unit_test_coach_tools()
    print(f"{'PASS' if not errs else 'FAIL'}: narzędzia recovery/nutrition/body/strength/profile" + "".join(f"\n    - {e}" for e in errs))
    failures = 1 if errs else 0
    total_cost, n = 0.0, 1
    for name in names:
        print(f"\n=== {name}: pytania + sędzia LLM ===")
        for case in COACH_CASES[name]:
            _clear_agent_runs()
            start = time.monotonic()
            answer = await ask_orchestrator_async(case.question)
            duration = time.monotonic() - start
            errors = case.check(answer, _routing())
            cost = _total_cost()
            if name == "strength" and not _strength_has_data():
                # Bez wpisów siłowych sędzia nie ma czego oceniać - Haiku
                # losowo ignoruje klauzulę "brak danych" (ta sama odpowiedź
                # raz 5/5, raz 2/5). Deterministycznie: coach ma uczciwie
                # powiedzieć, że danych brak, i nie wymyślać ciężarów.
                score = -1
                if not re.search(r"nie mam|brak|nie ma (wpis|dan)|żadn", answer.lower()):
                    errors.append("strength bez danych: odpowiedź nie mówi wprost, że brak wpisów")
                if re.search(r"e1rm\s*[:=]?\s*\d|\d+\s*kg\s*x\s*\d", answer.lower()) and "np." not in answer.lower():
                    errors.append("strength bez danych: odpowiedź zawiera konkretne ciężary/e1RM - skąd?")
            else:
                mode = "fact" if "fakt" in case.name else "analysis"
                score, reasons = await judge_running_answer(case.question, answer, mode=mode)
                if score < 4:
                    errors.append(f"sędzia: {score}/5 - " + "; ".join(reasons))
            total_cost += cost
            n += 1
            status = "PASS" if not errors else "FAIL"
            judge_str = f"sędzia {score}/5" if score >= 0 else "sędzia pominięty (brak danych)"
            print(f"{status}: {case.name}  ({duration:.1f}s, ${cost:.4f}, {judge_str})")
            for e in errors:
                print(f"    - {e}")
            if errors:
                failures += 1
    return failures, n, total_cost


def _strength_has_data() -> bool:
    from health_agent.tools.strength import get_strength_sessions

    return bool(get_strength_sessions(90)["manual_sessions"])



# ---------------------------------------------------------------------------
# Import wiedzy (agents/importer.py): ekstrakcja, rekoncyliacja, dedup, undo,
# digest + jeden test agentowy (czy coach używa wiedzy z notatki).
# ---------------------------------------------------------------------------

async def run_import_suite() -> tuple[int, int, float]:
    from pathlib import Path

    from sqlalchemy import delete as _delete

    from health_agent.agents.importer import import_document, undo_import
    from health_agent.db.models import AgentMemory, Document, Knowledge
    from health_agent.tools import knowledge as K
    from health_agent.tools import profile as P

    fixtures = Path(__file__).parent / "fixtures"
    doc1 = (fixtures / "import_fixture_bieganie.md").read_text(encoding="utf-8")
    doc2 = (fixtures / "import_fixture_wrzesien.md").read_text(encoding="utf-8")
    profile_before = P.get_user_profile()
    failures, n, cost = 0, 0, 0.0
    doc_ids: list[int] = []

    def _doc_id(report: str) -> int | None:
        m = re.search(r"\(#(\d+)", report)
        return int(m.group(1)) if m else None

    def result(name: str, errors: list[str], extra: str = "") -> None:
        nonlocal failures, n
        n += 1
        status = "PASS" if not errors else "FAIL"
        print(f"{status}: {name}{('  (' + extra + ')') if extra else ''}")
        for e in errors:
            print(f"    - {e}")
        if errors:
            failures += 1

    print("=== Import wiedzy ===")
    try:
        # 1. ekstrakcja
        _clear_agent_runs()
        r1 = await import_document(doc1, title="EVAL fixture czerwiec", source="cli")
        c1 = _total_cost(); cost += c1
        d1 = _doc_id(r1)
        errs = []
        if d1 is None:
            errs.append("brak id dokumentu w raporcie")
        else:
            doc_ids.append(d1)
            rows = K.knowledge_from_document(d1)
            if len(rows) < 10:
                errs.append(f"za mało faktów: {len(rows)} (<10)")
            doms = {r["domain"] for r in rows}
            if not {"running", "nutrition"} <= doms:
                errs.append(f"brak domen running/nutrition: {doms}")
            if not any(r["kind"] == "zyciowka" and (r["event_date"] or "").startswith("2024") for r in rows):
                errs.append("brak życiówki 10 km z datą 2024")
            if not any(r["kind"] == "lekcja" for r in rows):
                errs.append("brak żadnej 'lekcji' (sen <6h -> gorszy bieg)")
            spec = [r for r in rows if "przetren" in r["content"].lower()]
            if spec and not any(r["confidence"] == "low" for r in spec):
                errs.append("spekulacja o przetrenowaniu zapisana bez 'niskiej pewności' (prompt: low albo pomiń)")
            prof = P.get_user_profile()
            added = set(prof) - set(profile_before)
            if not added:
                errs.append("import nie dopisał nic do profilu (oczekiwano np. cel_biegowy/kontuzje)")
            if any(k in profile_before and prof[k] != profile_before[k] for k in profile_before):
                errs.append("import nadpisał istniejący klucz profilu bez daty dokumentu")
        result("import: ekstrakcja z fixture", errs, f"${c1:.4f}, {len(K.knowledge_from_document(d1)) if d1 else 0} faktów")

        # 2. dedup po hashu
        r_dup = await import_document(doc1, title="EVAL fixture czerwiec", source="cli")
        result("import: dedup tego samego dokumentu", [] if "już zaimportowany" in r_dup else [f"brak komunikatu o duplikacie: {r_dup[:120]}"])

        # 3. rekoncyliacja
        _clear_agent_runs()
        r2 = await import_document(doc2, title="EVAL fixture wrzesień", source="cli", doc_date=dt.date(2026, 9, 15))
        c2 = _total_cost(); cost += c2
        d2 = _doc_id(r2)
        errs = []
        if d2 is None:
            errs.append("brak id dokumentu w raporcie")
        else:
            doc_ids.append(d2)
            with get_session() as session:
                allk = session.execute(select(Knowledge)).scalars().all()
                session.expunge_all()
            active_2021 = [k for k in allk if k.active and "2021" in k.content]
            if len(active_2021) != 1:
                errs.append(f"fakt 'od 2021' powinien być raz (aktywny), jest {len(active_2021)}")
            superseded = [k for k in allk if k.superseded_by is not None]
            if not superseded:
                errs.append("żaden stary fakt nie został zastąpiony (oczekiwano cel wagi 85->84)")
            old_goal = [k for k in allk if k.source_id == d1 and "85" in k.content and "cel" in k.content.lower()]
            if old_goal and any(k.active for k in old_goal):
                errs.append("stary cel wagowy 85 kg (z pierwszego dokumentu) nadal aktywny")
            shin = [k for k in allk if "shin" in k.content.lower() and k.source_id == d1 and k.kind == "zdarzenie"]
            if shin and all(k.active for k in shin):
                errs.append("kontuzja shin splints nie została dezaktywowana mimo 'temat zamknięty'")
            if "❓" not in r2 or "cel_waga_kg" not in r2:
                errs.append("brak pytania o konflikt profilu cel_waga_kg (profil zapisany dziś, dokument z 15.09 -> pytanie)")
        result("import: rekoncyliacja drugim dokumentem", errs, f"${c2:.4f}")

        # 4. digest w limicie
        dg = K.knowledge_digest("running")
        result("import: digest running w limicie", [] if len(dg) <= K.DIGEST_LIMIT_CHARS + 300 and "52:10" in dg else [f"digest {len(dg)} zn. / brak życiówki"])

        # 5. agent używa wiedzy
        _clear_agent_runs()
        answer = await ask_orchestrator_async("Jaki mam rekord na 10 km?")
        c3 = _total_cost(); cost += c3
        errs = _expect_delegate("running")(answer, _routing())
        if "52:10" not in answer:
            errs.append(f"odpowiedź nie zawiera rekordu 52:10 z notatki: {answer[:160]}")
        result("import: running odpowiada z wiedzy (rekord 10 km)", errs, f"${c3:.4f}")

        # 6. undo
        errs = []
        for did in reversed(doc_ids):
            undo_import(did)
        with get_session() as session:
            left_docs = session.execute(select(Document).where(Document.id.in_(doc_ids))).scalars().all()
            left_k = session.execute(select(Knowledge).where(Knowledge.source_id.in_(doc_ids))).scalars().all()
        if left_docs or left_k:
            errs.append(f"po undo zostały: {len(left_docs)} dokumentów, {len(left_k)} wpisów")
        result("import: undo usuwa wiedzę i dokumenty", errs)
    finally:
        # sprzątanie: dokumenty/wiedza z fixture + klucze profilu dopisane przez import
        with get_session() as session:
            for did in doc_ids:
                session.execute(_delete(Knowledge).where(Knowledge.source_id == did, Knowledge.source_type == "document"))
                session.execute(_delete(Document).where(Document.id == did))
            for key in set(P.get_user_profile()) - set(profile_before):
                session.execute(_delete(AgentMemory).where(AgentMemory.agent == P.PROFILE_AGENT, AgentMemory.key == key))
        for key, val in profile_before.items():
            P.remember(P.PROFILE_AGENT, key, val)
    return failures, n, cost


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
    if settings.app_env != "test":
        raise SystemExit("eval_agents.py wymaga APP_ENV=test i osobnej bazy testowej")

    if suite == "running":
        failures, n, cost = await run_running_suite()
        print(f"\nRazem: {n} testów, {failures} nieudanych, koszt LLM: ${cost:.4f}")
        sys.exit(1 if failures else 0)
    if suite == "import":
        failures, n, cost = await run_import_suite()
        print(f"\nRazem: {n} testów, {failures} nieudanych, koszt LLM: ${cost:.4f}")
        sys.exit(1 if failures else 0)
    if suite in COACH_CASES or suite == "coaches":
        names = list(COACH_CASES) if suite == "coaches" else [suite]
        failures, n, cost = await run_coach_suite(names)
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
