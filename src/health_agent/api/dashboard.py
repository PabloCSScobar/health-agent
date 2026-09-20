"""Uwierzytelniony, jednoużytkownikowy dashboard bez zewnętrznego frontendu."""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from collections import defaultdict, deque
from collections.abc import Callable
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from health_agent.db.models import DashboardSession
from health_agent.db.session import get_session
from health_agent.settings import settings
from health_agent.time_utils import app_timezone, local_today
from health_agent.tools.correlations import get_correlations
from health_agent.tools.overview import overview_payload
from health_agent.tools.photos import (
    delete_progress_photo,
    list_progress_photos,
    progress_photo_path,
    save_progress_photo,
)
from health_agent.tools.proactive_alerts import list_alert_settings, update_alert_setting
from health_agent.tools.reminders import (
    activate_reminder_rule,
    add_supplement,
    list_reminder_occurrences,
    list_reminder_rules,
    list_supplements,
    list_unknown_notifications,
    propose_reminder_rule,
    record_supplement_intake,
    retry_unknown_notification,
    update_reminder_rule,
    update_supplement,
)

COOKIE_NAME = "health_agent_session"
_PASSWORD_HASHER = PasswordHasher()
_login_attempts: dict[str, deque[dt.datetime]] = defaultdict(deque)

# Frontend jest zestawem plików statycznych w pakiecie; serwuje je wyłącznie
# router /dash (tryb dostępu i allowlista IP obowiązują także dla CSS/JS).
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_ASSETS = {
    "dashboard.css": "text/css; charset=utf-8",
    "dashboard.js": "text/javascript; charset=utf-8",
    "login.js": "text/javascript; charset=utf-8",
}


def _asset_version() -> str:
    digest = hashlib.sha256()
    for name in sorted(STATIC_ASSETS):
        digest.update((STATIC_DIR / name).read_bytes())
    return digest.hexdigest()[:12]


ASSET_VERSION = _asset_version()


def _render_page(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8").replace(
        "__ASSET_VERSION__", ASSET_VERSION
    )


LOGIN_HTML = _render_page("login.html")
DASHBOARD_HTML = _render_page("dashboard.html")


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class SupplementBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    dose: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)


class SupplementUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    dose: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)
    active: bool | None = None


class IntakeBody(BaseModel):
    status: str
    note: str | None = Field(default=None, max_length=1000)


class ReminderBody(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    kind: str = "text"
    local_time: str = "20:00"
    condition_type: str | None = None
    condition_threshold: float | None = None
    condition_window_hours: int | None = None
    supplement_id: int | None = None


class ProactiveAlertUpdate(BaseModel):
    enabled: bool


class ReminderUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=256)
    local_time: str | None = None
    timezone: str | None = None
    schedule_json: dict | None = None
    condition_type: str | None = None
    condition_threshold: float | None = None
    condition_window_hours: int | None = None
    payload_json: dict | None = None
    status: str | None = None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


IPAddress = IPv4Address | IPv6Address
IPNetwork = IPv4Network | IPv6Network


def _configured_networks(value: str) -> tuple[IPNetwork, ...]:
    return tuple(
        ip_network(item.strip(), strict=False)
        for item in value.split(",")
        if item.strip()
    )


def _request_ip(request: Request) -> IPAddress | None:
    if request.client is None:
        return None
    try:
        peer = ip_address(request.client.host)
    except ValueError:
        return None
    trusted = _configured_networks(settings.dashboard_trusted_proxies)
    current = peer
    forwarded = [
        item
        for header in request.headers.getlist("x-forwarded-for")
        for item in header.split(",")
    ]
    for item in reversed(forwarded):
        if not any(current in network for network in trusted):
            break
        item = item.strip()
        if item:
            try:
                current = ip_address(item)
            except ValueError:
                return None
    return current


def _enforce_dashboard_access(request: Request) -> None:
    if settings.dashboard_access_mode == "disabled":
        raise HTTPException(status_code=404, detail="Dashboard jest wyłączony")
    allowed = _configured_networks(settings.dashboard_allowed_ips)
    if not allowed:
        return
    client_ip = _request_ip(request)
    if client_ip is None or not any(client_ip in network for network in allowed):
        raise HTTPException(status_code=403, detail="Adres IP nie ma dostępu do dashboardu")


router = APIRouter(prefix="/dash", dependencies=[Depends(_enforce_dashboard_access)])


def _current_session(request: Request) -> DashboardSession:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Zaloguj się")
    now = dt.datetime.now(dt.timezone.utc)
    authenticated = None
    with get_session() as session:
        row = session.execute(
            select(DashboardSession).where(
                DashboardSession.token_hash == _token_hash(token),
                DashboardSession.revoked.is_(False),
            )
        ).scalar_one_or_none()
        if (
            row is None
            or row.expires_at <= now
            or row.last_seen_at
            < now - dt.timedelta(hours=settings.dashboard_session_idle_hours)
        ):
            if row is not None:
                row.revoked = True
        else:
            row.last_seen_at = now
            session.flush()
            session.expunge(row)
            authenticated = row
    if authenticated is None:
        # Wyjątek dopiero po wyjściu z get_session: ewentualne revoked=True
        # musi zostać zatwierdzone, a nie wycofane przez rollback context managera.
        raise HTTPException(status_code=401, detail="Sesja wygasła")
    return authenticated


