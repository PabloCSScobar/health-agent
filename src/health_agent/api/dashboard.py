"""Uwierzytelniony, jednoużytkownikowy dashboard bez zewnętrznego frontendu."""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from collections import defaultdict, deque
from collections.abc import Callable
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
from health_agent.tools.correlations import get_correlations, daily_frame
from health_agent.tools.photos import (
    delete_progress_photo,
    list_progress_photos,
    progress_photo_path,
    save_progress_photo,
)
from health_agent.tools.reminders import (
    activate_reminder_rule,
    add_supplement,
    list_reminder_rules,
    list_supplements,
    list_unknown_notifications,
    propose_reminder_rule,
    record_supplement_intake,
    retry_unknown_notification,
    update_reminder_rule,
    update_supplement,
)

router = APIRouter(prefix="/dash")
COOKIE_NAME = "health_agent_session"
_PASSWORD_HASHER = PasswordHasher()
_login_attempts: dict[str, deque[dt.datetime]] = defaultdict(deque)


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


def _current_session(request: Request) -> DashboardSession:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Zaloguj się")
    now = dt.datetime.now(dt.timezone.utc)
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
            raise HTTPException(status_code=401, detail="Sesja wygasła")
        row.last_seen_at = now
        session.flush()
        session.expunge(row)
        return row


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
    return request.client.host if request.client else "unknown"


def _check_login_rate(request: Request) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    attempts = _login_attempts[_client_key(request)]
    cutoff = now - dt.timedelta(minutes=15)
    while attempts and attempts[0] < cutoff:
        attempts.popleft()
    if len(attempts) >= 5:
        raise HTTPException(status_code=429, detail="Za dużo prób. Spróbuj za 15 minut.")
    attempts.append(now)


LOGIN_HTML = """<!doctype html>
<html lang="pl"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>health-agent — logowanie</title>
<style>body{font-family:system-ui;max-width:28rem;margin:12vh auto;padding:1rem;background:#f4f6f8}
form{background:white;padding:2rem;border-radius:1rem;box-shadow:0 8px 30px #0001}input,button{box-sizing:border-box;width:100%;padding:.8rem;margin:.4rem 0}button{background:#14532d;color:white;border:0;border-radius:.5rem}</style>
<form id="login"><h1>health-agent</h1><p>Prywatny dashboard</p>
<input id="password" type="password" autocomplete="current-password" placeholder="Hasło" required>
<button>Zaloguj</button><p id="error"></p></form>
<script>document.getElementById("login").onsubmit=async function(e){e.preventDefault();
var r=await fetch("/dash/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password:document.getElementById("password").value})});
if(r.ok){location="/dash"}else{document.getElementById("error").textContent=(await r.json()).detail}}</script></html>"""


