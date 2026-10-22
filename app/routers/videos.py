"""OpenAI-compatible /v1/videos endpoints."""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import VideoJob, utcnow
from app.schemas import (
    CreateVideoRequest,
    ErrorResponse,
    ModelCard,
    ModelList,
    VideoList,
    VideoResource,
)
from app.services.catalog import catalog_service
from app.services.errors import openai_error
from app.services.queue import enqueue_job, job_to_resource

router = APIRouter()


@router.post(
    "/videos",
    response_model=VideoResource,
    responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse},
                429: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
async def create_video(
    request: Request,
    body: CreateVideoRequest,
    session: AsyncSession = Depends(get_session),
) -> VideoResource:
    """Create a video generation job.

    Returns immediately with `status: queued`; the worker picks it up.
    """
    catalog = await catalog_service.get(session)

    model = catalog.resolve(body.model)
    if model is None:
        raise openai_error(
            400,
            f"the model {body.model!r} does not exist. "
            f"Available models: {', '.join(sorted(m.requested for m in catalog.models))}",
            "invalid_request_error",
            param="model",
        )

    try:
        width, height = catalog.resolve_size(body.size, model)
        seconds = catalog.resolve_seconds(body.seconds, model)
    except ValueError as exc:
        raise openai_error(400, str(exc), "invalid_request_error") from exc

    if body.input_reference is not None:
        if not model.supports_image_input:
            raise openai_error(
                400,
                f"model {model.requested!r} does not support image input; "
                "use a text-to-video model",
                "invalid_request_error",
                param="input_reference",
            )
    elif not model.supports_txt2video:
        raise openai_error(
            400,
            f"model {model.requested!r} requires input_reference",
            "invalid_request_error",
            param="input_reference",
        )

    job_id = "video_" + uuid.uuid4().hex[:24]
    now = utcnow()
    job = VideoJob(
        id=job_id,
        model=body.model,
        prompt=body.prompt,
        negative_prompt=body.negative_prompt or "",
        seconds=str(seconds),
        # Record the size that was actually requested to the caller, and keep
        # the resolved dimensions in width/height.
        size=body.size or f"{width}x{height}",
        width=width,
        height=height,
        guidance_scale=body.guidance_scale or model.default_guidance_scale,
        num_inference_steps=body.num_inference_steps or model.default_steps,
        status="queued",
        progress=0,
        created_at=now,
        created_at_ts=int(now.timestamp()),
    )
    if body.input_reference and body.input_reference.image_url:
        job.input_reference_url = body.input_reference.image_url

    session.add(job)
    await session.flush()
    await enqueue_job(session, job)
    return job_to_resource(job)


@router.get("/videos/{video_id}", response_model=VideoResource)
async def retrieve_video(
    video_id: str, session: AsyncSession = Depends(get_session)
) -> VideoResource:
    job = await session.get(VideoJob, video_id)
    if job is None:
        raise openai_error(404, f"video {video_id!r} not found", "invalid_request_error")
    return job_to_resource(job)


@router.get("/videos", response_model=VideoList)
async def list_videos(
    limit: int = 20, session: AsyncSession = Depends(get_session)
) -> VideoList:
    jobs = (
        await session.execute(
            select(VideoJob).order_by(VideoJob.created_at_ts.desc()).limit(min(limit, 100))
        )
    ).scalars().all()
    return VideoList(data=[job_to_resource(j) for j in jobs])


@router.get("/videos/{video_id}/content")
async def retrieve_video_content(
    video_id: str, session: AsyncSession = Depends(get_session)
) -> Response:
    """Stream the generated MP4."""
    job = await session.get(VideoJob, video_id)
    if job is None:
        raise openai_error(404, f"video {video_id!r} not found", "invalid_request_error")
    if job.status != "completed":
        raise openai_error(
            409,
            f"video {video_id!r} is not ready (status: {job.status})",
            "invalid_request_error",
        )

    settings = get_settings()
    if job.local_path:
        path = settings.storage_dir / job.local_path
        if path.exists():
            return Response(
                content=path.read_bytes(),
                media_type="video/mp4",
                headers={"Content-Disposition": f'attachment; filename="{video_id}.mp4"'},
            )

    if job.upstream_url:
        import httpx

        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as c:
            upstream = await c.get(job.upstream_url)
            if upstream.status_code == 200:
                return Response(
                    content=upstream.content,
                    media_type="video/mp4",
                    headers={"Content-Disposition": f'attachment; filename="{video_id}.mp4"'},
                )

    raise openai_error(
        404,
        f"the video file for {video_id!r} is no longer available",
        "invalid_request_error",
    )


@router.delete("/videos/{video_id}")
async def delete_video(
    video_id: str, session: AsyncSession = Depends(get_session)
) -> JSONResponse:
    job = await session.get(VideoJob, video_id)
    if job is None:
        raise openai_error(404, f"video {video_id!r} not found", "invalid_request_error")

    settings = get_settings()
    if job.local_path:
        path = settings.storage_dir / job.local_path
        if path.exists():
            path.unlink(missing_ok=True)

    await session.delete(job)
    return JSONResponse({"id": video_id, "object": "video.deleted", "deleted": True})


@router.get("/models", response_model=ModelList)
async def list_models(session: AsyncSession = Depends(get_session)) -> ModelList:
    catalog = await catalog_service.get(session)
    created = int(dt.datetime.now(dt.timezone.utc).timestamp())
    return ModelList(
        data=[
            ModelCard(id=m.requested, created=created, owned_by="civision")
            for m in catalog.models
        ]
    )
