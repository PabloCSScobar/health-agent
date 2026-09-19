"""Definicje specjalistów, orchestrator, mechanizm delegate/ask_agent.

Głębokość ask_agent jest ograniczona do 1 strukturalnie, nie licznikiem w
runtime: agent zbudowany do obsługi delegate() ("pełny") dostaje narzędzie
ask_agent, ale agent zbudowany WEWNĄTRZ ask_agent ("liściowy") już go nie
dostaje - więc fizycznie nie da się zejść głębiej niż jeden poziom.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from pydantic import BaseModel

from health_agent.agents.base import build_agent, run_agent, run_agent_sync, run_agent_with_id
from health_agent.tools.body import get_body_composition_latest, get_body_composition_trend, get_body_trend
from health_agent.tools.energy import estimate_daily_expenditure
from health_agent.tools.manual_batch import log_manual_entries
from health_agent.tools.knowledge import add_knowledge, get_knowledge, knowledge_digest, list_documents, read_document, set_knowledge_active
from health_agent.tools.nutrition import (
    find_foods,
    get_energy_balance,
    get_nutrition_day,
    get_nutrition_range,
    get_nutrition_summary,
)
from health_agent.tools.profile import (
    get_user_profile,
    missing_onboarding_keys,
    onboarding_message,
    profile_prompt_block,
    set_user_profile_facts,
)
from health_agent.tools.recovery import (
    get_recovery_baseline,
    get_recovery_day,
    get_recovery_range,
    get_wellbeing_history,
)
from health_agent.tools.running import (
    analyze_run,
    find_comparable_runs,
    get_efficiency_trend,
    get_fitness_form,
    get_intensity_distribution,
    get_running_profile,
    get_weekly_running_load,
)
from health_agent.tools.strength import get_exercise_progress, get_strength_sessions, get_strength_weekly_volume
from health_agent.tools.workouts import get_latest_workout, get_workouts, get_workouts_on_date

_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


def _load_prompt(name: str) -> str:
    """Metodologia specjalisty jako plik .md, nie ciąg w Pythonie - łatwiej
    czytać, edytować i porównywać w diffie; cały plik idzie do system
    promptu (statyczny -> prompt cache)."""
    return (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


class AgentAnswer(BaseModel):
    """Ustrukturyzowana odpowiedź specjalisty na pytanie innego specjalisty
    (przez ask_agent) - krótkie podsumowanie, nie surowe dane."""

    summary: str
    evidence: list[str] = []
    confidence: str = "medium"  # low | medium | high


_PROMPT_SUFFIX = (
    "\n\nZASADY (krytyczne, zdrowotne dane muszą być wiarygodne):\n"
    "1. Zawsze używaj dostępnych narzędzi, żeby sprawdzić prawdziwe dane - "
    "nigdy nie zgaduj liczb, dat ani identyfikatorów (np. external_id).\n"
    "2. Jeśli narzędzie zwróci pustą listę / None / brak danych - odpowiedz "
    "WPROST 'nie znalazłem danych za ten okres', NIGDY nie wymyślaj "
    "realistycznie wyglądających wartości zamiast tego. To gorsze niż brak "
    "odpowiedzi - użytkownik może podjąć złą decyzję na podstawie zmyślonych "
    "danych zdrowotnych.\n"
    "3. Podawaj tylko wartości, które faktycznie wystąpiły w wyniku "
    "narzędzia - żadnych dodatkowych metryk, których narzędzie nie zwróciło "
    "(np. nie wymyślaj temperatury ciała czy stref tętna, jeśli tego nie ma "
    "w danych).\n"
    "4. Jeśli do PEŁNEJ odpowiedzi brakuje Ci danych z INNEJ dziedziny (np. "
    "pytanie o deficyt kaloryczny = kalorie spalone [Ty albo recovery] minus "
    "zjedzone [nutrition]) - masz do tego narzędzie `ask_agent`. UŻYJ GO "
    "SAM, od razu, w tej samej odpowiedzi. NIE pytaj użytkownika 'czy mam "
    "zapytać X' i NIE opisuj słownie że 'powinieneś wywołać ask_agent' - to "
    "błąd, wywołaj je naprawdę, tak samo jak każde inne narzędzie. Dopiero "
    "gdy `ask_agent` też nie da odpowiedzi, powiedz wprost czego brakuje.\n"
    "5. Jeśli w PROFILU brakuje faktu, który ZMIENIŁBY Twoją rekomendację "
    "(kontuzja przy planie, cel/data startu przy periodyzacji, problem "
    "zdrowotny przy diecie, docelowa waga przy deficycie, wzrost/wiek/płeć "
    "gdy narzędzie zwraca `missing`) - zadaj DOKŁADNIE JEDNO krótkie pytanie "
    "na samym końcu odpowiedzi, w osobnej linii zaczynającej się od '❓'. "
    "Nigdy o coś, co już jest w profilu; nigdy więcej niż jedno; nie przy "
    "krótkich odpowiedziach na pytanie o fakt. Odpowiedź użytkownika trafi "
    "do profilu automatycznie.\n"
    "6. WIEDZA: w prompcie masz digest wiedzy o użytkowniku (z jego notatek i "
    "wcześniejszych ustaleń) - uwzględniaj ją jak profil. Gdy pytanie dotyczy "
    "czegoś, czego tam nie ma (stara kontuzja, poprzedni plan, życiówka), "
    "użyj get_knowledge(domena, fraza) i w razie potrzeby read_document(id) "
    "- oryginał jest zawsze dostępny, nie zgaduj.\n"
    "Odpowiadaj po polsku, zwięźle."
)

SPECIALISTS: dict[str, tuple[str, list]] = {
    "running": (
        _load_prompt("running")
        + "\n\nKalorie: masz tylko kalorie spalone NA TRENINGU. Całodniowy wydatek "
        "(BMR + cała aktywność) ma 'recovery' - przy pytaniu 'ile spaliłem "
        "dzisiaj/wczoraj' (nie na konkretnym treningu) odeślij tam."
        + _PROMPT_SUFFIX,
        [
            get_running_profile,
            get_weekly_running_load,
            get_intensity_distribution,
            get_efficiency_trend,
            get_fitness_form,
            analyze_run,
            find_comparable_runs,
            get_latest_workout,
            get_workouts_on_date,
            get_workouts,
        ],
    ),
    "strength": (
        _load_prompt("strength") + _PROMPT_SUFFIX,
        [get_strength_sessions, get_exercise_progress, get_strength_weekly_volume],
    ),
    "nutrition": (
        _load_prompt("nutrition") + _PROMPT_SUFFIX,
        [get_nutrition_summary, get_energy_balance, find_foods, get_nutrition_day, get_nutrition_range],
    ),
    "body": (
        _load_prompt("body") + _PROMPT_SUFFIX,
        [get_body_trend, get_body_composition_latest, get_body_composition_trend],
    ),
    "recovery": (
        _load_prompt("recovery") + _PROMPT_SUFFIX,
        [get_recovery_baseline, get_recovery_day, get_recovery_range, get_wellbeing_history, estimate_daily_expenditure],
    ),
}

ORCHESTRATOR_PROMPT = (
    "Jesteś routerem. NIGDY nie znasz odpowiedzi sam - nie masz żadnych "
    "danych użytkownika w pamięci, tylko dostęp do narzędzi `delegate`, "
    "`log_manual_entries` i `set_user_profile_facts`.\n\n"
    "ZASADA 1 (pytania): każde pytanie o dane (kroki, waga, sen, treningi, "
    "jedzenie, HRV, tętno) MUSI najpierw przejść przez `delegate`, zanim "
    "cokolwiek odpowiesz - nawet jeśli wydaje Ci się, że znasz odpowiedź. "
    "Jedyny wyjątek to czysty small talk bez pytania o dane (np. samo "
    "'cześć').\n\n"
    "ZASADA 2 (wpisy): jeśli użytkownik PODAJE jeden lub kilka pomiarów "
    "(nie pyta), użyj DOKŁADNIE RAZ `log_manual_entries` z listą WSZYSTKICH "
    "wpisów. Każdy element ma kind, payload i dokładny text_original. "
    "Przykłady kind/payload:\n"
    "- waga: {'kind':'weight','payload':{'weight_kg':82.1,'fat_pct':18}}\n"
    "- całodzienne spalone kalorie: "
    "{'kind':'daily_calories','payload':{'calories_total':2400}}\n"
    "- samopoczucie: "
    "{'kind':'wellbeing','payload':{'score':2,'note':'słaby sen'}}; "
    "score musi być całkowite 1–5, opcjonalna data YYYY-MM-DD\n"
    "- notatka: {'kind':'note','payload':{'note':'boli łydka'}}\n"
    "- trening siłowy: kind='strength', payload={'cwiczenia':[{'nazwa':"
    "'wyciskanie','serie':4,'powtorzenia':8,'ciezar_kg':80}]} — używaj "
    "kluczy bez polskich znaków; brak ciężaru = pomiń ciezar_kg; różne "
    "serie -> 'serie':[{'powtorzenia':5,'ciezar_kg':100}, ...].\n"
    "Przykład 'ważę 87 i spaliłem 2600' = dwa elementy w jednym wywołaniu. "
    "Zawsze kopiuj pełny oryginalny tekst do text_original każdego elementu. "
    "NIE deleguj wpisów do specjalistów, to nie pytanie.\n\n"
    "ZASADA 3 (profil): gdy użytkownik podaje fakt O SOBIE (nie pomiar z "
    "dziś): wiek, wzrost, płeć, cel wagi/kcal/białka, cel biegowy, staż, "
    "kontuzja, choroba/problem zdrowotny (refluks, alergia), preferencje "
    "żywieniowe, suplementy -> `set_user_profile_facts({klucz: wartość, ...})` "
    "- WSZYSTKIE fakty z wiadomości w JEDNYM wywołaniu (klucze w opisie "
    "narzędzia), potem jedno zdanie potwierdzenia. "
    "'Ważę 87' to pomiar (ZASADA 2), 'chcę ważyć 84' to cel (ZASADA 3). "
    "Jeśli w poprzedniej turze (kontekst rozmowy) specjalista zadał pytanie "
    "zaczynające się od '❓' albo Ty wysłałeś wywiad z listą pytań o profil, "
    "to obecna wiadomość użytkownika jest ODPOWIEDZIĄ - zapisz WSZYSTKIE "
    "fakty z niej ('brak'/'bez celu' też zapisuj, jako 'brak'), potem jedno "
    "zdanie potwierdzenia.\n\n"
    "ZASADA 5 (korekty wiedzy): po imporcie notatki użytkownik dostaje "
    "numerowaną listę faktów (#id). 'usuń 3 i 7' / 'punkt 4 to nieprawda' -> "
    "forget_knowledge([3, 7]); 'przywróć 12' / 'cofnij' -> restore_knowledge. "
    "'Jakie mam notatki' -> list_documents. Pytania o TREŚĆ notatek (co "
    "mówił poprzedni trener, jaką miałem kontuzję) -> delegate do "
    "właściwego specjalisty, on ma get_knowledge/read_document.\n\n"
    "Przykład pytania:\n"
    "user: Ile miałem wczoraj kroków?\n"
    "-> wywołaj delegate(agent_name='recovery', question='ile kroków wczoraj?')\n"
    "-> dopiero wynik tego narzędzia przekaż użytkownikowi\n\n"
    "Dostępni specjaliści:\n"
    "- running: WSZYSTKIE zarejestrowane treningi z zegarka/Intervals.icu "
    "(biegi, rowery, inne sporty cardio) - to domyślny wybór dla ogólnego "
    "słowa 'trening' bez dodatkowego kontekstu. Tu też idą pytania o "
    "kalorie SPALONE/wydatkowane na treningu (nie mylić z nutrition, które "
    "ma tylko kalorie ZJEDZONE). ORAZ każda DECYZJA/PLAN treningowy: 'czy "
    "mogę jutro zrobić interwały/długi bieg', 'co mam dziś pobiec', 'ułóż "
    "plan', 'czy jestem gotowy na mocny trening' - to running (ma formę "
    "CTL/ATL/TSB, obciążenie, strefy) i SAM dopyta recovery o sen/HRV; "
    "recovery bez kontekstu treningowego nie ułoży treningu.\n"
    "- strength: TYLKO treningi siłowe/na siłowni wpisane RĘCZNIE przez "
    "użytkownika w tym czacie - używaj tylko gdy pytanie wprost wspomina "
    "siłownię, ciężary, serie/powtórzenia.\n"
    "- nutrition (odżywianie), body (waga/skład ciała), recovery "
    "(sen/HRV/tętno spoczynkowe/kroki/CAŁODNIOWE spalone kalorie - w "
    "odróżnieniu od kalorii z konkretnego treningu, które ma running). "
    "Jeśli pytanie dotyczy kilku "
    "dziedzin naraz (np. 'dlaczego bieg wyszedł gorzej'), zdeleguj do "
    "najbardziej pasującego specjalisty - on sam dopyta pozostałych przez "
    "własne narzędzia. Pytanie o DEFICYT/BILANS kaloryczny -> nutrition "
    "(ma get_energy_balance liczący zjedzone minus wydatek). Odpowiadaj po "
    "polsku, zwięźle, w stylu wiadomości na czacie."
)


def _make_ask_agent_tool(caller_name: str):
    async def ask_agent(agent_name: str, question: str) -> AgentAnswer:
        """Zadaj pytanie innemu specjaliście i dostań krótkie podsumowanie.

        Dostępni: running, strength, nutrition, body, recovery (nie pytaj
        samego siebie).
        """
        if agent_name == caller_name:
            raise ValueError("Nie możesz zapytać samego siebie - to Ty jesteś tym agentem.")
        if agent_name not in SPECIALISTS:
            raise ValueError(f"Nieznany agent '{agent_name}'. Dostępni: {list(SPECIALISTS)}")
        leaf = build_leaf_agent(agent_name)
        text = await run_agent(leaf, agent_name, question)
        return AgentAnswer(summary=text)

    return ask_agent


def _prompt_with_memory(agent_name: str, base_prompt: str) -> str:
    """Prompt specjalisty + PROFIL użytkownika (fakty od niego: cele, wzrost,
    kontuzje - patrz tools/profile.py) + fakty zapamiętane przez tego agenta.
    Obie rzeczy są statyczne między pytaniami -> lądują w prompt cache."""
    return base_prompt + profile_prompt_block() + knowledge_digest(agent_name)


def _make_remember_tool(agent_name: str):
    def remember_fact(content: str, kind: str = "wniosek", event_date: str | None = None) -> str:
        """Zapamiętaj trwały fakt o użytkowniku na przyszłość (trafia do tabeli
        wiedzy i do Twojego digestu w kolejnych rozmowach).

        TYLKO: (a) fakt podany wprost przez użytkownika w tej rozmowie (cel,
        kontuzja, preferencja, życiówka) - kind: fakt/preferencja/zyciowka/
        decyzja, albo (b) wzorzec potwierdzony w >=3 niezależnych obserwacjach
        (np. '3 z 3 biegów po <6h snu miały dryf >10%') - kind: lekcja.
        NIGDY: korelacja z jednego treningu/dnia, bieżące liczby, ogólna
        wiedza trenerska. Jedna obserwacja to anegdota, nie fakt.
        `event_date` (YYYY-MM-DD) gdy fakt dotyczy konkretnej daty."""
        date = dt.date.fromisoformat(event_date) if event_date else None
        new_id = add_knowledge(agent_name, kind, content, event_date=date, source_type="agent", source_agent=agent_name)
        return f"zapamiętane (#{new_id})"

    return remember_fact


def build_leaf_agent(
    agent_name: str, *, system_suffix: str = "", allow_memory: bool = True
):
    """Specjalista BEZ narzędzia ask_agent - używany wewnątrz ask_agent, żeby
    fizycznie zablokować głębokość > 1.

    Zwraca zwykły tekst (nie wymuszony AgentAnswer jako JSON) - lokalne
    modele (Ollama) zawodnie łączą wymuszony structured output z
    tool-callingiem: złapane na żywo, że model widział iż powinien wywołać
    narzędzie (log "thinking"), ale zamiast tego wypisywał od razu
    placeholder pasujący do schematu JSON. `ask_agent` (wyżej) sam owija
    zwrócony tekst w AgentAnswer - nie wymaga tego od modelu."""
    prompt, tools = SPECIALISTS[agent_name]
    agent = build_agent(
        agent_name,
        _prompt_with_memory(agent_name, prompt) + system_suffix,
        [*tools, get_knowledge, read_document],
    )
    if allow_memory:
        agent.tool_plain(_make_remember_tool(agent_name))
    return agent


def build_full_agent(agent_name: str):
    """Specjalista Z narzędziem ask_agent - używany jako cel delegate() z
    orchestratora, czyli na pierwszym poziomie zagnieżdżenia."""
    prompt, tools = SPECIALISTS[agent_name]
    agent = build_agent(agent_name, _prompt_with_memory(agent_name, prompt), [*tools, get_knowledge, read_document])
    agent.tool_plain(_make_ask_agent_tool(agent_name))
    agent.tool_plain(_make_remember_tool(agent_name))
    return agent


async def _delegate(agent_name: str, question: str) -> str:
    """Deleguj pytanie do specjalisty: running, strength, nutrition, body, recovery."""
    if agent_name not in SPECIALISTS:
        raise ValueError(f"Nieznany agent '{agent_name}'. Dostępni: {list(SPECIALISTS)}")
    full = build_full_agent(agent_name)
    return await run_agent(full, agent_name, question)


def forget_knowledge(ids: list[int]) -> str:
    """Dezaktywuj wpisy wiedzy o podanych numerach (#id z listy po imporcie
    albo z get_knowledge) - gdy użytkownik mówi 'usuń 3 i 7', 'to
    nieprawda', 'zapomnij o kontuzji kolana'. Odwracalne (restore_knowledge)."""
    changed = set_knowledge_active(ids, False)
    return f"Usunięto z aktywnej wiedzy: {changed}" if changed else "Nic nie zmieniono (nieznane id albo już nieaktywne)."