DASHBOARD_HTML = """<!doctype html>
<html lang="pl"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>health-agent</title>
<style>
:root{font-family:system-ui;color:#17221b;background:#eef3ef}body{max-width:1100px;margin:auto;padding:1rem}
nav{display:flex;gap:.5rem;flex-wrap:wrap}.card{background:white;border-radius:.8rem;padding:1rem;margin:1rem 0;box-shadow:0 5px 20px #0001}
button,input,select{padding:.55rem;margin:.2rem;border:1px solid #bdc9c0;border-radius:.4rem}button{cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1rem}.hidden{display:none}
.chart{height:160px;display:flex;align-items:end;gap:2px;border-bottom:1px solid #aaa}.bar{background:#2f855a;min-width:4px;flex:1}
.photos{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:1rem}.photos img{width:100%;border-radius:.5rem}
small{color:#526058}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:.5rem;border-bottom:1px solid #ddd}
</style>
<header><h1>health-agent</h1><nav>
<button onclick="showTab('overview')">Przegląd</button><button onclick="showTab('correlations')">Korelacje</button>
<button onclick="showTab('photos')">Zdjęcia</button><button onclick="showTab('supplements')">Suplementy</button>
<button onclick="showTab('reminders')">Przypomnienia</button><button onclick="logout()">Wyloguj</button></nav></header>
<main>
<section id="overview" class="tab"><div class="card"><label>Zakres <select id="days" onchange="loadOverview()"><option>7</option><option selected>30</option><option>90</option></select> dni</label>
<div id="summary" class="grid"></div></div><div id="charts"></div></section>
<section id="correlations" class="tab hidden"><div class="card"><h2>Korelacje</h2><p><small>Obserwacje eksploracyjne, nie dowód przyczynowości.</small></p><div id="corr"></div></div></section>
<section id="photos" class="tab hidden"><div class="card"><h2>Archiwum zdjęć</h2>
<input id="photoFile" type="file" accept="image/jpeg,image/png,image/webp"><input id="photoDate" type="date">
<select id="photoView"><option value="front">przód</option><option value="side">bok</option><option value="back">tył</option><option value="other">inne</option></select>
<input id="photoNote" placeholder="Notatka"><button onclick="uploadPhoto()">Dodaj</button></div><div id="photoList" class="photos"></div></section>
<section id="supplements" class="tab hidden"><div class="card"><h2>Suplementy</h2>
<input id="supName" placeholder="Nazwa"><input id="supDose" placeholder="Dawka"><button onclick="addSupplement()">Dodaj</button>
<table><tbody id="supList"></tbody></table></div></section>
<section id="reminders" class="tab hidden"><div class="card"><h2>Przypomnienia</h2>
<input id="remTitle" placeholder="Treść"><input id="remTime" type="time" value="20:00">
<select id="remKind"><option value="text">tekst</option><option value="supplement">suplement</option><option value="run">bieg</option><option value="workout">trening</option></select>
<select id="remCondition"><option value="">bez warunku</option><option value="steps_below">kroki poniżej</option><option value="protein_below">białko poniżej</option><option value="no_run">brak biegu</option><option value="no_workout">brak treningu</option></select>
<input id="remThreshold" type="number" placeholder="Próg"><button onclick="addReminder()">Dodaj szkic</button>
<table><tbody id="remList"></tbody></table></div></section></main>
<script>
var csrf="";
function esc(v){return String(v==null?"":v).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]})}
async function api(path,opt){opt=opt||{};opt.headers=opt.headers||{};if(opt.method&&opt.method!="GET"){opt.headers["X-CSRF-Token"]=csrf}var r=await fetch("/dash/api"+path,opt);if(r.status==401){location="/dash";throw Error("auth")}var body=await r.json().catch(function(){return {}});if(!r.ok)throw Error(body.detail||"Błąd");return body}
function showTab(id){document.querySelectorAll(".tab").forEach(function(x){x.classList.add("hidden")});document.getElementById(id).classList.remove("hidden");if(id=="correlations")loadCorrelations();if(id=="photos")loadPhotos();if(id=="supplements")loadSupplements();if(id=="reminders")loadReminders()}
function chart(name,points,key){var vals=points.map(function(x){return x[key]}).filter(function(x){return x!=null});var max=Math.max.apply(null,vals.concat([1]));return '<div class="card"><h3>'+name+'</h3><div class="chart">'+points.map(function(x){return '<div class="bar" title="'+x.date+': '+(x[key]==null?'brak':x[key])+'" style="height:'+((x[key]||0)/max*100)+'%"></div>'}).join("")+'</div></div>'}
async function loadOverview(){var data=await api("/overview?days="+document.getElementById("days").value);csrf=data.csrf;document.getElementById("summary").innerHTML=Object.keys(data.latest).map(function(k){return '<div><b>'+esc(k)+'</b><br>'+(data.latest[k]==null?'brak':data.latest[k])+'</div>'}).join("");document.getElementById("charts").innerHTML=chart("Waga (kg)",data.series,"weight_kg")+chart("Sen (h)",data.series,"sleep_h")+chart("HRV",data.series,"hrv")+chart("Kroki",data.series,"steps")+chart("Kilometry biegu",data.series,"run_km")+chart("Białko (g)",data.series,"protein_g")}
async function loadCorrelations(){var data=await api("/correlations");document.getElementById("corr").innerHTML=data.map(function(x){return '<p><b>'+x.label+'</b>: rho '+(x.rho==null?'—':x.rho)+', n='+x.n+' <small>'+x.status+'</small></p>'}).join("")}
async function loadPhotos(){var data=await api("/photos");document.getElementById("photoList").innerHTML=data.map(function(x){return '<article class="card"><img src="/dash/api/photos/'+x.id+'/file"><b>'+esc(x.captured_date)+' · '+esc(x.view)+'</b><br><small>'+(x.weight_kg||'—')+' kg · '+(x.fat_pct||'—')+'% · '+esc(x.note||'')+'</small><br><button onclick="deletePhoto('+x.id+')">Usuń</button></article>'}).join("")}
async function uploadPhoto(){var f=document.getElementById("photoFile").files[0];if(!f)return;await api("/photos",{method:"POST",headers:{"Content-Type":f.type,"X-Photo-Date":document.getElementById("photoDate").value,"X-Photo-View":document.getElementById("photoView").value,"X-Photo-Note":encodeURIComponent(document.getElementById("photoNote").value)},body:f});loadPhotos()}
async function deletePhoto(id){await api("/photos/"+id,{method:"DELETE"});loadPhotos()}
async function loadSupplements(){var data=await api("/supplements");document.getElementById("supList").innerHTML=data.map(function(x){return '<tr><td>'+esc(x.name)+'</td><td>'+esc(x.dose||'')+'</td><td>'+esc(x.last_status||'—')+'</td><td><button onclick="intake('+x.id+',&quot;taken&quot;)">Wzięte</button><button onclick="intake('+x.id+',&quot;skipped&quot;)">Pominięte</button><button onclick="toggleSupplement('+x.id+','+(!x.active)+')">'+(x.active?'Wyłącz':'Włącz')+'</button></td></tr>'}).join("")}
async function addSupplement(){await api("/supplements",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:document.getElementById("supName").value,dose:document.getElementById("supDose").value})});loadSupplements()}
async function intake(id,status){await api("/supplements/"+id+"/intakes",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({status:status})});loadSupplements()}
async function toggleSupplement(id,active){await api("/supplements/"+id,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({active:active})});loadSupplements()}
async function loadReminders(){var data=await api("/reminders");var unknown=await api("/outbox/unknown");document.getElementById("remList").innerHTML=data.map(function(x){return '<tr><td>'+esc(x.title)+'</td><td>'+esc(x.local_time)+'</td><td>'+esc(x.status)+'</td><td>'+(x.status=="draft"?'<button onclick="activateReminder('+x.id+')">Aktywuj</button>':'<button onclick="toggleReminder('+x.id+',&quot;'+(x.status=="active"?'paused':'active')+'&quot;)">'+(x.status=="active"?'Wstrzymaj':'Wznów')+'</button>')+'</td></tr>'}).join("")+unknown.map(function(x){return '<tr><td>'+esc(x.title)+'</td><td colspan="2">Niepewna wysyłka</td><td><button onclick="retryOutbox('+x.id+')">Wyślij ponownie</button></td></tr>'}).join("")}
async function addReminder(){var c=document.getElementById("remCondition").value||null;var t=document.getElementById("remThreshold").value;await api("/reminders",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({title:document.getElementById("remTitle").value,kind:document.getElementById("remKind").value,local_time:document.getElementById("remTime").value,condition_type:c,condition_threshold:t?Number(t):null})});loadReminders()}
async function activateReminder(id){await api("/reminders/"+id+"/activate",{method:"POST"});loadReminders()}
async function toggleReminder(id,status){await api("/reminders/"+id,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({status:status})});loadReminders()}
async function retryOutbox(id){await api("/outbox/"+id+"/retry",{method:"POST"});loadReminders()}
async function logout(){await api("/logout",{method:"POST"});location="/dash"}
document.getElementById("photoDate").value=new Date().toISOString().slice(0,10);loadOverview();
</script></html>"""


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    try:
        _current_session(request)
    except HTTPException:
        return HTMLResponse(LOGIN_HTML)
    return HTMLResponse(DASHBOARD_HTML)


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
    days = min(max(days, 7), 90)
    rows = daily_frame(days=days)
    serialized = [
        {key: value.isoformat() if isinstance(value, dt.date) else value for key, value in row.items()}
        for row in rows
    ]
    latest = {}
    for key, label in (
        ("sleep_h", "Sen"),
        ("hrv", "HRV"),
        ("steps", "Kroki"),
        ("run_km", "Kilometry biegu"),
        ("weight_kg", "Waga"),
        ("protein_g", "Białko"),
    ):
        latest[label] = next((row[key] for row in reversed(rows) if row[key] is not None), None)
    return {"csrf": auth.csrf_token, "latest": latest, "series": serialized}


@router.get("/api/correlations")
def correlations(auth: DashboardSession = Depends(_current_session)) -> list[dict]:
    return get_correlations()


@router.get("/api/photos")
def photos(auth: DashboardSession = Depends(_current_session)) -> list[dict]:
    return list_progress_photos()


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


@router.get("/api/reminders")
def reminders(auth: DashboardSession = Depends(_current_session)) -> list[dict]:
    return list_reminder_rules()


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
