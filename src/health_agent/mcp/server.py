"""Serwer MCP (streamable HTTP, bezstanowy) - warstwa danych dla agenta w OpenBot.

Udostępnia narzędzia z `health_agent.tools` pod tymi samymi nazwami, co dawniej
miały agenci LLM (bez routingu `delegate`/`ask_agent`). Obliczenia zostają w
`tools/`; ten moduł tylko opakowuje wywołania: wykonuje je w wątku, zamienia
wynik na JSON (daty, Decimal, modele pydantic) i błędy na polskie komunikaty.

Proces NIE startuje schedulera ani bota Telegrama. Uruchomienie:
    uvicorn health_agent.mcp.server:app --host 0.0.0.0 --port 8765 --workers 1

Autoryzacja: nagłówek `Authorization: Bearer <MCP_TOKEN>`; pusty MCP_TOKEN =
401 na każde żądanie. `GET /health` jest publiczny.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import decimal
import functools
import hmac
import inspect
import json
import logging
import re
import uuid
from enum import Enum
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from health_agent.settings import settings
from health_agent.tools.body import get_body_composition_latest, get_body_composition_trend, get_body_trend
from health_agent.tools.correlations import get_correlations
from health_agent.tools.energy import estimate_daily_expenditure
from health_agent.tools.knowledge import (
    add_knowledge,
    forget_knowledge,
    get_knowledge,
    list_documents,
    read_document,
    restore_knowledge,
    save_document as _save_document,
)
from health_agent.tools.manual_batch import log_manual_entries
from health_agent.tools.nutrition import (
    find_foods,
    get_energy_balance,
    get_nutrition_day,
    get_nutrition_range,
    get_nutrition_summary,
)
from health_agent.tools.overview import get_data_freshness
from health_agent.tools.profile import get_user_profile, set_user_profile_facts
from health_agent.tools.recovery import (
    get_recovery_baseline,
    get_recovery_day,
    get_recovery_range,
    get_wellbeing_history,
)
from health_agent.tools.reminders import (
    activate_reminder_rule as _activate_reminder_rule,
    add_supplement,
    list_reminder_rules,
    list_supplements,
    propose_reminder_rule as _propose_reminder_rule,
    record_supplement_intake as _record_supplement_intake,
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

logger = logging.getLogger("health_agent.mcp")

SERVER_NAME = "health"


def to_jsonable(value: Any) -> Any:
    """Zamień wynik narzędzia na strukturę serializowalną do JSON."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, (uuid.UUID, Enum)):
        return str(value.value if isinstance(value, Enum) else value)
    if isinstance(value, BaseModel):
        return to_jsonable(value.model_dump())
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in value]
    if hasattr(value, "_asdict"):
        return to_jsonable(value._asdict())
    return str(value)