def restore_knowledge(ids: list[int]) -> str:
    """Przywróć wcześniej usunięte/zastąpione wpisy wiedzy ('przywróć 12',
    'cofnij')."""
    changed = set_knowledge_active(ids, True)
    return f"Przywrócono: {changed}" if changed else "Nic nie zmieniono."


_GREETING_RE = re.compile(
    r"^\s*(cześć|czesc|hej|hejka|siema|elo|yo|hi|hello|dzień dobry|dzien dobry|witam|co tam|start|/start)\b[\s!.,?]*$",
    re.IGNORECASE,
)


def _onboarding_reply(question: str) -> str | None:
    """Wywiad o profil BEZ LLM: powitanie + niekompletny profil -> gotowa
    wiadomość z pytaniami. Wcześniej robił to orchestrator wg zasady w
    prompcie i (Haiku) doklejał wywiad do pytań o trening zamiast delegować -
    złapane na żywo. Deterministycznie nie ma jak się pomylić."""
    if _GREETING_RE.match(question) and missing_onboarding_keys():
        return "Cześć! Pomagam analizować bieganie, siłownię, dietę, wagę i regenerację z Twoich danych.\n\n" + onboarding_message()
    return None


def _orchestrator_prompt() -> str:
    missing = missing_onboarding_keys()
    status = "kompletny" if not missing else "niekompletny (brakuje: " + ", ".join(missing) + ") - NIE dopytuj o to sam i nie doklejaj listy pytań; specjaliści zapytają o to, czego akurat potrzebują, a użytkownik może użyć /profil"
    return ORCHESTRATOR_PROMPT + f"\n\nPROFIL UŻYTKOWNIKA: {status}."


