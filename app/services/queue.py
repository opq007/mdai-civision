"""Job queue and the worker pool that drains it.

Jobs live in SQLite so a restart does not lose them. The queue itself is an
in-process asyncio queue; the database row is the source of truth for state.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session_scope
from app.models import Account, VideoJob, utcnow
from app.schemas import VideoResource

log = logging.getLogger(__name__)


def job_to_resource(job: VideoJob) -> VideoResource:
    """Render a job row as an OpenAI VideoResource."""
    error = None
    if job.error_message:
        error = {
            "message": job.error_message,
            "type": job.error_code or "server_error",
            "code": job.error_code,
            "param": None,
        }
    return VideoResource(
        id=job.id,
        object="video",
        model=job.model,
        status=job.status,
        progress=job.progress,
        created_at=job.created_at_ts or int(job.created_at.timestamp()),
        completed_at=int(job.completed_at.timestamp()) if job.completed_at else None,
        expires_at=int(job.expires_at.timestamp()) if job.expires_at else None,
        prompt=job.prompt,
        size=job.size,
        seconds=job.seconds,
        remixed_from_video_id=None,
        error=error,
    )


class JobQueue:
    """In-process queue with a database-backed pending set."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._started = False

    async def start(self, concurrency: int) -> None:
        if self._started:
            return
        self._started = True
        for i in range(concurrency):
            self._workers.append(asyncio.create_task(self._worker(i)))
        log.info("queue started with %d workers", concurrency)

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        self._workers.clear()
        self._started = False

    async def enqueue(self, job_id: str) -> None:
        await self._queue.put(job_id)

    async def requeue_pending(self) -> int:
        """Re-queue jobs that were queued or running when we last stopped.

        Also clears the per-account running-job counters: they describe work
        that was in flight in the previous process, which no longer exists.
        Leaving them set would make every account look permanently busy.
        """
        count = 0
        async with session_scope() as session:
            accounts = (await session.execute(select(Account))).scalars().all()
            for account in accounts:
                if account.running_jobs:
                    account.running_jobs = 0
                if account.cooldown_until and account.cooldown_until < utcnow():
                    account.cooldown_until = None

            jobs = (
                await session.execute(
                    select(VideoJob).where(
                        VideoJob.status.in_(["queued", "in_progress"])
                    )
                )
            ).scalars().all()
            for job in jobs:
                job.status = "queued"
                job.progress = 0
                await self._queue.put(job.id)
                count += 1
        if count:
            log.info("re-queued %d pending jobs", count)
        return count

    async def _worker(self, index: int) -> None:
        from app.services.runner import run_job

        while True:
            try:
                job_id = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                await run_job(job_id)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                log.exception("job %s crashed: %s", job_id, exc)
                async with session_scope() as session:
                    job = await session.get(VideoJob, job_id)
                    if job and job.status not in ("completed", "failed"):
                        job.status = "failed"
                        job.error_code = "internal_error"
                        job.error_message = str(exc)
                        job.completed_at = utcnow()
            finally:
                self._queue.task_done()


job_queue = JobQueue()


async def enqueue_job(session: AsyncSession, job: VideoJob) -> None:
    """Persist a queued job and hand it to the workers."""
    await session.flush()
    await job_queue.enqueue(job.id)


async def queue_depth(session: AsyncSession) -> int:
    pending = (
        await session.execute(
            select(VideoJob).where(VideoJob.status == "queued")
        )
    ).scalars().all()
    return len(pending)