def _validate_mutation(request: Request, auth: DashboardSession) -> None:
    if request.headers.get("x-csrf-token") != auth.csrf_token:
        raise HTTPException(status_code=403, detail="Nieprawidłowy token CSRF")
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if not origin or not host or urlsplit(origin).netloc != host:
        raise HTTPException(status_code=403, detail="Nieprawidłowe źródło żądania")


def _service_call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return function(*args, **kwargs)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _client_key(request: Request) -> str:
    client_ip = _request_ip(request)
    return str(client_ip) if client_ip is not None else "unknown"


def _check_login_rate(request: Request) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    attempts = _login_attempts[_client_key(request)]
    cutoff = now - dt.timedelta(minutes=15)
    while attempts and attempts[0] < cutoff:
        attempts.popleft()
    if len(attempts) >= 5:
        raise HTTPException(status_code=429, detail="Za dużo prób. Spróbuj za 15 minut.")
    attempts.append(now)


_PAGE_HEADERS = {"Cache-Control": "private, no-store"}


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    try:
        _current_session(request)
    except HTTPException:
        return HTMLResponse(LOGIN_HTML, headers=_PAGE_HEADERS)
    return HTMLResponse(DASHBOARD_HTML, headers=_PAGE_HEADERS)


@router.get("/static/{asset}")
def static_asset(asset: str) -> FileResponse:
    media_type = STATIC_ASSETS.get(asset)
    if media_type is None:
        raise HTTPException(status_code=404, detail="Brak zasobu")
    return FileResponse(
        STATIC_DIR / asset,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/api/session")
def session_info(auth: DashboardSession = Depends(_current_session)) -> dict:
    return {
        "csrf": auth.csrf_token,
        "today": local_today().isoformat(),
        "timezone": str(app_timezone()),
        "access_mode": settings.dashboard_access_mode,
        "expires_at": auth.expires_at.isoformat(),
    }


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response) -> dict:
    _check_login_rate(request)
    if not settings.dashboard_password_hash:
        raise HTTPException(status_code=503, detail="Dashboard nie ma skonfigurowanego hasła")
    try:
        _PASSWORD_HASHER.verify(settings.dashboard_password_hash, body.password)
    except (VerifyMismatchError, InvalidHashError):
        raise HTTPException(status_code=401, detail="Nieprawidłowe hasło") from None
    token = secrets.token_urlsafe(32)
    now = dt.datetime.now(dt.timezone.utc)
    csrf = secrets.token_urlsafe(24)
    with get_session() as session:
        session.add(
            DashboardSession(
                token_hash=_token_hash(token),
                csrf_token=csrf,
                created_at=now,
                last_seen_at=now,
                expires_at=now + dt.timedelta(days=settings.dashboard_session_absolute_days),
                revoked=False,
            )
        )
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.dashboard_session_absolute_days * 86400,
        secure=settings.dashboard_cookie_secure,
        httponly=True,
        samesite="lax",
        path="/dash",
    )
    _login_attempts.pop(_client_key(request), None)
    return {"status": "ok"}


@router.get("/api/overview")
def overview(days: int = 30, auth: DashboardSession = Depends(_current_session)) -> dict:
    payload = overview_payload(days)
    # `latest` zostaje dla zgodności z dotychczasowymi klientami/testami.
    metrics = {metric["key"]: metric["latest"] for metric in payload["metrics"]}
    payload["latest"] = {
        "Sen": metrics["sleep_h"],
        "HRV": metrics["hrv"],
        "Kroki": metrics["steps"],
        "Kilometry biegu": metrics["run_km"],
        "Waga": metrics["weight_kg"],
        "Białko": metrics["protein_g"],
    }
    return {"csrf": auth.csrf_token, **payload}


@router.get("/api/correlations")
def correlations(auth: DashboardSession = Depends(_current_session)) -> list[dict]:
    return get_correlations()


@router.get("/api/photos")
def photos(
    view: str | None = None, auth: DashboardSession = Depends(_current_session)
) -> list[dict]:
    return _service_call(list_progress_photos, view=view or None)


