"""Background maintenance: session renewal, balance refresh, TTL cleanup."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
from pathlib import Path

from sqlalchemy import select

from app.civision.accounts import (
    client_for,
    refresh_balance,
    save_jar,
    validate_account,
)
from app.civision.client import CivisionError
from app.config import get_settings
from app.db import session_scope
from app.models import Account, Proxy, VideoJob, utcnow
from app.services.alerts import send_alert
from app.services.metrics import active_accounts_gauge

log = logging.getLogger(__name__)

# ModelScope stamps the daily grant in Beijing time: a grant claimed at 08:00
# expires 24 hours later, so "today" for the grant is the Beijing day.
BEIJING = dt.timezone(dt.timedelta(hours=8))

# How often the loop wakes to see whether anyone is due. The real limit is
# the per-account interval below; this only bounds how late a claim can be.
DAILY_GRANT_TICK_S = 60 * 60


def grant_due(
    now: dt.datetime,
    last_grant_at: dt.datetime | None,
    *,
    min_interval_s: int,
    window_start_hour: int,
    window_end_hour: int,
    account_id: int = 0,
) -> bool:
    """Whether an account should claim its daily grant right now.

    Two guards, both there to keep the traffic from looking scheduled:

    - at most once per ``min_interval_s``, so a restart cannot fire it twice
    - only inside the Beijing-time window, and not before an offset derived
      from the account id, so accounts do not all claim at the opening minute
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    if last_grant_at is not None:
        if last_grant_at.tzinfo is None:
            last_grant_at = last_grant_at.replace(tzinfo=dt.timezone.utc)
        if (now - last_grant_at).total_seconds() < min_interval_s:
            return False
    local = now.astimezone(BEIJING)
    if not window_start_hour <= local.hour < window_end_hour:
        return False
    earliest = window_start_hour * 60 + (account_id * 47) % 150
    return local.hour * 60 + local.minute >= earliest


class Maintenance:
    """Runs the periodic upkeep tasks."""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._session_loop()),
            asyncio.create_task(self._cleanup_loop()),
            asyncio.create_task(self._daily_grant_loop()),
        ]
        log.info("maintenance started")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()

    async def _session_loop(self) -> None:
        settings = get_settings()
        while True:
            try:
                await asyncio.sleep(settings.session_check_interval_s)
                await self.check_sessions()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                log.exception("session check failed")

    async def _daily_grant_loop(self) -> None:
        """Claim the daily magicube grant, at most once per account per window.

        The grant is a side effect of reading /api/v1/users/login/info, which is
        what the site does when an already-signed-in browser opens a page. The
        session probe does not do this, so without this loop the grant is never
        collected. A short random delay on startup keeps a restart from claiming
        at a predictable instant.
        """
        await asyncio.sleep(random.uniform(30, 300))
        while True:
            try:
                await self.claim_daily_grants()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                log.exception("daily grant check failed")
            try:
                await asyncio.sleep(DAILY_GRANT_TICK_S)
            except asyncio.CancelledError:
                return

    async def claim_daily_grants(self, *, now: dt.datetime | None = None) -> dict:
        """Claim the day's grant for every account that is due for it."""
        settings = get_settings()
        now = now or utcnow()
        summary = {"checked": 0, "claimed": 0, "skipped": 0, "failed": 0}
        async with session_scope() as session:
            accounts = (await session.execute(select(Account))).scalars().all()
            for account in accounts:
                if account.status != "active":
                    continue
                if not grant_due(
                    now,
                    account.daily_grant_at,
                    min_interval_s=settings.daily_grant_min_interval_s,
                    window_start_hour=settings.daily_grant_window_start_hour,
                    window_end_hour=settings.daily_grant_window_end_hour,
                    account_id=account.id or 0,
                ):
                    summary["skipped"] += 1
                    continue
                summary["checked"] += 1
                # Spread the accounts out instead of requesting them back to back.
                await asyncio.sleep(random.uniform(5, 40))
                try:
                    await self._claim_one(session, account)
                    summary["claimed"] += 1
                except CivisionError as exc:
                    # A failed read grants nothing, so leave daily_grant_at alone
                    # and let the next wake try again. It is not evidence that
                    # the session is dead; the session loop decides that.
                    summary["failed"] += 1
                    log.warning(
                        "daily grant failed for %s: %s", account.name, exc.diagnostic()
                    )
        return summary

    async def _claim_one(self, session, account: Account) -> None:
        proxy = await session.get(Proxy, account.proxy_id) if account.proxy_id else None
        client = client_for(account, proxy)
        await client.touch_login()
        account.daily_grant_at = utcnow()
        save_jar(account, client.jar)
        try:
            await refresh_balance(session, account, proxy=proxy)
        except CivisionError:
            log.warning("balance refresh after daily grant failed for %s", account.name)

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(3600)
                await cleanup_expired()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                log.exception("cleanup failed")

    async def check_sessions(self) -> dict:
        """Validate every account and refresh balances."""
        summary = {
            "checked": 0,
            "ok": 0,
            "expired": 0,
            "unreachable": 0,
            "transient": 0,
        }
        async with session_scope() as session:
            accounts = (await session.execute(select(Account))).scalars().all()
            for account in accounts:
                if account.status == "disabled":
                    continue
                summary["checked"] += 1
                try:
                    info = await validate_account(session, account)
                    if info.authenticated:
                        summary["ok"] += 1
                        try:
                            await refresh_balance(session, account)
                        except Exception:  # noqa: BLE001
                            pass
                    elif info.unreachable:
                        # ModelScope is down or the network is broken. This is
                        # not a session problem, so the account keeps its
                        # status and no alert goes out.
                        summary["unreachable"] += 1
                        log.warning(
                            "could not reach ModelScope for %s: %s",
                            account.name,
                            info.reason,
                        )
                    elif info.transient:
                        # A rate limit, WAF/403, or another upstream error.
                        # The cookie may still be valid; keep the account and
                        # record the reason for the operator.
                        summary["transient"] += 1
                        log.warning(
                            "upstream gave no session verdict for %s: %s",
                            account.name,
                            info.reason,
                        )
                    else:
                        summary["expired"] += 1
                        if account.status != "expired":
                            await send_alert(
                                f"ModelScope session expired for account '{account.name}'"
                            )
                except Exception as exc:  # noqa: BLE001
                    log.warning("session check failed for %s: %s", account.name, exc)

            active = sum(1 for a in accounts if a.status == "active")
        active_accounts_gauge.set(active)
        return summary


async def cleanup_expired() -> int:
    """Delete video files past their TTL and mark the jobs as expired."""
    settings = get_settings()
    removed = 0
    now = utcnow()

    async with session_scope() as session:
        jobs = (
            await session.execute(
                select(VideoJob).where(VideoJob.status == "completed")
            )
        ).scalars().all()
        for job in jobs:
            if job.expires_at and job.expires_at < now:
                if job.local_path:
                    path: Path = settings.storage_dir / job.local_path
                    if path.exists():
                        path.unlink(missing_ok=True)
                        removed += 1
                job.local_path = None
                job.upstream_url = None
                job.status = "failed"
                job.error_code = "expired"
                job.error_message = "the video expired and was removed"

    if removed:
        log.info("removed %d expired video files", removed)
    return removed


maintenance = Maintenance()
