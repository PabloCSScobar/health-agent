"""Fabryka agentów: wybór modelu z config/agents.yaml + rejestrowanie kosztu
w tabeli agent_runs po każdym uruchomieniu, z drzewem parent_run_id.

Drzewo działa przez contextvar: kiedy agent A woła narzędziem agenta B
(delegate/ask_agent), B startuje "w środku" wywołania A - `run_agent`
wstawia wiersz agent_runs OD RAZU (żeby zagnieżdżone wywołanie mogło się do
niego odwołać jako do rodzica), a dopiero po zakończeniu dopisuje
usage/koszt/czas.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic_ai import Agent

from health_agent.db.models import AgentRun
from health_agent.db.session import get_session
from health_agent.settings import settings
from health_agent.time_utils import local_today

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "agents.yaml"

_current_run_id: ContextVar[int | None] = ContextVar("current_run_id", default=None)


def _load_agent_models() -> dict[str, str]:
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


_AGENT_MODELS = _load_agent_models()


def _build_anthropic_model(model_ref: str) -> Any:
    """Buduje AnthropicModel z jawnie podanym kluczem z Settings (z .env),
    zamiast pozwolić pydantic-ai wywnioskować model z samego stringa.

    Złapane na żywo: przekazanie samego stringa "anthropic:claude-opus-5" do
    Agent() każe pydantic-ai stworzyć domyślny AnthropicProvider(), który
    czyta klucz WYŁĄCZNIE z prawdziwej zmiennej środowiskowej os.environ -
    pydantic-settings ładuje .env tylko do obiektu Settings, NIE eksportuje
    go do os.environ, więc mimo poprawnie ustawionego ANTHROPIC_API_KEY w
    .env, wywołanie kończyło się UserError "Set the ANTHROPIC_API_KEY
    environment variable...". Jawne AnthropicProvider(api_key=...) omija ten
    problem."""
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    model_name = model_ref.removeprefix("anthropic:")
    return AnthropicModel(model_name, provider=AnthropicProvider(api_key=settings.anthropic_api_key))


def resolve_model(agent_name: str) -> Any:
    spec = _AGENT_MODELS.get(agent_name, "ollama")
    if spec == "claude":
        return _build_anthropic_model(settings.claude_model)
    if spec == "ollama":
        from pydantic_ai.models.ollama import OllamaModel
        from pydantic_ai.providers.ollama import OllamaProvider

        return OllamaModel(settings.ollama_model, provider=OllamaProvider(base_url=settings.ollama_base_url))
    if spec.startswith("anthropic:"):
        return _build_anthropic_model(spec)
    return spec  # inny literalny string modelu (nie-anthropic)


def model_display_name(agent_name: str) -> str:
    """Czytelna nazwa modelu do zapisu w agent_runs - "ollama"/"claude" z
    configu to tylko etykieta wyboru, nie konkretny model."""
    spec = _AGENT_MODELS.get(agent_name, "ollama")
    if spec == "claude":
        return settings.claude_model
    if spec == "ollama":
        return f"ollama:{settings.ollama_model}"
    return spec


def build_agent(agent_name: str, system_prompt: str, tools: list[Callable] | None = None, output_type: Any = str, retries: int = 1) -> Agent:
    model = resolve_model(agent_name)

    # Model nie wie jaki jest "dziś" - złapane na żywo: realną datę treningu
    # (2026-09-15) model ocenił jako "datę z przyszłości" i odmówił podania
    # danych, bo jego wewnętrzne założenie "dzisiaj" jest wcześniejsze niż
    # dane treningowe z tego roku. Jawne podanie dzisiejszej daty w promptcie
    # to standardowa poprawka na dokładnie ten problem.
    system_prompt = f"Dzisiejsza data: {local_today().isoformat()}.\n\n{system_prompt}"

    # Lokalne modele przez Ollamę zawodnie generują poprawny JSON przy
    # zwykłym structured output pydantic-ai (złapane na żywo: qwen3:14b w
    # zagnieżdżonym wywołaniu ask_agent zwrócił tekst zamiast JSON-a).
    # NativeOutput każe modelowi trzymać się schematu przez natywny
    # mechanizm Ollamy (format=json schema), zamiast liczyć na to, że model
    # sam się dobrze sformatuje - dużo bardziej niezawodne dla mniejszych
    # modeli. Dla Claude zostawiamy domyślny mechanizm (tool-based), który
    # tam jest już wystarczająco solidny.
    # Tylko dla pojedynczego typu strukturalnego (np. output_type=AgentAnswer)
    # - NIE dla list output functions (output_type=[str, _delegate, ...],
    # patrz build_orchestrator) - NativeOutput opakowujący całą listę nie ma
    # sensu, to mechanizm dla jednego schematu JSON, nie dla wyboru funkcji.
    # Nieużywane dziś (orchestrator jest na Anthropic), ale zabezpiecza
    # przyszły powrót do Ollama przed cichym błędem.
    spec = _AGENT_MODELS.get(agent_name, "ollama")
    if spec == "ollama" and isinstance(output_type, type) and output_type is not str:
        from pydantic_ai.output import NativeOutput

        output_type = NativeOutput(output_type)

    model_settings = None
    if spec == "claude" or (isinstance(spec, str) and spec.startswith("anthropic:")):
        # Prompt caching: system prompt (data + reguły + pamięć agenta) i
        # definicje narzędzi są IDENTYCZNE między kolejnymi requestami w
        # ramach jednego `agent.run()` (np. decyzja o wywołaniu narzędzia ->
        # odpowiedź po wyniku narzędzia to już 2 requesty z tym samym
        # prefiksem) i między kolejnymi pytaniami tego samego dnia (system
        # prompt zmienia się tylko przy zmianie daty/pamięci agenta).
        # Cache'owany odczyt kosztuje ~10% ceny inputu - przy ~85% tokenów
        # wejścia będących stałym promptem/narzędziami to realna oszczędność,
        # nie tylko teoretyczna (zmierz `cache_read_tokens` w agent_runs po
        # włączeniu, żeby to zweryfikować, nie zakładać).
        from pydantic_ai.models.anthropic import AnthropicModelSettings

        # `anthropic_cache=True` dodatkowo przesuwa automatyczny punkt cache na
        # ostatni blok wiadomości: w agentach z kilkoma rundami narzędzi
        # (running: profil -> analiza -> ask_agent -> odpowiedź) każda kolejna
        # runda czyta wyniki poprzednich z cache zamiast płacić pełną cenę.
        model_settings = AnthropicModelSettings(
            anthropic_cache_instructions=True,
            anthropic_cache_tool_definitions=True,
            anthropic_cache=True,
        )

    agent = Agent(model, system_prompt=system_prompt, output_type=output_type, model_settings=model_settings, retries=retries)
    for tool_fn in tools or []:
        agent.tool_plain(tool_fn)
    return agent


async def run_agent_with_id(agent: Agent, agent_name: str, prompt: str) -> tuple[Any, int]:
    """Uruchamia agenta (async - patrz niżej dlaczego), zapisuje koszt/czas/
    drzewo do agent_runs.

    Musi być async, nie `run_sync()`: gdy ten sam wywoływany jest WEWNĄTRZ
    narzędzia innego, już działającego agenta (delegate/ask_agent), jesteśmy
    już w pętli zdarzeń tamtego biegu - `run_sync()` próbowałby odpalić
    drugą pętlę i pydantic-ai świadomie to blokuje (realny błąd złapany przy
    pierwszym teście: "cannot be used inside a synchronous tool... can
    deadlock the run"). Wejście z zewnątrz (CLI/Telegram) używa
    `run_agent_sync` niżej.

    Zwraca wynik oraz id biegu. Zwykli wywołujący korzystają z `run_agent`,
    który zachowuje dotychczasowy kontrakt i zwraca tylko wynik.
    """
    parent_run_id = _current_run_id.get()

    with get_session() as session:
        run = AgentRun(agent=agent_name, model=model_display_name(agent_name), parent_run_id=parent_run_id)
        session.add(run)
        session.flush()
        run_id = run.id

    token = _current_run_id.set(run_id)
    try:
        start = time.monotonic()
        result = await agent.run(prompt)
        duration_ms = int((time.monotonic() - start) * 1000)
    finally:
        _current_run_id.reset(token)

    usage = result.usage
    tools_called = [
        part.tool_name
        for message in result.all_messages()
        for part in getattr(message, "parts", [])
        if type(part).__name__ == "ToolCallPart"
    ]
    with get_session() as session:
        row = session.get(AgentRun, run_id)
        row.input_tokens = usage.input_tokens
        row.output_tokens = usage.output_tokens
        row.cost_usd = float(usage.cost) if usage.cost is not None else None
        row.duration_ms = duration_ms
        row.tools_called = tools_called

    return result.output, run_id


async def run_agent(agent: Agent, agent_name: str, prompt: str) -> Any:
    """Uruchom agenta i zwróć tylko wynik, zachowując dotychczasowe API."""
    output, _ = await run_agent_with_id(agent, agent_name, prompt)
    return output


@asynccontextmanager
async def agent_run_group(agent_name: str, child_agents: list[str]):
    """Utwórz syntetyczny korzeń dla kilku równoległych wywołań agentów."""
    parent_run_id = _current_run_id.get()
    with get_session() as session:
        run = AgentRun(agent=agent_name, model="parallel-specialists", parent_run_id=parent_run_id)
        session.add(run)
        session.flush()
        run_id = run.id
    token = _current_run_id.set(run_id)
    started = time.monotonic()
    try:
        yield run_id
    finally:
        _current_run_id.reset(token)
        with get_session() as session:
            row = session.get(AgentRun, run_id)
            row.duration_ms = int((time.monotonic() - started) * 1000)
            row.tools_called = child_agents


def run_agent_sync(agent: Agent, agent_name: str, prompt: str) -> Any:
    """Wejście z zewnątrz (CLI, Telegram) - tu NIE jesteśmy jeszcze w żadnej
    pętli zdarzeń pydantic-ai, więc `asyncio.run` jest bezpieczne."""
    return asyncio.run(run_agent(agent, agent_name, prompt))