def build_orchestrator():
    """`_delegate`/`log_manual_entries` są zarejestrowane jako OUTPUT FUNCTIONS
    (przez `output_type`), nie zwykłe narzędzia (`tool_plain`). Różnica:
    kiedy orchestrator wywoła jedną z nich, jej zwrócony string staje się OD
    RAZU finalną odpowiedzią (`result.output`) - bez dodatkowego wywołania
    modelu na przepisanie/sparafrazowanie wyniku. Zmierzone na żywo: to
    połowa kosztu i czasu orchestratora na pytanie (2 requesty -> 1), bo
    druga "parafraza" nie dodawała nic ponad to, co i tak już powiedział
    specjalista (albo deterministyczne potwierdzenie zapisu - patrz
    `_format_confirmation` w tools/manual.py). `str` zostaje jako trzecia
    opcja dla czystego small talk bez delegacji/zapisu."""
    # set_user_profile_facts to ZWYKŁE narzędzie (tool_plain), nie output
    # function: złapane na żywo, że przy dwóch wywołaniach output function w
    # jednej turze ("mam refluks i cel 84 kg") pydantic-ai wykonuje tylko
    # pierwsze - drugi fakt przepadał. Zwykłe narzędzie można wołać dowolnie
    # i łączyć z wpisem pomiaru w tej samej wiadomości; kosztuje jedną
    # dodatkową, tanią rundę Haiku na potwierdzenie - profil zmienia się rzadko.
    agent = build_agent(
        "orchestrator", _orchestrator_prompt(),
        [get_user_profile, set_user_profile_facts, forget_knowledge, restore_knowledge, list_documents],
        output_type=[str, _delegate, log_manual_entries],
    )
    return agent


