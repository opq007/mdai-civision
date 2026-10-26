"""OpenAI-compatible request/response schemas.

Field names and enum values follow the official OpenAI OpenAPI spec
(v2.3.0, `VideoResource` / `CreateVideoJsonBody`).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

VideoStatus = Literal["queued", "in_progress", "completed", "failed"]

OPENAI_SECONDS = ("4", "8", "12")
OPENAI_SIZES = ("720x1280", "1280x720", "1024x1792", "1792x1024")


class ImageRefParam(BaseModel):
    image_url: str | None = None
    file_id: str | None = None

    @classmethod
    def coerce(cls, value: Any, *, field_name: str) -> "ImageRefParam | None":
        """Accept both the object form and a bare image string.

        OpenAI-style clients send ``{"image_url": "..."}`` (or ``file_id``).
        Gateways such as new-api declare the same field as a string and forward
        the URL or ``data:`` URL as-is. Both have to land as one image.
        """
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            return cls(image_url=text)
        if isinstance(value, dict):
            return cls.model_validate(value)
        raise ValueError(
            f"{field_name} must be an image URL string or an object "
            "with image_url or file_id"
        )


class CreateVideoRequest(BaseModel):
    model: str = Field(default="sora-2")
    prompt: str = Field(min_length=1, max_length=32000)
    input_reference: ImageRefParam | None = None
    # Not part of the OpenAI spec: the end frame for a first-to-last-frame
    # job. civision's MiniMax-H3 takes two images, and the page sends the
    # second one alongside the first.
    input_reference_end: ImageRefParam | None = None
    seconds: str | None = None
    # new-api accepts `duration` as an integer or a numeric string, alongside
    # OpenAI's `seconds`.
    duration: int | str | None = None
    size: str | None = None
    # new-api's OpenAI video channel names the reference `image` (one string)
    # or `images` (a list). They are aliases of the two frames above, not a
    # third image slot.
    image: str | None = None
    images: list[str] | None = None

    @field_validator("input_reference", "input_reference_end", mode="before")
    @classmethod
    def _coerce_image_ref(cls, value: Any, info) -> ImageRefParam | None:
        return ImageRefParam.coerce(value, field_name=info.field_name)

    @field_validator("seconds", mode="before")
    @classmethod
    def _seconds_as_text(cls, value: Any) -> str | None:
        # new-api's openai_video decoder turns a multipart `seconds` into a
        # JSON number before forwarding. OpenAI itself sends the string.
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, bool):
            raise ValueError("seconds must be a string or a number")
        if isinstance(value, int):
            return str(value)
        raise ValueError("seconds must be a string or a number")

    def model_post_init(self, context: Any) -> None:
        if self.seconds is None and self.duration is not None:
            self.seconds = str(self.duration).strip() or None
        frames = [item.strip() for item in (self.images or []) if item and item.strip()]
        if self.image and self.image.strip():
            frames.insert(0, self.image.strip())
        if frames:
            if self.input_reference is None:
                self.input_reference = ImageRefParam(image_url=frames[0])
            if len(frames) > 1 and self.input_reference_end is None:
                self.input_reference_end = ImageRefParam(image_url=frames[1])

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