async def run_tool(name: str, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Wykonaj funkcję z `tools/` w wątku; błędy zamień na komunikat po polsku.

    `name` i `fn` są tylko pozycyjne, bo narzędzia mają własne parametry o tych nazwach (np. add_supplement(name=...)).
    """
    try:
        result = await asyncio.to_thread(fn, *args, **kwargs)
        return to_jsonable(result)
    except ToolError:
        raise
    except (ValueError, LookupError) as exc:
        raise ToolError(str(exc) or type(exc).__name__) from exc
    except Exception as exc:
        logger.exception("Błąd narzędzia MCP %s", name)
        raise ToolError(f"wewnętrzny błąd serwera ({type(exc).__name__}). Szczegóły są w logu serwera.") from exc


def _wrap(fn: Callable[..., Any], name: str | None = None) -> Callable[..., Any]:
    """Opakuj funkcję z `tools/` zachowując nazwę, docstring i sygnaturę argumentów."""
    tool_name = name or fn.__name__

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return await run_tool(tool_name, fn, *args, **kwargs)

    wrapper.__name__ = tool_name
    wrapper.__signature__ = inspect.signature(fn, eval_str=True).replace(return_annotation=Any)  # type: ignore[attr-defined]
    return wrapper


# Narzędzia tylko do odczytu: nazwa -> funkcja z tools/.
READ_TOOLS: dict[str, Callable[..., Any]] = {
    fn.__name__: fn
    for fn in (
        get_running_profile, get_weekly_running_load, get_intensity_distribution, get_efficiency_trend,
        get_fitness_form, analyze_run, find_comparable_runs, get_latest_workout, get_workouts_on_date,
        get_workouts, get_correlations,
        get_strength_sessions, get_exercise_progress, get_strength_weekly_volume,
        get_nutrition_summary, get_energy_balance, find_foods, get_nutrition_day, get_nutrition_range,
        list_supplements, list_reminder_rules,
        get_body_trend, get_body_composition_latest, get_body_composition_trend,
        get_recovery_baseline, get_recovery_day, get_recovery_range, get_wellbeing_history,
        estimate_daily_expenditure,
        get_knowledge, list_documents, read_document, get_user_profile, get_data_freshness,
    )
}

# Narzędzia zapisujące dane.
WRITE_TOOLS: dict[str, Callable[..., Any]] = {
    fn.__name__: fn
    for fn in (
        log_manual_entries, set_user_profile_facts, forget_knowledge, restore_knowledge, add_supplement,
    )
}

_DRAFT_MARKER_RE = re.compile(r"\s*\[REMINDER_DRAFT:\d+\]\s*")
_DRAFT_ID_RE = re.compile(r"\[REMINDER_DRAFT:(\d+)\]")


def remember_fact(
    content: str, domain: str = "general", kind: str = "wniosek", event_date: dt.date | None = None
) -> str:
    """Zapamiętaj trwały fakt o użytkowniku na przyszłość (tabela wiedzy).

    TYLKO: (a) fakt podany wprost przez użytkownika w tej rozmowie (cel,
    kontuzja, preferencja, życiówka) - kind: fakt/preferencja/zyciowka/
    decyzja, albo (b) wzorzec potwierdzony w >=3 niezależnych obserwacjach
    (np. '3 z 3 biegów po <6h snu miały dryf >10%') - kind: lekcja.
    NIGDY: korelacja z jednego treningu/dnia, bieżące liczby, ogólna
    wiedza trenerska. Jedna obserwacja to anegdota, nie fakt.
    `domain`: running | strength | nutrition | body | recovery | general.
    `event_date` (YYYY-MM-DD) gdy fakt dotyczy konkretnej daty."""
    new_id = add_knowledge(domain, kind, content, event_date=event_date, source_type="agent", source_agent="openbot")
    return f"zapamiętane (#{new_id})"


def propose_reminder_rule(
    title: str,
    kind: str = "text",
    local_time: str = "20:00",
    condition_type: str | None = None,
    condition_threshold: float | None = None,
    condition_window_hours: int | None = None,
    supplement_id: int | None = None,
) -> dict:
    """Utwórz WYŁĄCZNIE szkic reguły przypomnienia (status draft; nie wysyła
    niczego). Pokaż szkic użytkownikowi i dopiero po jego jawnym potwierdzeniu
    wywołaj activate_reminder_rule.

    `kind`: supplement | run | workout | text. Dostępne warunki
    (`condition_type`): steps_below, protein_below, no_run, no_workout. Dla
    kroków i białka podaj jawny próg użytkownika, nie wymyślaj go. Domyślne
    "wieczorem" oznacza 20:00 Europe/Warsaw. `condition_window_hours` 1-168."""
    text = _propose_reminder_rule(
        title, kind, local_time, condition_type, condition_threshold, condition_window_hours, supplement_id
    )
    match = _DRAFT_ID_RE.search(text)
    cleaned = _DRAFT_MARKER_RE.sub("", text).replace("Potwierdź ją przyciskiem.", "").strip()
    return {"rule_id": int(match.group(1)) if match else None, "status": "draft", "opis": cleaned}


def activate_reminder_rule(rule_id: int) -> dict:
    """Aktywuj szkic przypomnienia (od tej chwili będzie wysyłane na Telegram).
    Wywołuj WYŁĄCZNIE po tym, jak użytkownik jawnie potwierdził szkic
    utworzony przez propose_reminder_rule."""
    return _activate_reminder_rule(rule_id)


def record_supplement_intake(
    supplement_id: int, status: str, scheduled_for: dt.datetime | None = None, note: str | None = None
) -> dict:
    """Zapisz przyjęcie albo pominięcie suplementu. `status`: taken | skipped.
    `scheduled_for` (opcjonalnie, data i godzina ISO z strefą) wiąże wpis z
    zaplanowaną porą - ponowny zapis tej samej pory aktualizuje wpis."""
    intake_id = _record_supplement_intake(supplement_id, status, scheduled_for=scheduled_for, source="openbot", note=note)
    return {"intake_id": intake_id, "supplement_id": supplement_id, "status": status}


def save_document(text: str, title: str | None = None, doc_date: dt.date | None = None) -> dict:
    """Zapisz ORYGINAŁ notatki (min. 40 znaków) w dokumentach, bez analizy
    LLM i bez wyciągania faktów. Ten sam tekst nie zapisze się drugi raz
    (zwraca wtedy istniejące id i deduplicated=true). Fakty z notatki zapisz
    osobno przez remember_fact / set_user_profile_facts; oryginał wraca przez
    read_document(id). `doc_date` (YYYY-MM-DD) = data, której dotyczy treść."""
    return _save_document(text, title=title, doc_date=doc_date, source="openbot")


CUSTOM_WRITE_TOOLS: dict[str, Callable[..., Any]] = {
    fn.__name__: fn
    for fn in (remember_fact, propose_reminder_rule, activate_reminder_rule, record_supplement_intake, save_document)
}

ALL_TOOLS: dict[str, Callable[..., Any]] = {**READ_TOOLS, **WRITE_TOOLS, **CUSTOM_WRITE_TOOLS}


# Opisy, które w `tools/` są napisane pod orchestratora (a nie pod klienta MCP).
DESCRIPTION_OVERRIDES: dict[str, str] = {
    "log_manual_entries": (
        "Zapisz JEDNYM wywołaniem wszystkie pomiary podane przez użytkownika (nie pytania). "
        "Każdy element ma kind, payload i dokładny, pełny text_original (oryginalna wypowiedź). "
        "Przykłady kind/payload: "
        "waga: {'kind':'weight','payload':{'weight_kg':82.1,'fat_pct':18}}; "
        "całodzienne spalone kalorie: {'kind':'daily_calories','payload':{'calories_total':2400}}; "
        "samopoczucie: {'kind':'wellbeing','payload':{'score':2,'note':'słaby sen'}} "
        "(score całkowite 1-5, opcjonalna data YYYY-MM-DD w payload); "
        "notatka: {'kind':'note','payload':{'note':'boli łydka'}}; "
        "trening siłowy: {'kind':'strength','payload':{'cwiczenia':[{'nazwa':'wyciskanie','serie':4,"
        "'powtorzenia':8,'ciezar_kg':80}]}} - klucze bez polskich znaków, brak ciężaru = pomiń ciezar_kg, "
        "różne serie -> 'serie':[{'powtorzenia':5,'ciezar_kg':100}, ...]. "
        "'Ważę 87 i spaliłem 2600' to dwa elementy w jednym wywołaniu. "
        "Pomiar z innego dnia niż dziś (np. historia wag, zaległy trening): dodaj 'date':'YYYY-MM-DD' do payload "
        "(działa dla weight, strength, daily_calories, wellbeing; data nie może być z przyszłości). "
        "Zwraca potwierdzenie zapisu."
    ),
}


def build_mcp() -> MCPServer:
    server = MCPServer(SERVER_NAME)
    for name, fn in ALL_TOOLS.items():
        server.add_tool(
            _wrap(fn, name), name=name, description=DESCRIPTION_OVERRIDES.get(name), structured_output=False
        )
    return server


def allowed_hosts() -> list[str]:
    return [item.strip() for item in settings.mcp_allowed_hosts.split(",") if item.strip()]


def _transport_security() -> TransportSecuritySettings:
    hosts = allowed_hosts()
    origins = [f"{scheme}://{host}" for host in hosts for scheme in ("https", "http")]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )


def _bearer_ok(authorization: str | None, expected: str) -> bool:
    if not expected or not authorization:
        return False
    scheme, _, supplied = authorization.partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        return False
    return hmac.compare_digest(supplied.strip().encode("utf-8"), expected.encode("utf-8"))


class BearerAuthMiddleware:
    """Czysty ASGI: wszystko poza GET /health wymaga poprawnego tokenu Bearer."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/health":
            await self.app(scope, receive, send)
            return
        expected = settings.mcp_token
        if not expected:
            logger.warning("MCP_TOKEN jest pusty - odrzucam żądanie (ustaw MCP_TOKEN w .env)")
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if not _bearer_ok(headers.get("authorization"), expected):
            response = JSONResponse(
                {"error": "Brak lub nieprawidłowy token."}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def _health(request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def build_app():
    mcp_app = build_mcp().streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security(),
    )
    mcp_app.add_route("/health", _health, methods=["GET"])
    mcp_app.add_middleware(BearerAuthMiddleware)
    return mcp_app


app = build_app()
