"""Tests for the request bodies the service sends upstream.

These use a mock transport, so nothing here reaches ModelScope and no
magicubes are spent. The bodies are compared against what the civision page
sends, which was read out of the page's own JavaScript bundle.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.civision.client import CivisionClient

# The upstream schema, as the page's bundle builds it.
#   imageInputFrontArgs: {image, stength}   <- upstream's own spelling
#   extraImageInputFrontArgs: {image, stength}
#   videoInferenceArgs: {accelerateLora}
#
# Note there is no "mode": the page never sets one, and upstream rejects an
# explicit mode=REF2VA with "MiniMax-H3 推理加速当前仅支持 FL2VA 模式".


def _capture(monkeypatch, calls: list) -> None:
    """Route every request through a mock transport and record the bodies."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content or b"{}")))
        return httpx.Response(
            200, json={"Code": 200, "Data": {"taskId": "t1", "quota": 34}}
        )

    transport = httpx.MockTransport(handler)

    def patched(self):
        return httpx.AsyncClient(
            headers=self._headers(),
            cookies=self.jar,
            timeout=10,
            follow_redirects=False,
            transport=transport,
        )

    monkeypatch.setattr(CivisionClient, "_client", patched)


@pytest.fixture
def calls(monkeypatch):
    recorded: list = []
    _capture(monkeypatch, recorded)
    return recorded


@pytest.fixture
def client():
    return CivisionClient(jar={"m_session_id": "x", "csrf_token": "tok"})


class TestSubmitVideoBody:
    async def test_text_to_video_body(self, client, calls):
        await client.submit_video(
            version_id=546,
            prompt="a cat",
            width=480,
            height=480,
            seconds=3,
            guidance_scale=7.5,
            num_inference_steps=50,
            predict_type="TXT_2_VIDEO",
        )
        path, body = calls[0]
        assert path == "/api/v1/muse/predict/task/video/submit"
        assert body["predictType"] == "TXT_2_VIDEO"
        assert body["promptArgs"] == {"prompt": "a cat", "negativePrompt": ""}
        assert body["basicDiffusionArgs"]["width"] == 480
        assert body["basicDiffusionArgs"]["totalSeconds"] == 3
        assert body["modelArgs"] == {
            "checkpointModelVersionId": 546,
            "loraArgs": [],
        }
        # A text-to-video job carries no image blocks.
        assert "imageInputFrontArgs" not in body
        assert "extraImageInputFrontArgs" not in body
        assert "videoInferenceArgs" not in body

    async def test_image_to_video_body(self, client, calls):
        await client.submit_video(
            version_id=648,
            prompt="a cat",
            width=480,
            height=480,
            seconds=3,
            predict_type="IMG_2_VIDEO",
            image_input="https://example.com/start.png",
        )
        _, body = calls[0]
        assert body["predictType"] == "IMG_2_VIDEO"
        assert body["imageInputFrontArgs"] == {
            "image": "https://example.com/start.png",
            "stength": 1,
        }
        # No end frame, so no second image block.
        assert "extraImageInputFrontArgs" not in body

    async def test_first_to_last_frame_body(self, client, calls):
        """Two images produce the two image blocks the page sends."""
        await client.submit_video(
            version_id=150867,
            prompt="a bird",
            width=768,
            height=1280,
            seconds=3,
            guidance_scale=4.0,
            num_inference_steps=4,
            predict_type="IMG_2_VIDEO",
            image_input="https://example.com/start.png",
            image_input_end="https://example.com/end.png",
            accelerate_lora=True,
        )
        _, body = calls[0]
        assert body["predictType"] == "IMG_2_VIDEO"
        assert body["imageInputFrontArgs"]["image"] == (
            "https://example.com/start.png"
        )
        assert body["extraImageInputFrontArgs"]["image"] == (
            "https://example.com/end.png"
        )
        assert body["videoInferenceArgs"] == {"accelerateLora": True}

    async def test_acceleration_block_has_no_mode(self, client, calls):
        """Upstream rejects an explicit mode; the page sends none."""
        await client.submit_video(
            version_id=150867,
            prompt="a bird",
            predict_type="IMG_2_VIDEO",
            image_input="https://example.com/start.png",
            accelerate_lora=True,
        )
        _, body = calls[0]
        assert body["videoInferenceArgs"] == {"accelerateLora": True}
        assert "mode" not in body["videoInferenceArgs"]
        assert "references" not in body["videoInferenceArgs"]


class TestEstimateQuotaBody:
    """The estimate body must mirror the submit body, field for field."""

    async def test_mirrors_the_submit_body(self, client, calls):
        await client.estimate_quota(
            version_id=150867,
            width=768,
            height=1280,
            seconds=3,
            predict_type="IMG_2_VIDEO",
            prompt="a bird",
            guidance_scale=4.0,
            num_inference_steps=4,
            accelerate_lora=True,
            image_input="https://example.com/start.png",
            image_input_end="https://example.com/end.png",
        )
        path, body = calls[0]
        assert path == "/openapi/v1/aigc/agentic/estimate-quota"
        request = body["video_predict_task_request"]
        assert request["predict_type"] == "IMG_2_VIDEO"
        assert request["image_input_front_args"] == {
            "image": "https://example.com/start.png",
            "stength": 1,
        }
        assert request["extra_image_input_front_args"] == {
            "image": "https://example.com/end.png",
            "stength": 1,
        }
        assert request["video_inference_args"] == {"accelerate_lora": True}

    async def test_single_image_has_no_end_block(self, client, calls):
        await client.estimate_quota(
            version_id=648,
            width=480,
            height=480,
            seconds=3,
            predict_type="IMG_2_VIDEO",
            image_input="https://example.com/start.png",
        )
        _, body = calls[0]
        request = body["video_predict_task_request"]
        assert "image_input_front_args" in request
        assert "extra_image_input_front_args" not in request

    async def test_text_to_video_has_no_image_blocks(self, client, calls):
        await client.estimate_quota(
            version_id=546,
            width=480,
            height=480,
            seconds=3,
            predict_type="TXT_2_VIDEO",
        )
        _, body = calls[0]
        request = body["video_predict_task_request"]
        assert "image_input_front_args" not in request
        assert "extra_image_input_front_args" not in request
