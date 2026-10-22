"""FastAPI application wiring."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select

from app.config import get_settings
from app.db import init_db, session_scope
from app.models import Account
from app.ratelimit import enforce_rate_limit
from app.routers import admin, auth, videos
from app.security import require_api_key
from app.services.maintenance import maintenance
from app.services.metrics import requests_total
from app.services.queue import job_queue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await job_queue.start(settings.worker_concurrency)
    await job_queue.requeue_pending()
    await maintenance.start()
    log.info("%s ready", settings.app_name)
    try:
        yield
    finally:
        await maintenance.stop()
        await job_queue.stop()


app = FastAPI(
    title=settings.app_name,
    description="OpenAI-compatible video generation on top of ModelScope Civision",
    version="0.1.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(
    videos.router,
    prefix="/v1",
    dependencies=[Depends(require_api_key), Depends(enforce_rate_limit)],
)


@app.middleware("http")
async def count_requests(request: Request, call_next):
    response = await call_next(request)
    requests_total.labels(
        path=request.url.path, status=str(response.status_code)
    ).inc()
    return response


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """Render body-validation failures the way OpenAI does.

    OpenAI answers a malformed body with 400 and `invalid_request_error`;
    FastAPI's default is 422 with a `detail` array, which breaks clients that
    parse the error envelope.
    """
    first = exc.errors()[0] if exc.errors() else {}
    loc = [str(p) for p in first.get("loc", []) if p not in ("body", "query", "path")]
    param = ".".join(loc) or None
    message = first.get("msg", "invalid request")
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "code": None,
                "param": param,
            }
        },
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Render errors in the OpenAI error shape.

    The admin UI does not care about the shape, but OpenAI clients expect
    `{"error": {...}}` at the top level, not FastAPI's `{"detail": ...}`.
    """
    detail = exc.detail
    if isinstance(detail, dict) and "error" in detail:
        payload = detail
    else:
        payload = {
            "error": {
                "message": str(detail),
                "type": (
                    "rate_limit_error"
                    if exc.status_code == 429
                    else "server_error"
                    if exc.status_code >= 500
                    else "invalid_request_error"
                ),
                "code": None,
                "param": None,
            }
        }
    return JSONResponse(status_code=exc.status_code, content=payload)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    """Render unexpected errors in the OpenAI error shape."""
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "message": "internal server error",
                "type": "server_error",
                "code": "internal_error",
                "param": None,
            }
        },
    )


@app.get("/healthz")
async def healthz() -> JSONResponse:
    async with session_scope() as session:
        accounts = (await session.execute(select(Account))).scalars().all()
    active = sum(1 for a in accounts if a.status == "active")
    return JSONResponse(
        {
            "status": "ok",
            "accounts": len(accounts),
            "active_accounts": active,
        }
    )


@app.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(
        "<h1>mdai-civision</h1>"
        "<p>OpenAI-compatible video generation backed by ModelScope Civision.</p>"
        '<ul><li><a href="/docs">API docs</a></li>'
        '<li><a href="/admin">Admin</a></li></ul>'
    )
