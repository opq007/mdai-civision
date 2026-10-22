"""Background maintenance: session renewal, balance refresh, TTL cleanup."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy import select

from app.civision.accounts import (
    refresh_balance,
    validate_account,
)
from app.config import get_settings
from app.db import session_scope
from app.models import Account, VideoJob, utcnow
from app.services.alerts import send_alert
from app.services.metrics import active_accounts_gauge

log = logging.getLogger(__name__)


class Maintenance:
    """Runs the periodic upkeep tasks."""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._session_loop()),
            asyncio.create_task(self._cleanup_loop()),
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
        summary = {"checked": 0, "ok": 0, "expired": 0, "unreachable": 0}
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
