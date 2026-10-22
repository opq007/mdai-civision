"""Admin UI: accounts, proxies, API keys, models, usage, settings."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.civision import costs
from app.civision.accounts import (
    add_account_from_cookies,
    assign_proxy,
    refresh_balance,
    validate_account,
)
from app.civision.mapping import DEFAULT_ALIASES
from app.config import get_settings
from app.crypto import hash_secret, new_api_key
from app.db import get_session
from app.models import Account, ApiKey, ModelAlias, Proxy, UsageLog, VideoJob
from app.security import clear_admin_session, create_admin_session, require_admin
from app.services.catalog import catalog_service
from app.services.errors import openai_error
from app.services.oauth import build_github_auth_url

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "login.html", {"error": None, "settings": get_settings()}
    )


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> RedirectResponse:
    settings = get_settings()
    ok = username == settings.admin_username and password == settings.admin_password
    if not ok:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Incorrect username or password.", "settings": settings},
            status_code=401,
        )
    response = RedirectResponse("/admin", status_code=303)
    create_admin_session(response, settings)
    return response


@router.post("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/admin/login", status_code=303)
    clear_admin_session(response)
    return response


@router.get("", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    accounts = (await session.execute(select(Account))).scalars().all()
    proxies = (await session.execute(select(Proxy))).scalars().all()
    keys = (await session.execute(select(ApiKey))).scalars().all()

    total_balance = sum(a.available_balance for a in accounts)
    active = sum(1 for a in accounts if a.status == "active")
    expired = sum(1 for a in accounts if a.status == "expired")

    day_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    recent = (
        await session.execute(
            select(func.count(VideoJob.id)).where(VideoJob.created_at >= day_ago)
        )
    ).scalar_one()
    spent = (
        await session.execute(
            select(func.coalesce(func.sum(UsageLog.moli_actual), 0.0)).where(
                UsageLog.created_at >= day_ago
            )
        )
    ).scalar_one()
    queued = (
        await session.execute(
            select(func.count(VideoJob.id)).where(VideoJob.status == "queued")
        )
    ).scalar_one()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "accounts": accounts,
            "proxies": proxies,
            "api_keys": keys,
            "total_balance": total_balance,
            "active_accounts": active,
            "expired_accounts": expired,
            "jobs_24h": recent,
            "moli_24h": spent,
            "queued": queued,
            "settings": get_settings(),
        },
    )


@router.get("/accounts", response_class=HTMLResponse)
async def accounts_page(
    request: Request,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    accounts = (await session.execute(select(Account))).scalars().all()
    proxies = (await session.execute(select(Proxy))).scalars().all()
    return templates.TemplateResponse(
        request,
        "accounts.html",
        {"accounts": accounts, "proxies": proxies, "message": None},
    )


@router.post("/accounts/from-cookies")
async def add_account_cookies(
    request: Request,
    name: str = Form(...),
    cookies: str = Form(...),
    proxy_id: str = Form(""),
    max_concurrency: int = Form(1),
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Add an account by pasting the cookie string from the browser."""
    proxy_id_int = int(proxy_id) if proxy_id.strip().isdigit() else None
    account, info = await add_account_from_cookies(
        session,
        name=name,
        cookie_text=cookies,
        proxy_id=proxy_id_int,
        max_concurrency=max_concurrency,
    )
    if account is None:
        return templates.TemplateResponse(
            request,
            "accounts.html",
            {
                "accounts": (await session.execute(select(Account))).scalars().all(),
                "proxies": (await session.execute(select(Proxy))).scalars().all(),
                "message": {
                    "kind": "error",
                    "text": f"Could not add the account: {info.reason}",
                },
            },
            status_code=400,
        )

    if proxy_id_int is None:
        await assign_proxy(session, account)

    return templates.TemplateResponse(
        request,
        "accounts.html",
        {
            "accounts": (await session.execute(select(Account))).scalars().all(),
            "proxies": (await session.execute(select(Proxy))).scalars().all(),
            "message": {
                "kind": "ok",
                "text": f"Added '{account.name}'. Balance: {account.available_balance:.0f} magicubes.",
            },
        },
    )


@router.post("/accounts/oauth-url")
async def account_oauth_url(
    request: Request,
    name: str = Form(""),
    _admin: str = Depends(require_admin),
) -> HTMLResponse:
    """Return a one-click GitHub sign-in URL for a new account."""
    settings = get_settings()
    callback = f"{settings.public_base_url.rstrip('/')}/auth/github/callback"
    url = await build_github_auth_url(callback)
    return templates.TemplateResponse(
        request,
        "accounts.html",
        {
            "accounts": [],
            "proxies": [],
            "message": {
                "kind": "ok",
                "text": "Open this URL, sign in with GitHub, then paste the cookies "
                "back here (DevTools > Application > Cookies > modelscope.ai).",
                "link": url,
            },
        },
    )


