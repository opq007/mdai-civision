"""Authentication for both audiences:

* clients of the OpenAI-compatible API use `Authorization: Bearer <key>`
* the admin UI uses a signed session cookie
"""

from __future__ import annotations

import secrets

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import hash_secret
from app.db import get_session
from app.models import ApiKey
from app.services.errors import openai_error

ADMIN_SESSION_COOKIE = "civision_admin"


def hash_api_key(raw: str) -> str:
    return hash_secret(raw)


def constant_time_equals(a: str, b: str) -> bool:
    return secrets.compare_digest(a, b)


async def require_api_key(
    request: Request, session: AsyncSession = Depends(get_session)
) -> ApiKey:
    """Authenticate an OpenAI-compatible API caller."""
    header = request.headers.get("authorization") or ""
    token = ""
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    if not token:
        token = request.headers.get("x-api-key", "").strip()
    if not token:
        raise openai_error(
            401, "missing API key; send 'Authorization: Bearer <key>'", "invalid_request_error"
        )

    row = (
        await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_api_key(token)))
    ).scalar_one_or_none()
    if row is None or not row.enabled:
        raise openai_error(401, "invalid API key", "invalid_request_error")

    row.last_used_at = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    )
    return row


# -- admin session ----------------------------------------------------------


def _signer():
    from itsdangerous import URLSafeTimedSerializer

    from app.config import get_settings

    return URLSafeTimedSerializer(get_settings().secret_key, salt="civision-admin")


def create_admin_session(response, settings) -> None:
    token = _signer().dumps({"u": settings.admin_username})
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=7 * 24 * 3600,
        path="/",
    )


def clear_admin_session(response) -> None:
    response.delete_cookie(ADMIN_SESSION_COOKIE, path="/")


def admin_is_logged_in(request: Request) -> bool:
    from itsdangerous import BadSignature, SignatureExpired

    from app.config import get_settings

    settings = get_settings()
    raw = request.cookies.get(ADMIN_SESSION_COOKIE)
    if not raw:
        return False
    try:
        data = _signer().loads(raw, max_age=7 * 24 * 3600)
    except (BadSignature, SignatureExpired):
        return False
    return data.get("u") == settings.admin_username


async def require_admin(request: Request) -> str:
    if not admin_is_logged_in(request):
        raise openai_error(401, "admin sign-in required", "invalid_request_error")
    from app.config import get_settings

    return get_settings().admin_username