def _with_history(question: str, history: list[tuple[str, str]] | None) -> str:
    """Dokleja kilka ostatnich wiadomości jako kontekst tekstowy.

    Bez tego każde pytanie trafiało do orchestratora jako oderwana,
    pojedyncza wiadomość - złapane na żywo: "Daj mi jeszcze szczegóły
    ostatniego treningu" po wcześniejszym pytaniu o kroki zostało
    zrozumiane kompletnie bez kontekstu. Prosty tekstowy prefiks zamiast
    natywnego message_history pydantic-ai - działa niezależnie od modelu
    (Ollama/Claude) i nie wymaga serializacji wewnętrznego formatu wiadomości.
    """
    if not history:
        return question
    lines = "\n".join(f"{role}: {content}" for role, content in history)
    return f"Poprzednia rozmowa (dla kontekstu):\n{lines}\n\nNowe pytanie: {question}"


def ask_orchestrator(question: str, history: list[tuple[str, str]] | None = None) -> str:
    """Wejście z zewnątrz gdy NIE jesteśmy jeszcze w żadnej pętli zdarzeń
    (CLI) - synchroniczne, patrz run_agent_sync."""
    if (reply := _onboarding_reply(question)) is not None:
        return reply
    orchestrator = build_orchestrator()
    return run_agent_sync(orchestrator, "orchestrator", _with_history(question, history))


async def ask_orchestrator_async(question: str, history: list[tuple[str, str]] | None = None) -> str:
    """Wejście z zewnątrz gdy JUŻ jesteśmy w pętli zdarzeń (np. handler
    Telegrama, sam będący `async def` w pętli python-telegram-bot) -
    wywołanie `ask_orchestrator` (sync) tutaj wywaliłoby się identycznie jak
    wcześniej złapany błąd z zagnieżdżonym `run_sync` - `asyncio.run()`
    też nie da się odpalić wewnątrz już działającej pętli."""
    if (reply := _onboarding_reply(question)) is not None:
        return reply
    orchestrator = build_orchestrator()
    return await run_agent(orchestrator, "orchestrator", _with_history(question, history))


async def ask_orchestrator_async_tracked(
    question: str, history: list[tuple[str, str]] | None = None
) -> tuple[str, int | None]:
    """Wariant dla kanałów, które zapisują odpowiedź razem z root run ID."""
    if (reply := _onboarding_reply(question)) is not None:
        return reply, None
    orchestrator = build_orchestrator()
    return await run_agent_with_id(orchestrator, "orchestrator", _with_history(question, history))