@router.post("/api/photos")
async def upload_photo(
    request: Request, auth: DashboardSession = Depends(_current_session)
) -> dict:
    _validate_mutation(request, auth)
    try:
        captured_date = dt.date.fromisoformat(request.headers.get("x-photo-date", ""))
    except ValueError:
        raise HTTPException(status_code=422, detail="Podaj datę zdjęcia") from None
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > settings.progress_photo_max_bytes:
            raise HTTPException(status_code=413, detail="Zdjęcie przekracza limit 15 MB")
    try:
        return save_progress_photo(
            bytes(chunks),
            captured_date=captured_date,
            view=request.headers.get("x-photo-view", "other"),
            note=unquote(request.headers.get("x-photo-note", "")) or None,
            source="dashboard",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/photos/{photo_id}/file")
def photo_file(
    photo_id: int, auth: DashboardSession = Depends(_current_session)
) -> FileResponse:
    found = progress_photo_path(photo_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Brak zdjęcia")
    path, media_type = found
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, no-store"})


@router.delete("/api/photos/{photo_id}")
def remove_photo(
    photo_id: int, request: Request, auth: DashboardSession = Depends(_current_session)
) -> dict:
    _validate_mutation(request, auth)
    if not delete_progress_photo(photo_id):
        raise HTTPException(status_code=404, detail="Brak zdjęcia")
    return {"status": "deleted"}


@router.get("/api/supplements")
def supplements(auth: DashboardSession = Depends(_current_session)) -> list[dict]:
    return list_supplements(include_inactive=True)


@router.post("/api/supplements")
def create_supplement(
    body: SupplementBody, request: Request,
    auth: DashboardSession = Depends(_current_session),
) -> dict:
    _validate_mutation(request, auth)
    message = _service_call(add_supplement, body.name, body.dose, body.notes)
    return {"status": "ok", "message": message}


@router.patch("/api/supplements/{supplement_id}")
def patch_supplement(
    supplement_id: int, body: SupplementUpdate, request: Request,
    auth: DashboardSession = Depends(_current_session),
) -> dict:
    _validate_mutation(request, auth)
    return _service_call(
        update_supplement, supplement_id, **body.model_dump(exclude_unset=True)
    )


@router.post("/api/supplements/{supplement_id}/intakes")
def supplement_intake(
    supplement_id: int, body: IntakeBody, request: Request,
    auth: DashboardSession = Depends(_current_session),
) -> dict:
    _validate_mutation(request, auth)
    return {
        "id": _service_call(
            record_supplement_intake, supplement_id, body.status, note=body.note
        )
    }


@router.get("/api/proactive-alerts")
def proactive_alerts(auth: DashboardSession = Depends(_current_session)) -> dict:
    return list_alert_settings()


@router.patch("/api/proactive-alerts/{topic}")
def patch_proactive_alert(
    topic: str, body: ProactiveAlertUpdate, request: Request,
    auth: DashboardSession = Depends(_current_session),
) -> dict:
    _validate_mutation(request, auth)
    return _service_call(update_alert_setting, topic, body.enabled)


@router.get("/api/reminders")
def reminders(auth: DashboardSession = Depends(_current_session)) -> list[dict]:
    return list_reminder_rules()


@router.get("/api/reminders/history")
def reminder_history(
    limit: int = 30, auth: DashboardSession = Depends(_current_session)
) -> list[dict]:
    return list_reminder_occurrences(limit=limit)


@router.get("/api/outbox/unknown")
def unknown_outbox(auth: DashboardSession = Depends(_current_session)) -> list[dict]:
    return list_unknown_notifications()


@router.post("/api/outbox/{outbox_id}/retry")
def retry_outbox(
    outbox_id: int, request: Request, auth: DashboardSession = Depends(_current_session)
) -> dict:
    _validate_mutation(request, auth)
    _service_call(retry_unknown_notification, outbox_id)
    return {"status": "queued"}


@router.post("/api/reminders")
def create_reminder(
    body: ReminderBody, request: Request,
    auth: DashboardSession = Depends(_current_session),
) -> dict:
    _validate_mutation(request, auth)
    text = _service_call(propose_reminder_rule, **body.model_dump())
    rule_id = int(text.split("#", 1)[1].split(":", 1)[0])
    return {"id": rule_id, "status": "draft", "preview": text}


@router.patch("/api/reminders/{rule_id}")
def patch_reminder(
    rule_id: int, body: ReminderUpdate, request: Request,
    auth: DashboardSession = Depends(_current_session),
) -> dict:
    _validate_mutation(request, auth)
    return _service_call(
        update_reminder_rule, rule_id, **body.model_dump(exclude_unset=True)
    )


@router.post("/api/reminders/{rule_id}/activate")
def activate_reminder(
    rule_id: int, request: Request, auth: DashboardSession = Depends(_current_session)
) -> dict:
    _validate_mutation(request, auth)
    return _service_call(activate_reminder_rule, rule_id)


@router.post("/api/logout")
def logout(
    request: Request, response: Response,
    auth: DashboardSession = Depends(_current_session),
) -> dict:
    _validate_mutation(request, auth)
    with get_session() as session:
        row = session.get(DashboardSession, auth.id)
        if row:
            row.revoked = True
    response.delete_cookie(COOKIE_NAME, path="/dash")
    return {"status": "ok"}
