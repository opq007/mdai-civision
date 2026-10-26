"""OpenAI-compatible /v1/videos endpoints."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Form, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
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

# The download route is mounted separately, without the API-key dependency,
# so that a video can be fetched by URL alone: `GET /v1/videos/{id}/content`
# is what a browser <video> tag, a player or a third-party page points at,
# and those callers have no API key. The job id is an unguessable token, so
# the id itself is the capability.
public_router = APIRouter()


@router.post(
    "/videos",
    response_model=VideoResource,
    responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse},
                429: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
async def create_video(
    request: Request,
    session: AsyncSession = Depends(get_session),
    model: str | None = Form(None),
    prompt: str | None = Form(None),
    seconds: str | None = Form(None),
    size: str | None = Form(None),
    image: str | None = Form(None),
    duration: str | None = Form(None),
    negative_prompt: str | None = Form(None),
    guidance_scale: float | None = Form(None),
    num_inference_steps: int | None = Form(None),
) -> VideoResource:
    """Create a video generation job.

    Returns immediately with `status: queued`; the worker picks it up.

    JSON is what OpenAI documents and what a client sends directly. n8n's
    video node sends `multipart/form-data` instead, with the reference image
    as a file part named `input_reference`, and new-api's OpenAI video channel
    forwards that body unchanged.
    """
    content_type = request.headers.get("content-type", "")
    uploads: dict[str, UploadFile] = {}
    if content_type.startswith("application/json") or not content_type:
        raw = await request.json()
        if not isinstance(raw, dict):
            raise openai_error(400, "request body must be an object", "invalid_request_error")
        body = _parse_create_body(raw)
    else:
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "seconds": seconds,
            "size": size,
            "duration": duration,
            "image": image,
            "negative_prompt": negative_prompt,
            "guidance_scale": guidance_scale,
            "num_inference_steps": num_inference_steps,
        }
        # A form field and a file can share the name `input_reference`. The file
        # is the image n8n uploaded; the field, when it is text, is a URL.
        # Reading the file parts from the parsed form avoids FastAPI also
        # binding that same part to the string parameter above.
        form = await request.form()
        uploads = _reference_uploads(form)
        for name in ("input_reference", "input_reference_end"):
            if name in uploads:
                continue
            value = form.get(name)
            if isinstance(value, str) and value.strip():
                payload[name] = value
        body = _parse_create_body({key: value for key, value in payload.items() if value is not None})

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

    has_start = body.input_reference is not None or "input_reference" in uploads
    has_end = body.input_reference_end is not None or "input_reference_end" in uploads
    if has_end:
        if not model.supports_flf2video:
            raise openai_error(
                400,
                f"model {model.requested!r} does not support a first-to-last-frame "
                "job; pass only input_reference",
                "invalid_request_error",
                param="input_reference_end",
            )
        if not has_start:
            raise openai_error(
                400,
                "input_reference_end needs a first frame; pass input_reference too",
                "invalid_request_error",
                param="input_reference",
            )
    elif has_start:
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
    if body.input_reference_end and body.input_reference_end.image_url:
        job.input_reference_end_url = body.input_reference_end.image_url
    if "input_reference" in uploads:
        job.input_reference_url = await _store_reference(
            uploads["input_reference"], settings=get_settings(), job_id=job_id, slot="start"
        )
    if "input_reference_end" in uploads:
        job.input_reference_end_url = await _store_reference(
            uploads["input_reference_end"], settings=get_settings(), job_id=job_id, slot="end"
        )

    session.add(job)
    await session.flush()
    await enqueue_job(session, job)
    return job_to_resource(job)


def _reference_uploads(form) -> dict[str, Any]:
    # A file part exposes `filename`; a text part is a plain string. Checking
    # the attribute rather than the class avoids matching the wrong copy of
    # Starlette's UploadFile when the process imported it twice.
    found: dict[str, Any] = {}
    for name in ("input_reference", "input_reference_end"):
        value = form.get(name)
        if getattr(value, "filename", None):
            found[name] = value
    return found


def _parse_create_body(payload: dict[str, Any]) -> CreateVideoRequest:
    try:
        return CreateVideoRequest.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = [str(part) for part in first.get("loc", [])]
        raise openai_error(
            400,
            str(first.get("msg", "invalid request")),
            "invalid_request_error",
            param=".".join(loc) or None,
        ) from exc


async def _store_reference(upload: UploadFile, *, settings, job_id: str, slot: str) -> str:
    """Keep an uploaded reference image where the worker can read it.

    The worker treats a non-URL value as a local path and uploads those bytes
    to ModelScope. Writing the file here means a multipart request does not
    have to be turned into a base64 string first.
    """
    name = (upload.filename or "reference.png").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else "png"
    if suffix not in {"png", "jpg", "jpeg", "webp", "gif"}:
        suffix = "png"
    relative = f"references/{job_id}-{slot}.{suffix}"
    path = settings.storage_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(await upload.read())
    return str(path)


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


@public_router.get("/videos/{video_id}", response_model=VideoResource)
async def retrieve_video(
    video_id: str, session: AsyncSession = Depends(get_session)
) -> VideoResource:
    """Report the state of one job.

    Public: the job id is the capability, so a caller that only knows the
    id can poll for the result without holding an API key.
    """
    job = await session.get(VideoJob, video_id)
    if job is None:
        raise openai_error(404, f"video {video_id!r} not found", "invalid_request_error")
    return job_to_resource(job)


@public_router.get("/videos/{video_id}/content")
async def retrieve_video_content(
    video_id: str,
    request: Request,
    download: bool = False,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Stream the generated MP4.

    Public: this URL is what gets handed to a browser, a player or a
    third-party page, and those callers have no API key to present. The
    unguessable job id is the credential.

    The clip is served `inline` so it can be used directly as a `<video>`
    source; pass `?download=true` to force a file download instead.
    """
    job = await session.get(VideoJob, video_id)
    if job is None:
        raise openai_error(404, f"video {video_id!r} not found", "invalid_request_error")
    if job.status != "completed":
        raise openai_error(
            409,
            f"video {video_id!r} is not ready (status: {job.status})",
            "invalid_request_error",
        )

    disposition = "attachment" if download else "inline"
    filename = f"{video_id}.mp4"

    settings = get_settings()
    if job.local_path:
        path = settings.storage_dir / job.local_path
        if path.exists():
            # `FileResponse` streams the file and answers `Range` requests, so
            # a browser <video> player can seek without pulling the whole
            # clip into memory first.
            return FileResponse(
                path,
                media_type="video/mp4",
                filename=filename,
                content_disposition_type=disposition,
            )

    if job.upstream_url:
        import httpx

        # The local copy has been cleaned up, so the clip is fetched from
        # the upstream CDN. A `Range` header is passed on, so a player can
        # still seek without pulling the whole file.
        range_header = request.headers.get("range")
        headers = {"Range": range_header} if range_header else {}

        async with httpx.AsyncClient(
            timeout=120.0,
            follow_redirects=True,
            trust_env=settings.trust_env_proxy,
        ) as c:
            upstream = await c.get(job.upstream_url, headers=headers)
            if upstream.status_code in (200, 206):
                content = upstream.content
                return Response(
                    content=content,
                    status_code=upstream.status_code,
                    media_type="video/mp4",
                    headers={
                        "Content-Disposition": f'{disposition}; filename="{filename}"',
                        "Content-Length": str(len(content)),
                        **(
                            {"Content-Range": upstream.headers["content-range"]}
                            if "content-range" in upstream.headers
                            else {}
                        ),
                        **(
                            {"Accept-Ranges": "bytes"}
                            if upstream.headers.get("accept-ranges")
                            else {}
                        ),
                    },
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
