"""Executes one video job end to end.

The flow mirrors what a human does on the civision page: pick an account,
submit the task, poll until it settles, then download the result. Accounts
are rotated when one runs out of magicubes, goes stale, or gets rate
limited.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

import httpx

from app.civision.accounts import (
    assign_proxy,
    mark_failure,
    mark_success,
    pick_account,
    proxy_url,
    refresh_balance,
    stored_jar,
)
from app.civision.client import (
    CivisionClient,
    CivisionError,
    InsufficientBalance,
    InvalidRequest,
    NotAuthenticated,
    RateLimited,
)
from app.civision.upload import upload_image
from app.config import get_settings
from app.db import session_scope
from app.models import Account, UsageLog, VideoJob, utcnow
from app.services.alerts import send_alert
from app.services.catalog import catalog_service
from app.services.metrics import jobs_failed_total, jobs_succeeded_total, moli_spent_total

log = logging.getLogger(__name__)

UPSTREAM_TERMINAL = {"SUCCEED", "FAILED", "CANCELED"}


@dataclass
class JobSpec:
    """The immutable description of a job, read once at the start."""

    job_id: str
    model: str
    prompt: str
    negative_prompt: str
    width: int
    height: int
    seconds: int
    guidance_scale: float
    num_inference_steps: int
    input_reference_url: Optional[str]


async def _load_spec(job_id: str) -> Optional[JobSpec]:
    async with session_scope() as session:
        job = await session.get(VideoJob, job_id)
        if job is None:
            return None
        return JobSpec(
            job_id=job.id,
            model=job.model,
            prompt=job.prompt,
            negative_prompt=job.negative_prompt,
            width=job.width,
            height=job.height,
            seconds=int(job.seconds),
            guidance_scale=job.guidance_scale,
            num_inference_steps=job.num_inference_steps,
            input_reference_url=job.input_reference_url,
        )


async def _prepare_image_input(
    spec: JobSpec, client: CivisionClient
) -> Optional[str]:
    """Turn an OpenAI `input_reference` into a URL ModelScope can fetch.

    Returns the image URL, or None for a text-to-video job. Base64 data and
    local files are uploaded to ModelScope's own storage first, because
    upstream has to be able to download the reference image.
    """
    if not spec.input_reference_url:
        return None

    url = spec.input_reference_url

    if url.startswith("data:"):
        import base64

        header, _, payload = url.partition(",")
        if ";base64" not in header:
            raise InvalidRequest("input_reference data URL must be base64 encoded")
        try:
            raw = base64.b64decode(payload)
        except Exception as exc:  # noqa: BLE001
            raise InvalidRequest(f"input_reference is not valid base64: {exc}") from exc
        return await upload_image(client, raw, f"ref_{spec.job_id}.png")

    if url.startswith(("http://", "https://")):
        return url

    # A local path: upload the bytes.
    path = Path(url)
    if not path.exists():
        raise InvalidRequest(f"input_reference file not found: {url}")
    return await upload_image(client, path.read_bytes(), path.name)


async def _download(url: str, destination) -> int:
    async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as c:
        async with c.stream("GET", url) as r:
            r.raise_for_status()
            total = 0
            with open(destination, "wb") as fh:
                async for chunk in r.aiter_bytes(256 * 1024):
                    fh.write(chunk)
                    total += len(chunk)
            return total


async def _sleep_until_next_submit(last_used_at: Optional[dt.datetime]) -> None:
    """Respect the upstream per-account submit rate limit.

    Upstream rejects rapid submissions with "Request frequency too fast";
    measured to need roughly 40 seconds between them.
    """
    settings = get_settings()
    if not last_used_at:
        return
    elapsed = (utcnow() - last_used_at).total_seconds()
    wait = settings.submit_cooldown_s - elapsed
    if wait > 0:
        log.info("waiting %.0fs before submitting", wait)
        await asyncio.sleep(wait)


async def run_job(job_id: str) -> None:
    """Run one job to completion, rotating accounts as needed."""
    settings = get_settings()

    spec = await _load_spec(job_id)
    if spec is None:
        return

    async with session_scope() as session:
        job = await session.get(VideoJob, job_id)
        if job is None or job.status in ("completed", "failed"):
            return
        job.status = "in_progress"
        job.progress = 0

    catalog = await catalog_service.get()
    model = catalog.resolve(spec.model)
    if model is None:
        await _fail(job_id, "invalid_request_error", f"unknown model {spec.model!r}")
        jobs_failed_total.inc()
        return

    # The reference image has to be uploaded to ModelScope's own storage
    # before it can be referenced, and that needs an account's session, so
    # it happens per account below.
    wants_image = bool(spec.input_reference_url)
    predict_type = "IMG_2_VIDEO" if wants_image else "TXT_2_VIDEO"
    attempts = 0
    last_error: Optional[str] = None

    # If the job already has an upstream task id, the process was restarted
    # mid-flight. Resume polling that task instead of submitting again, which
    # would waste magicubes.
    async with session_scope() as session:
        job = await session.get(VideoJob, job_id)
        resume_task_id = job.upstream_task_id if job else None

    if resume_task_id:
        resumed = await _resume_task(job_id, resume_task_id)
        if resumed:
            return

    while attempts < settings.max_account_retries:
        attempts += 1

        account = await _select_account()
        if account is None:
            async with session_scope() as session:
                job = await session.get(VideoJob, job_id)
                if job:
                    job.status = "queued"
                    job.attempts = attempts
            log.info("no usable account for %s; requeued", job_id)
            await asyncio.sleep(10)
            attempts -= 1
            if attempts < 0:
                break
            continue

        try:
            await _run_with_account(job_id, spec, account, model, predict_type)
            await _log_usage(job_id, account, "completed", None)
            return
        except (NotAuthenticated, InsufficientBalance, RateLimited) as exc:
            last_error = str(exc)
            await _handle_account_error(account, exc)
        except InvalidRequest as exc:
            await _fail(job_id, "invalid_request_error", str(exc))
            await _log_usage(job_id, account, "failed", str(exc))
            jobs_failed_total.inc()
            return
        except CivisionError as exc:
            last_error = str(exc)
            await _handle_account_error(account, exc)
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            log.exception("job %s failed unexpectedly", job_id)
            await _handle_account_error(account, exc)

    await _fail(job_id, "server_error", last_error or "could not complete the job")
    await _log_usage(job_id, None, "failed", last_error)
    jobs_failed_total.inc()


async def _select_account() -> Optional[Account]:
    """Choose an account and return a detached snapshot of it."""
    async with session_scope() as session:
        account = await pick_account(session)
        if account is None:
            return None
        return Account(
            id=account.id,
            name=account.name,
            status=account.status,
            cookie_jar=account.cookie_jar,
            proxy_id=account.proxy_id,
            max_concurrency=account.max_concurrency,
            available_balance=account.available_balance,
            running_jobs=account.running_jobs,
            last_used_at=account.last_used_at,
            cooldown_until=account.cooldown_until,
            consecutive_failures=account.consecutive_failures,
        )


async def _handle_account_error(account: Account, exc: Exception) -> None:
    async with session_scope() as session:
        real = await session.get(Account, account.id)
        if real is None:
            return
        await mark_failure(session, real, exc)
        if isinstance(exc, NotAuthenticated):
            await send_alert(f"ModelScope session expired for account '{real.name}'")
        elif isinstance(exc, InsufficientBalance):
            await send_alert(f"Account '{real.name}' ran out of magicubes")


async def _run_with_account(
    job_id: str,
    spec: JobSpec,
    account: Account,
    model,
    predict_type: str,
) -> None:
    """Submit, poll and download using one specific account."""

    async with session_scope() as session:
        real = await session.get(Account, account.id)
        if real is None:
            raise CivisionError("account disappeared", status=500)
        real.running_jobs += 1
        proxy = await assign_proxy(session, real)
        last_used = real.last_used_at
        real.last_used_at = utcnow()
        await session.flush()
        jar = stored_jar(real)

    try:
        client = CivisionClient(jar=jar, proxy=proxy_url(proxy))

        image_input = None
        if spec.input_reference_url:
            image_input = await _prepare_image_input(spec, client)

        await _sleep_until_next_submit(last_used)

        try:
            estimate = await client.estimate_quota(
                version_id=model.version_id,
                width=spec.width,
                height=spec.height,
                seconds=spec.seconds,
                predict_type=predict_type,
                prompt=spec.prompt,
                guidance_scale=spec.guidance_scale,
                num_inference_steps=spec.num_inference_steps,
                accelerate_lora=model.accelerate_lora or None,
                image_input=image_input,
            )
        except Exception:  # noqa: BLE001
            estimate = 0.0

        async with session_scope() as session:
            job = await session.get(VideoJob, job_id)
            if job:
                job.estimated_moli = estimate
                job.account_id = account.id
                job.attempts += 1

        balance = await client.balance()
        if estimate > 0 and balance["available"] < estimate:
            raise InsufficientBalance(
                f"account has {balance['available']} magicubes left, "
                f"the job needs {estimate}"
            )

        task_id = await client.submit_video(
            version_id=model.version_id,
            prompt=spec.prompt,
            negative_prompt=spec.negative_prompt,
            width=spec.width,
            height=spec.height,
            seconds=spec.seconds,
            guidance_scale=spec.guidance_scale,
            num_inference_steps=spec.num_inference_steps,
            predict_type=predict_type,
            image_input=image_input,
            accelerate_lora=model.accelerate_lora or None,
        )

        async with session_scope() as session:
            job = await session.get(VideoJob, job_id)
            if job:
                job.upstream_task_id = task_id

        log.info("job %s submitted as task %s on account %s", job_id, task_id, account.id)

        upstream_url = await _poll_until_settled(job_id, client, task_id)

        if upstream_url:
            await _store_result(job_id, upstream_url)

        async with session_scope() as session:
            real = await session.get(Account, account.id)
            if real:
                await mark_success(session, real)
                try:
                    await refresh_balance(session, real, proxy=proxy)
                except Exception:  # noqa: BLE001
                    pass

        async with session_scope() as session:
            job = await session.get(VideoJob, job_id)
            if job:
                job.actual_moli = job.estimated_moli
                moli_spent_total.inc(max(job.actual_moli, 0.0))

        jobs_succeeded_total.inc()
        log.info("job %s completed", job_id)
    finally:
        async with session_scope() as session:
            real = await session.get(Account, account.id)
            if real and real.running_jobs > 0:
                real.running_jobs -= 1


async def _resume_task(job_id: str, task_id: str) -> bool:
    """Pick up a task that was already submitted upstream.

    Returns True when the job reached a terminal state here.
    """
    log.info("resuming upstream task %s for job %s", task_id, job_id)
    account = await _select_account()
    if account is None:
        return False

    proxy = None
    async with session_scope() as session:
        real = await session.get(Account, account.id)
        if real is None:
            return False
        proxy = await assign_proxy(session, real)
        real.running_jobs += 1

    try:
        client = CivisionClient(jar=stored_jar(account), proxy=proxy_url(proxy))
        upstream_url = await _poll_until_settled(job_id, client, task_id)
        if upstream_url:
            await _store_result(job_id, upstream_url)
        async with session_scope() as session:
            job = await session.get(VideoJob, job_id)
            if job:
                job.actual_moli = job.estimated_moli
                moli_spent_total.inc(max(job.actual_moli, 0.0))
        jobs_succeeded_total.inc()
        await _log_usage(job_id, account, "completed", None)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("could not resume task %s: %s", task_id, exc)
        return False
    finally:
        async with session_scope() as session:
            real = await session.get(Account, account.id)
            if real and real.running_jobs > 0:
                real.running_jobs -= 1


async def _store_result(job_id: str, upstream_url: str) -> None:
    """Download the finished video and remember where it landed."""
    settings = get_settings()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{job_id}.mp4"
    await _download(upstream_url, settings.storage_dir / filename)
    async with session_scope() as session:
        job = await session.get(VideoJob, job_id)
        if job:
            job.local_path = filename


async def _poll_until_settled(job_id: str, client: CivisionClient, task_id: str) -> Optional[str]:
    """Poll the upstream task until it settles; return the video URL.

    Upstream reports a null percent while a task is processing, so progress
    is estimated from elapsed time. The estimate follows a curve that
    approaches but never reaches 100, since a real percentage only appears
    once the task finishes.
    """
    settings = get_settings()
    deadline = time.monotonic() + settings.poll_timeout_s
    started = time.monotonic()

    while time.monotonic() < deadline:
        status = await client.task_status(task_id)
        state = (status.get("status") or "").upper()
        percent = _extract_percent(status)
        if percent == 0:
            elapsed = time.monotonic() - started
            # Saturating curve: fast at first, then approaching the cap.
            percent = min(95, int(95 * (1 - 1 / (1 + elapsed / 45.0))))

        async with session_scope() as session:
            job = await session.get(VideoJob, job_id)
            if job is None:
                raise CivisionError("job vanished", status=500)
            job.progress = percent
            job.status = "in_progress"
            if state == "SUCCEED":
                job.progress = 100
                job.status = "completed"
                job.completed_at = utcnow()
                job.expires_at = utcnow() + dt.timedelta(days=get_settings().video_ttl_days)
                job.upstream_url = status.get("url")
                return status.get("url")
            if state in ("FAILED", "CANCELED"):
                job.status = "failed"
                job.error_code = "upstream_error"
                job.error_message = (
                    status.get("errorMsg") or f"upstream task ended with status {state}"
                )
                job.completed_at = utcnow()
                raise CivisionError(job.error_message, code="upstream_error", status=502)

        await asyncio.sleep(settings.poll_interval_s)

    raise CivisionError("upstream task did not finish in time", status=504)


def _extract_percent(status: dict) -> int:
    progress = status.get("progress")
    if isinstance(progress, dict):
        value = progress.get("percent")
        if isinstance(value, (int, float)):
            return max(0, min(100, int(value)))
    if isinstance(progress, (int, float)):
        return max(0, min(100, int(progress)))
    return 0


async def _fail(job_id: str, code: str, message: str) -> None:
    async with session_scope() as session:
        job = await session.get(VideoJob, job_id)
        if job:
            job.status = "failed"
            job.error_code = code
            job.error_message = message
            job.completed_at = utcnow()


async def _log_usage(
    job_id: str,
    account: Optional[Account],
    status: str,
    error: Optional[str],
) -> None:
    async with session_scope() as session:
        job = await session.get(VideoJob, job_id)
        if not job:
            return
        session.add(
            UsageLog(
                job_id=job.id,
                account_id=account.id if account else job.account_id,
                model=job.model,
                width=job.width,
                height=job.height,
                seconds=job.seconds,
                status=status,
                moli_estimated=job.estimated_moli,
                moli_actual=job.actual_moli,
                duration_ms=0,
                error=error,
            )
        )
