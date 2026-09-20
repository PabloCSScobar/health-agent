"""FastAPI - właściwy serwer produkcyjny (następca scripts/test_healthconnect_webhook.py).

Uruchomienie:
    uv run uvicorn health_agent.api.app:app --host 0.0.0.0 --port 8000 \\
        --ssl-certfile secrets/tailscale.crt --ssl-keyfile secrets/tailscale.key
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request

from health_agent.api.dashboard import router as dashboard_router
from health_agent.db.session import get_session
from health_agent.ingest.healthconnect import ingest_payload
from health_agent.scheduler import build_scheduler
from health_agent.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("health_agent.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from health_agent.tools.photos import reconcile_progress_photos

    photo_reconcile = reconcile_progress_photos()
    if photo_reconcile["fixed"] or photo_reconcile["deleted_staging_rows"]:
        logger.info("Naprawiono stan archiwum zdjęć: %s", photo_reconcile)
    scheduler = None
    if settings.scheduler_enabled:
        scheduler = build_scheduler()
        scheduler.start()
        logger.info(
            "Scheduler wystartował (środowisko=%s, polling Intervals.icu co godzinę)",
            settings.app_env,
        )
    else:
        logger.info("Scheduler wyłączony (środowisko=%s)", settings.app_env)
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)


app = FastAPI(title="health-agent", lifespan=lifespan)
app.include_router(dashboard_router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/dash/api/"):
        response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    # Dashboard nie ma już skryptów inline; CSS inline zostaje dla atrybutów
    # style ustawianych przez wykresy i porównanie zdjęć.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    return response


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/webhook/healthconnect")  # ścieżka zgodna z tym co już skonfigurowane w apce na telefonie
async def ingest_healthconnect(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
) -> dict:
    if settings.app_env == "production" and not settings.webhook_shared_secret:
        raise HTTPException(status_code=503, detail="WEBHOOK_SHARED_SECRET nie jest skonfigurowany")
    if settings.webhook_shared_secret and x_webhook_secret != settings.webhook_shared_secret:
        raise HTTPException(status_code=401, detail="zły albo brakujący X-Webhook-Secret")

    payload = await request.json()
    with get_session() as session:
        summary = ingest_payload(session, payload)

    return {"status": "ok", **summary}
