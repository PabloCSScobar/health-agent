"""
Smoke test: ten sam agent (pydantic-ai) z jednym narzędziem (tool),
uruchomiony raz na Claude API, raz na lokalnym modelu w Ollamie.

Cel: potwierdzić, że w przyszłej architekturze (config/agents.yaml -> model
per agent) możemy swobodnie przełączać model bez zmiany kodu agenta,
i że lokalny model w ogóle potrafi poprawnie wywołać narzędzie (tool calling).

Użycie:
    uv run scripts/test_llm_pydantic_ai.py claude
    uv run scripts/test_llm_pydantic_ai.py ollama
    uv run scripts/test_llm_pydantic_ai.py both   (domyślnie)

Wymaga:
    - dla "claude": zmienna środowiskowa ANTHROPIC_API_KEY (w .env lub w shellu)
    - dla "ollama": lokalnie odpalony `ollama serve` + model wskazany w OLLAMA_MODEL
      (domyślnie qwen2.5-coder:14b - dobrze radzi sobie z tool callingiem;
      qwen3:14b i bielik są też zainstalowane, można podmienić zmienną env)
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

from dotenv import load_dotenv
from pydantic import BaseModel
from pydantic_ai import Agent, RunContext

load_dotenv()

OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:14b")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "anthropic:claude-opus-5")

# UWAGA: qwen2.5-coder:14b w tej wersji Ollamy (0.32.6) NIE zwraca poprawnego
# structured tool_calls - model wypisuje wywołanie narzędzia jako zwykły JSON
# w treści odpowiedzi (błąd chat template modelu, potwierdzony bezpośrednio
# przez Ollama API, nie przez pydantic-ai). qwen3:14b działa poprawnie.
# Sprawdzaj to ponownie przy każdej zmianie modelu kandydującego do agentów.

SYSTEM_PROMPT = (
    "Jesteś asystentem treningowym. Gdy użytkownik pyta o ostatni trening, "
    "MUSISZ użyć narzędzia get_last_workout, żeby pobrać prawdziwe dane - "
    "nigdy nie zgaduj wartości. Odpowiadaj krótko, po polsku."
)


class Workout(BaseModel):
    """Fałszywe dane treningowe - w prawdziwej apce to będzie zapytanie do Postgresa."""

    sport: str
    date: str
    distance_km: float
    duration_min: int
    avg_hr: int


def build_agent(model) -> Agent:
    agent = Agent(model, system_prompt=SYSTEM_PROMPT)

    @agent.tool
    async def get_last_workout(ctx: RunContext, sport: str = "running") -> Workout:
        """Zwraca ostatni zarejestrowany trening danego sportu (dane testowe)."""
        return Workout(
            sport=sport,
            date=str(date.today() - timedelta(days=1)),
            distance_km=8.2,
            duration_min=44,
            avg_hr=158,
        )

    return agent


def run_case(label: str, model) -> None:
    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
    agent = build_agent(model)
    try:
        result = agent.run_sync("Jak wyglądał mój ostatni bieg?")
    except Exception as exc:  # noqa: BLE001 - to jest smoke test, chcemy zobaczyć każdy błąd
        print(f"BŁĄD: {type(exc).__name__}: {exc}")
        return

    print(f"Odpowiedź modelu:\n{result.output}\n")

    tool_calls = [
        part.tool_name
        for msg in result.all_messages()
        for part in getattr(msg, "parts", [])
        if getattr(part, "part_kind", "") == "tool-call"
    ]
    if "get_last_workout" in tool_calls:
        print("✅ Narzędzie get_last_workout zostało wywołane.")
    else:
        print("⚠️  Narzędzie NIE zostało wywołane - model prawdopodobnie zmyślił dane.")


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "both"

    if which in ("claude", "both"):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("⚠️  Brak ANTHROPIC_API_KEY w środowisku - pomijam test Claude.")
        else:
            run_case(f"Claude ({CLAUDE_MODEL})", CLAUDE_MODEL)

    if which in ("ollama", "both"):
        from pydantic_ai.models.ollama import OllamaModel
        from pydantic_ai.providers.ollama import OllamaProvider

        model = OllamaModel(
            OLLAMA_MODEL,
            provider=OllamaProvider(base_url=OLLAMA_BASE_URL),
        )
        run_case(f"Ollama ({OLLAMA_MODEL} @ {OLLAMA_BASE_URL})", model)


if __name__ == "__main__":
    main()
