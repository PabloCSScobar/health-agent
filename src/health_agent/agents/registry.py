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
from health_agent.tools.body import get_body_composition_latest, get_body_composition_trend, get_body_trend
from health_agent.tools.energy import estimate_daily_expenditure
from health_agent.tools.manual import log_manual_entry
from health_agent.tools.memory import recall_all, remember
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
from health_agent.tools.recovery import get_recovery_baseline, get_recovery_day, get_recovery_range
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
        [get_recovery_baseline, get_recovery_day, get_recovery_range, estimate_daily_expenditure],
    ),
}

ORCHESTRATOR_PROMPT = (
    "Jesteś routerem. NIGDY nie znasz odpowiedzi sam - nie masz żadnych "
    "danych użytkownika w pamięci, tylko dostęp do narzędzi `delegate`, "
    "`log_manual_entry` i `set_user_profile_facts`.\n\n"
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
    "- 'dziś klata: wyciskanie 4x8 80kg, dipy 3x12' -> log_manual_entry("
    "kind='strength', payload={'cwiczenia': [{'nazwa': 'wyciskanie sztangi', "
    "'serie': 4, 'powtorzenia': 8, 'ciezar_kg': 80}, {'nazwa': 'dipy', "
    "'serie': 3, 'powtorzenia': 12}]}) - DOKŁADNIE te klucze bez polskich "
    "znaków (cwiczenia/nazwa/serie/powtorzenia/ciezar_kg); brak ciężaru = "
    "pomiń ciezar_kg (masa ciała); różne serie -> 'serie': [{'powtorzenia': "
    "5, 'ciezar_kg': 100}, ...]; 'notatka' na resztę (RPE, uwagi).\n"
    "Zawsze ustaw `text_original` na dokładny, oryginalny tekst "
    "użytkownika. NIE deleguj wpisów do specjalistów, to nie pytanie.\n\n"
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
    "ZASADA 4 (wywiad): gdy PROFIL jest niekompletny (status niżej) i "
    "użytkownik zaczyna rozmowę small talkiem ('cześć', 'hej', 'co tam') "
    "albo pyta, co potrafisz - odpowiedz krótko i dołącz DOKŁADNIE treść "
    "z sekcji WYWIAD niżej (bez zmian). Nie dołączaj wywiadu do odpowiedzi "
    "na pytania o dane ani do potwierdzeń wpisów - specjaliści sami dopytają "
    "o to, co im potrzebne, jednym pytaniem na raz.\n\n"
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
    prompt = base_prompt + profile_prompt_block()
    facts = recall_all(agent_name)
    if facts:
        facts_block = "\n".join(f"- {k}: {v}" for k, v in facts.items())
        prompt += f"\n\nFakty zapamiętane przez Ciebie z poprzednich rozmów (Twoje wnioski, nie słowa użytkownika):\n{facts_block}"
    return prompt


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


def _orchestrator_prompt() -> str:
    missing = missing_onboarding_keys()
    status = "kompletny" if not missing else "niekompletny - brakuje: " + ", ".join(missing)
    prompt = ORCHESTRATOR_PROMPT + f"\n\nPROFIL UŻYTKOWNIKA: {status}."
    onboarding = onboarding_message()
    if onboarding:
        prompt += "\n\nWYWIAD (dołącz tylko wg ZASADY 4, dokładnie w tej formie):\n" + onboarding
    return prompt


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
    # set_user_profile_facts to ZWYKŁE narzędzie (tool_plain), nie output
    # function: złapane na żywo, że przy dwóch wywołaniach output function w
    # jednej turze ("mam refluks i cel 84 kg") pydantic-ai wykonuje tylko
    # pierwsze - drugi fakt przepadał. Zwykłe narzędzie można wołać dowolnie
    # i łączyć z wpisem pomiaru w tej samej wiadomości; kosztuje jedną
    # dodatkową, tanią rundę Haiku na potwierdzenie - profil zmienia się rzadko.
    agent = build_agent("orchestrator", _orchestrator_prompt(), [get_user_profile, set_user_profile_facts], output_type=[str, _delegate, log_manual_entry])
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