@router.post("/accounts/{account_id}/validate")
async def validate_account_route(
    request: Request,
    account_id: int,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    account = await session.get(Account, account_id)
    if account is None:
        raise openai_error(404, "account not found", "invalid_request_error")
    info = await validate_account(session, account)
    if info.authenticated:
        await refresh_balance(session, account)
    await session.commit()
    return templates.TemplateResponse(
        request,
        "_account_row.html",
        {"a": account, "proxies": (await session.execute(select(Proxy))).scalars().all()},
    )


@router.post("/accounts/{account_id}/toggle")
async def toggle_account(
    request: Request,
    account_id: int,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    account = await session.get(Account, account_id)
    if account is None:
        raise openai_error(404, "account not found", "invalid_request_error")
    account.status = "disabled" if account.status == "active" else "active"
    await session.commit()
    return templates.TemplateResponse(
        request,
        "_account_row.html",
        {"a": account, "proxies": (await session.execute(select(Proxy))).scalars().all()},
    )


@router.post("/accounts/{account_id}/delete")
async def delete_account(
    account_id: int,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    account = await session.get(Account, account_id)
    if account is not None:
        await session.delete(account)
    await session.commit()
    return HTMLResponse("")


@router.get("/proxies", response_class=HTMLResponse)
async def proxies_page(
    request: Request,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    proxies = (await session.execute(select(Proxy))).scalars().all()
    return templates.TemplateResponse(
        request, "proxies.html", {"proxies": proxies, "message": None}
    )


@router.post("/proxies")
async def create_proxy(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    username: str = Form(""),
    password: str = Form(""),
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from app.crypto import encrypt

    proxy = Proxy(
        name=name,
        url=url,
        username=username or None,
        password=encrypt(password) if password else None,
        enabled=True,
        healthy=True,
    )
    session.add(proxy)
    await session.commit()
    return templates.TemplateResponse(
        request,
        "_proxy_row.html",
        {"p": proxy},
    )


@router.post("/proxies/{proxy_id}/delete")
async def delete_proxy(
    proxy_id: int,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    proxy = await session.get(Proxy, proxy_id)
    if proxy is not None:
        await session.delete(proxy)
    await session.commit()
    return HTMLResponse("")


@router.get("/keys", response_class=HTMLResponse)
async def keys_page(
    request: Request,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    keys = (await session.execute(select(ApiKey))).scalars().all()
    return templates.TemplateResponse(
        request, "keys.html", {"keys": keys, "new_key": None}
    )


@router.post("/keys")
async def create_key(
    request: Request,
    name: str = Form(...),
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    raw = new_api_key()
    key = ApiKey(
        name=name,
        key_hash=hash_secret(raw),
        key_prefix=raw[:12],
        enabled=True,
    )
    session.add(key)
    await session.commit()
    return templates.TemplateResponse(
        request,
        "keys.html",
        {
            "keys": (await session.execute(select(ApiKey))).scalars().all(),
            "new_key": raw,
        },
    )


@router.post("/keys/{key_id}/delete")
async def delete_key(
    key_id: int,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    key = await session.get(ApiKey, key_id)
    if key is not None:
        await session.delete(key)
    await session.commit()
    return HTMLResponse("")


@router.get("/models", response_class=HTMLResponse)
async def models_page(
    request: Request,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    catalog = await catalog_service.get(session)
    aliases = (await session.execute(select(ModelAlias))).scalars().all()
    alias_names = {row.alias for row in aliases} | set(DEFAULT_ALIASES)
    return templates.TemplateResponse(
        request,
        "models.html",
        {
            "catalog": catalog,
            "aliases": aliases,
            # One block per real model. The aliases point at the same ones, so
            # they are skipped here; the two MiniMax-H3 variants are separate
            # models and each gets its own block.
            "cost_lines": [
                line
                for m in catalog.models
                if m.requested not in alias_names
                for line in costs.lines_for(m)
            ],
            "message": None,
        },
    )


@router.get("/usage", response_class=HTMLResponse)
async def usage_page(
    request: Request,
    _admin: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    jobs = (
        await session.execute(
            select(VideoJob).order_by(VideoJob.created_at_ts.desc()).limit(200)
        )
    ).scalars().all()
    logs = (
        await session.execute(
            select(UsageLog).order_by(UsageLog.created_at.desc()).limit(200)
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request, "usage.html", {"jobs": jobs, "logs": logs}
    )
