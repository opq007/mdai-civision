"""OpenAI-compatible request/response schemas.

Field names and enum values follow the official OpenAI OpenAPI spec
(v2.3.0, `VideoResource` / `CreateVideoJsonBody`).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

VideoStatus = Literal["queued", "in_progress", "completed", "failed"]

OPENAI_SECONDS = ("4", "8", "12")
OPENAI_SIZES = ("720x1280", "1280x720", "1024x1792", "1792x1024")


class ImageRefParam(BaseModel):
    image_url: str | None = None
    file_id: str | None = None


class CreateVideoRequest(BaseModel):
    model: str = Field(default="sora-2")
    prompt: str = Field(min_length=1, max_length=32000)
    input_reference: ImageRefParam | None = None
    # Not part of the OpenAI spec: the end frame for a first-to-last-frame
    # job. civision's MiniMax-H3 takes two images, and the page sends the
    # second one alongside the first.
    input_reference_end: ImageRefParam | None = None
    seconds: str | None = None
    size: str | None = None
    # OpenAI also accepts these on the multipart form; harmless extras.
    negative_prompt: str | None = None
    guidance_scale: float | None = None
    num_inference_steps: int | None = None


class VideoResource(BaseModel):
    id: str
    object: Literal["video"] = "video"
    model: str
    status: VideoStatus
    progress: int
    created_at: int
    completed_at: int | None = None
    expires_at: int | None = None
    prompt: str | None = None
    size: str | None = None
    seconds: str | None = None
    remixed_from_video_id: str | None = None
    error: dict[str, Any] | None = None


class VideoList(BaseModel):
    object: Literal["list"] = "list"
    data: list[VideoResource]


class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str = "civision"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelCard]


class ErrorDetail(BaseModel):
    message: str
    type: str
    code: str | None = None
    param: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
