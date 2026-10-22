"""Account pool: rotation, proxy pinning, session validation and renewal."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.civision.client import (
    KEEP_COOKIES,
    CivisionClient,
    NotAuthenticated,
    SessionInfo,
    parse_cookie_header,
    probe_pasted_cookies,
)
from app.config import get_settings
from app.crypto import decrypt, encrypt
from app.models import Account, Proxy, utcnow

log = logging.getLogger(__name__)


def stored_jar(account: Account) -> dict[str, str]:
    raw = decrypt(account.cookie_jar)
    return parse_cookie_header(raw) if raw else {}


def save_jar(account: Account, jar: dict[str, str]) -> None:
    keep = {k: v for k, v in jar.items() if k in KEEP_COOKIES}
    account.cookie_jar = encrypt("; ".join(f"{k}={v}" for k, v in keep.items()))


def proxy_url(proxy: Optional[Proxy]) -> Optional[str]:
    """Build the httpx proxy URL, embedding credentials when present."""
    if proxy is None:
        return None
    url = proxy.url
    if proxy.username:
        scheme, sep, rest = url.partition("://")
        if not sep:
            scheme, rest = "", url
        from urllib.parse import quote

        from app.crypto import decrypt

        cred = quote(proxy.username, safe="")
        if proxy.password:
            secret = decrypt(proxy.password)
            if secret:
                cred += ":" + quote(secret, safe="")
        url = f"{scheme}://{cred}@{rest}" if scheme else f"{cred}@{rest}"
    return url


def client_for(account: Account, proxy: Optional[Proxy] = None) -> CivisionClient:
    return CivisionClient(jar=stored_jar(account), proxy=proxy_url(proxy))


async def validate_account(
    session: AsyncSession, account: Account, *, proxy: Optional[Proxy] = None
) -> SessionInfo:
    """Check whether an account's stored session still works."""
    if proxy is None and account.proxy_id:
        proxy = await session.get(Proxy, account.proxy_id)
    client = client_for(account, proxy)
    info = await client.check_session()
    account.last_validated_at = utcnow()
    if info.authenticated:
        account.status = "active"
        account.last_error = None
        save_jar(account, info.cookie_jar)
    else:
        if account.status != "disabled":
            account.status = "expired"
        account.last_error = info.reason
    return info


async def refresh_balance(
    session: AsyncSession, account: Account, *, proxy: Optional[Proxy] = None
) -> dict:
    if proxy is None and account.proxy_id:
        proxy = await session.get(Proxy, account.proxy_id)
    client = client_for(account, proxy)
    bal = await client.balance()
    account.total_balance = bal["total"]
    account.available_balance = bal["available"]
    account.frozen_balance = bal["frozen"]
    account.balance_checked_at = utcnow()
    return bal


async def add_account_from_cookies(
    session: AsyncSession,
    *,
    name: str,
    cookie_text: str,
    proxy_id: Optional[int] = None,
    max_concurrency: Optional[int] = None,
) -> tuple[Optional[Account], SessionInfo]:
    """Create an account from a pasted cookie string.

    The cookie is validated against the live site before it is stored, so a
    bad paste fails loudly instead of failing later during a generation.
    """
    settings = get_settings()
    proxy = await session.get(Proxy, proxy_id) if proxy_id else None
    info = await probe_pasted_cookies(cookie_text, proxy_url(proxy))
    if not info.authenticated:
        return None, info

    account = Account(
        name=name,
        status="active",
        auth_source="cookie",
        proxy_id=proxy_id,
        max_concurrency=max_concurrency or settings.default_max_concurrency,
    )
    save_jar(account, info.cookie_jar)
    session.add(account)
    await session.flush()

    try:
        await refresh_balance(session, account, proxy=proxy)
    except Exception as exc:  # noqa: BLE001
        log.warning("balance refresh failed for new account: %s", exc)

    account.last_validated_at = utcnow()
    return account, info


async def assign_proxy(session: AsyncSession, account: Account) -> Optional[Proxy]:
    """Pin the account to a proxy, keeping its egress IP stable.

    An account that already has a healthy proxy keeps it. Otherwise we pick
    the least-loaded healthy proxy from the pool.
    """
    if account.proxy_id:
        current = await session.get(Proxy, account.proxy_id)
        if current and current.enabled and current.healthy:
            return current

    proxies = (
        await session.execute(
            select(Proxy).where(Proxy.enabled.is_(True), Proxy.healthy.is_(True))
        )
    ).scalars().all()
    if not proxies:
        return None

    counts: dict[int, int] = {}
    for p in proxies:
        counts[p.id] = 0
    bound = (
        await session.execute(
            select(Account).where(Account.proxy_id.in_([p.id for p in proxies]))
        )
    ).scalars().all()
    for a in bound:
        if a.proxy_id:
            counts[a.proxy_id] = counts.get(a.proxy_id, 0) + 1

    best = min(proxies, key=lambda p: (counts.get(p.id, 0), p.fail_count))
    account.proxy_id = best.id
    return best


async def pick_account(session: AsyncSession) -> Optional[Account]:
    """Choose the next account to use.

    Round-robin over accounts that are active, funded, not cooling down and
    below their concurrency cap. Accounts that have been idle longest go
    first, which spreads load and keeps every session warm.
    """
    now = utcnow()
    accounts = (
        await session.execute(select(Account).where(Account.status == "active"))
    ).scalars().all()

    candidates = [
        a
        for a in accounts
        if a.available_balance > 0
        and a.running_jobs < a.max_concurrency
        and not (a.cooldown_until and a.cooldown_until > now)
    ]
    if not candidates:
        return None

    # Round-robin: the account that has been idle longest goes first. The
    # sentinel is timezone-aware because the model layer normalises every
    # timestamp to UTC-aware.
    epoch = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    candidates.sort(key=lambda a: (a.last_used_at or epoch))
    return candidates[0]


async def mark_failure(
    session: AsyncSession, account: Account, error: Exception, *, settings=None
) -> None:
    """Record a failure and decide whether the account should stand down."""
    settings = settings or get_settings()
    account.consecutive_failures += 1
    account.last_error = str(error)

    if isinstance(error, NotAuthenticated):
        account.status = "expired"
        log.warning("account %s session expired", account.id)
        return

    if isinstance(error, Exception) and getattr(error, "code", "") == "insufficient_quota":
        account.status = "quota_exhausted"
        return

    if account.consecutive_failures >= 3:
        account.cooldown_until = utcnow() + dt.timedelta(
            seconds=settings.account_error_cooldown_s
        )
        account.consecutive_failures = 0
        log.warning(
            "account %s cooling down for %ss after repeated failures",
            account.id,
            settings.account_error_cooldown_s,
        )


async def mark_success(session: AsyncSession, account: Account) -> None:
    account.consecutive_failures = 0
    account.last_error = None
    account.last_used_at = utcnow()
    account.cooldown_until = None
