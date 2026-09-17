"""Definicje specjalistów, orchestrator, mechanizm delegate/ask_agent.

Głębokość ask_agent jest ograniczona do 1 strukturalnie, nie licznikiem w
runtime: agent zbudowany do obsługi delegate() ("pełny") dostaje narzędzie
ask_agent, ale agent zbudowany WEWNĄTRZ ask_agent ("liściowy") już go nie
dostaje - więc fizycznie nie da się zejść głębiej niż jeden poziom.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from health_agent.agents.base import build_agent, run_agent, run_agent_sync
from health_agent.tools.body import get_body_composition_latest, get_body_composition_trend
from health_agent.tools.manual import get_recent_manual_logs, log_manual_entry
from health_agent.tools.memory import recall_all, remember
from health_agent.tools.nutrition import get_nutrition_day, get_nutrition_range
from health_agent.tools.recovery import get_recovery_day, get_recovery_range
from health_agent.tools.running import (
    analyze_run,
    find_comparable_runs,
    get_efficiency_trend,
    get_fitness_form,
    get_intensity_distribution,
    get_running_profile,
    get_weekly_running_load,
)
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
        "Jesteś trenerem siłowym (StrengthCoach). Analizujesz treningi siłowe "
        "zapisane ręcznie przez użytkownika: progresję, objętość." + _PROMPT_SUFFIX,
        [get_recent_manual_logs],
    ),
    "nutrition": (
        "Jesteś dietetykiem (NutritionCoach). Analizujesz WYŁĄCZNIE kalorie i "
        "makroskładniki ZJEDZONE (dziennik jedzenia z Fitatu) - NIE masz "
        "dostępu do kalorii SPALONYCH na treningu (to domena 'running')." + _PROMPT_SUFFIX,
        [get_nutrition_day, get_nutrition_range],
    ),
    "body": (
        "Jesteś analitykiem składu ciała (BodyCompCoach). Analizujesz wagę, "
        "tkankę tłuszczową, masę mięśniową - trendy 7/30-dniowe." + _PROMPT_SUFFIX,
        [get_body_composition_latest, get_body_composition_trend],
    ),
    "recovery": (
        "Jesteś analitykiem regeneracji (RecoveryAnalyst). Analizujesz sen, HRV, "
        "tętno spoczynkowe, kroki, CAŁODNIOWY wydatek kaloryczny (jeśli "
        "dostępny - pola calories_active/calories_total mogą być puste, "
        "zależnie od źródła; jeśli puste, powiedz wprost że tej danej nie "
        "ma, nie zgaduj) - jak organizm radzi sobie z obciążeniem. Do pytań "
        "o KONKRETNY dzień ('wczoraj', 'dziś', 'w poniedziałek' - przelicz "
        "na datę używając dzisiejszej daty wyżej) używaj get_recovery_day - "
        "NIGDY nie zgaduj liczby dni wstecz w get_recovery_range dla "
        "pytania o jeden dzień. get_recovery_range tylko do TRENDÓW "
        "(zmiana w czasie, średnie z tygodnia itp.)." + _PROMPT_SUFFIX,
        [get_recovery_day, get_recovery_range],
    ),
}

ORCHESTRATOR_PROMPT = (
    "Jesteś routerem. NIGDY nie znasz odpowiedzi sam - nie masz żadnych "
    "danych użytkownika w pamięci, tylko dostęp do narzędzi `delegate` i "
    "`log_manual_entry`.\n\n"
    "ZASADA 1 (pytania): każde pytanie o dane (kroki, waga, sen, treningi, "
    "jedzenie, HRV, tętno) MUSI najpierw przejść przez `delegate`, zanim "
    "cokolwiek odpowiesz - nawet jeśli wydaje Ci się, że znasz odpowiedź. "
    "Jedyny wyjątek to czysty small talk bez pytania o dane (np. samo "
    "'cześć').\n\n"
    "ZASADA 2 (wpisy): jeśli użytkownik PODAJE fakt do zapisania (nie "
    "pyta), użyj `log_manual_entry` zamiast `delegate`. Rozpoznaj to po "
    "formie - stwierdzenie, nie pytanie. Przykłady:\n"
    "- 'waga 82.1' albo 'ważę dziś 82.1 kg, 18% tłuszczu' -> "
    "log_manual_entry(kind='weight', payload={'weight_kg': 82.1, "
    "'fat_pct': 18})\n"
    "- 'spaliłem dziś 2400 kalorii' (z zegarka/apki, nie zgadywanie) -> "
    "log_manual_entry(kind='daily_calories', payload={'calories_total': "
    "2400})\n"
    "- 'dziś klata: wyciskanie 4x8 80kg' -> log_manual_entry(kind='strength', "
    "payload={'ćwiczenia': [...]}) - zapisz WSZYSTKIE podane szczegóły w "
    "payload, strukturyzując je sensownie.\n"
    "Zawsze ustaw `text_original` na dokładny, oryginalny tekst "
    "użytkownika. Po zapisie krótko potwierdź co zapisałeś - NIE deleguj "
    "wpisów do specjalistów, to nie pytanie.\n\n"
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
    "własne narzędzia. Odpowiadaj po polsku, zwięźle, w stylu wiadomości "
    "na czacie."
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
    facts = recall_all(agent_name)
    if not facts:
        return base_prompt
    facts_block = "\n".join(f"- {k}: {v}" for k, v in facts.items())
    return f"{base_prompt}\n\nFakty zapamiętane z poprzednich rozmów:\n{facts_block}"


def _make_remember_tool(agent_name: str):
    def remember_fact(key: str, value: str) -> str:
        """Zapamiętaj trwały fakt o użytkowniku na przyszłość.

        TYLKO: (a) fakt podany wprost przez użytkownika (cel, kontuzja,
        preferencja) albo (b) wzorzec potwierdzony w >=3 niezależnych
        obserwacjach (np. '3 z 3 biegów po <6h snu miały dryf >10%').
        NIGDY: korelacja z jednego treningu/dnia, bieżące liczby, ogólna
        wiedza trenerska. Jedna obserwacja to anegdota, nie fakt."""
        remember(agent_name, key, value)
        return "zapamiętane"

    return remember_fact


def build_leaf_agent(agent_name: str):
    """Specjalista BEZ narzędzia ask_agent - używany wewnątrz ask_agent, żeby
    fizycznie zablokować głębokość > 1.

    Zwraca zwykły tekst (nie wymuszony AgentAnswer jako JSON) - lokalne
    modele (Ollama) zawodnie łączą wymuszony structured output z
    tool-callingiem: złapane na żywo, że model widział iż powinien wywołać
    narzędzie (log "thinking"), ale zamiast tego wypisywał od razu
    placeholder pasujący do schematu JSON. `ask_agent` (wyżej) sam owija
    zwrócony tekst w AgentAnswer - nie wymaga tego od modelu."""
    prompt, tools = SPECIALISTS[agent_name]
    agent = build_agent(agent_name, _prompt_with_memory(agent_name, prompt), tools)
    agent.tool_plain(_make_remember_tool(agent_name))
    return agent


def build_full_agent(agent_name: str):
    """Specjalista Z narzędziem ask_agent - używany jako cel delegate() z
    orchestratora, czyli na pierwszym poziomie zagnieżdżenia."""
    prompt, tools = SPECIALISTS[agent_name]
    agent = build_agent(agent_name, _prompt_with_memory(agent_name, prompt), tools)
    agent.tool_plain(_make_ask_agent_tool(agent_name))
    agent.tool_plain(_make_remember_tool(agent_name))
    return agent


async def _delegate(agent_name: str, question: str) -> str:
    """Deleguj pytanie do specjalisty: running, strength, nutrition, body, recovery."""
    if agent_name not in SPECIALISTS:
        raise ValueError(f"Nieznany agent '{agent_name}'. Dostępni: {list(SPECIALISTS)}")
    full = build_full_agent(agent_name)
    return await run_agent(full, agent_name, question)


def build_orchestrator():
    """`_delegate`/`log_manual_entry` są zarejestrowane jako OUTPUT FUNCTIONS
    (przez `output_type`), nie zwykłe narzędzia (`tool_plain`). Różnica:
    kiedy orchestrator wywoła jedną z nich, jej zwrócony string staje się OD
    RAZU finalną odpowiedzią (`result.output`) - bez dodatkowego wywołania
    modelu na przepisanie/sparafrazowanie wyniku. Zmierzone na żywo: to
    połowa kosztu i czasu orchestratora na pytanie (2 requesty -> 1), bo
    druga "parafraza" nie dodawała nic ponad to, co i tak już powiedział
    specjalista (albo deterministyczne potwierdzenie zapisu - patrz
    `_format_confirmation` w tools/manual.py). `str` zostaje jako trzecia
    opcja dla czystego small talk bez delegacji/zapisu."""
    return build_agent("orchestrator", ORCHESTRATOR_PROMPT, [], output_type=[str, _delegate, log_manual_entry])


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
    orchestrator = build_orchestrator()
    return run_agent_sync(orchestrator, "orchestrator", _with_history(question, history))


async def ask_orchestrator_async(question: str, history: list[tuple[str, str]] | None = None) -> str:
    """Wejście z zewnątrz gdy JUŻ jesteśmy w pętli zdarzeń (np. handler
    Telegrama, sam będący `async def` w pętli python-telegram-bot) -
    wywołanie `ask_orchestrator` (sync) tutaj wywaliłoby się identycznie jak
    wcześniej złapany błąd z zagnieżdżonym `run_sync` - `asyncio.run()`
    też nie da się odpalić wewnątrz już działającej pętli."""
    orchestrator = build_orchestrator()
    return await run_agent(orchestrator, "orchestrator", _with_history(question, history))
