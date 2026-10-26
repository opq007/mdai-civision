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

    async def test_acceleration_off_is_sent_explicitly(self, client, calls):
        """The page sends the flag when the switch is off, not nothing.

        `None` (omit the block) and `False` (send it, disabled) are
        different requests, and only the first one is legal for the Wan
        models.
        """
        await client.submit_video(
            version_id=150867,
            prompt="a bird",
            num_inference_steps=20,
            predict_type="IMG_2_VIDEO",
            image_input="https://example.com/start.png",
            accelerate_lora=False,
        )
        _, body = calls[0]
        assert body["videoInferenceArgs"] == {"accelerateLora": False}
        assert body["basicDiffusionArgs"]["numInferenceSteps"] == 20

    async def test_no_acceleration_block_for_the_wan_models(self, client, calls):
        """The Wan models reject a videoInferenceArgs block outright."""
        await client.submit_video(
            version_id=648,
            prompt="a bird",
            num_inference_steps=50,
            predict_type="IMG_2_VIDEO",
            image_input="https://example.com/start.png",
            accelerate_lora=None,
        )
        _, body = calls[0]
        assert "videoInferenceArgs" not in body


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


class TestModelVariantsReachTheWire:
    """Each public model name must produce its own upstream parameters.

    The two MiniMax-H3 names are the same upstream model with the civision
    page's acceleration switch on and off. They differ in the step count
    and in the acceleration flag, and both differences have to survive all
    the way into the request body.
    """

    @pytest.fixture
    def catalog(self):
        from app.civision.mapping import DEFAULT_ALIASES, build_catalog

        from tests.test_mapping import SAMPLE_CATALOG

        return build_catalog(SAMPLE_CATALOG, dict(DEFAULT_ALIASES))

    async def _body_for(self, client, calls, model):
        await client.submit_video(
            version_id=model.version_id,
            prompt="a bird",
            width=model.default_width,
            height=model.default_height,
            seconds=3,
            guidance_scale=model.default_guidance_scale,
            num_inference_steps=model.default_steps,
            predict_type="TXT_2_VIDEO",
            accelerate_lora=model.accelerate_lora,
        )
        return calls[-1][1]

    async def test_turbo_sends_four_steps_and_acceleration_on(
        self, client, calls, catalog
    ):
        from app.civision.mapping import resolve_model

        model = resolve_model("minimax-h3-turbo", catalog)
        body = await self._body_for(client, calls, model)
        assert body["basicDiffusionArgs"]["numInferenceSteps"] == 4
        assert body["videoInferenceArgs"] == {"accelerateLora": True}

    async def test_plain_minimax_sends_twenty_steps_and_acceleration_off(
        self, client, calls, catalog
    ):
        from app.civision.mapping import resolve_model

        model = resolve_model("minimax-h3", catalog)
        body = await self._body_for(client, calls, model)
        assert body["basicDiffusionArgs"]["numInferenceSteps"] == 20
        assert body["videoInferenceArgs"] == {"accelerateLora": False}

    async def test_the_two_variants_differ_on_the_wire(self, client, calls, catalog):
        from app.civision.mapping import resolve_model

        turbo = await self._body_for(
            client, calls, resolve_model("minimax-h3-turbo", catalog)
        )
        plain = await self._body_for(
            client, calls, resolve_model("minimax-h3", catalog)
        )
        assert turbo != plain
        assert (
            turbo["basicDiffusionArgs"]["numInferenceSteps"]
            != plain["basicDiffusionArgs"]["numInferenceSteps"]
        )
        assert (
            turbo["videoInferenceArgs"]["accelerateLora"]
            != plain["videoInferenceArgs"]["accelerateLora"]
        )

    async def test_sora_aliases_reach_the_right_variant(
        self, client, calls, catalog
    ):
        from app.civision.mapping import resolve_model

        sora = await self._body_for(
            client, calls, resolve_model("sora-2", catalog)
        )
        assert sora["basicDiffusionArgs"]["numInferenceSteps"] == 4
        assert sora["videoInferenceArgs"] == {"accelerateLora": True}

        pro = await self._body_for(
            client, calls, resolve_model("sora-2-pro", catalog)
        )
        assert pro["basicDiffusionArgs"]["numInferenceSteps"] == 20
        assert pro["videoInferenceArgs"] == {"accelerateLora": False}

    async def test_wan_models_send_no_acceleration_block(
        self, client, calls, catalog
    ):
        from app.civision.mapping import resolve_model

        model = resolve_model("wan2.1-t2v-1.3b", catalog)
        body = await self._body_for(client, calls, model)
        assert body["basicDiffusionArgs"]["numInferenceSteps"] == 50
        assert "videoInferenceArgs" not in body


class TestSessionProbeDistinguishesNetworkFromAuth:
    """A network failure must not be reported as a rejected session.

    `check_session` feeds `validate_account`, which expires an account when
    the probe comes back unauthenticated. If a DNS blip counted as
    "unauthenticated", a temporary outage would take the account out of the
    pool for good.
    """

    async def test_unreachable_upstream_is_flagged(self, monkeypatch):
        from app.civision.client import CivisionClient, UpstreamUnavailable

        client = CivisionClient(jar={"m_session_id": "x", "h_uid": "y"})

        async def boom(self):
            raise UpstreamUnavailable("cannot reach ModelScope: DNS failure")

        monkeypatch.setattr(CivisionClient, "whoami", boom)

        info = await client.check_session()
        assert info.authenticated is False
        assert info.unreachable is True
        assert info.transient is True
        assert "cannot reach ModelScope" in info.reason

    async def test_a_rejected_session_is_not_flagged_unreachable(self, monkeypatch):
        from app.civision.client import CivisionClient, NotAuthenticated

        client = CivisionClient(jar={"m_session_id": "x", "h_uid": "y"})

        async def rejected(self):
            raise NotAuthenticated()

        monkeypatch.setattr(CivisionClient, "whoami", rejected)

        info = await client.check_session()
        assert info.authenticated is False
        assert info.unreachable is False
        assert info.transient is False

    async def test_a_rejected_session_reason_includes_the_diagnostic(
        self, monkeypatch
    ):
        from app.civision.client import CivisionClient, NotAuthenticated

        client = CivisionClient(jar={"m_session_id": "x", "h_uid": "y"})

        async def rejected(self):
            raise NotAuthenticated(
                "user not logged in",
                upstream_code=10010101003,
                request_id="rid-3",
            )

        monkeypatch.setattr(CivisionClient, "whoami", rejected)

        info = await client.check_session()
        assert info.transient is False
        assert "HTTP 401" in info.reason
        assert "upstream_code=10010101003" in info.reason
        assert "request_id=rid-3" in info.reason

    async def test_a_rate_limited_probe_is_transient(self, monkeypatch):
        from app.civision.client import CivisionClient, RateLimited

        client = CivisionClient(jar={"m_session_id": "x", "h_uid": "y"})

        async def limited(self):
            raise RateLimited("Request frequency too fast")

        monkeypatch.setattr(CivisionClient, "whoami", limited)

        info = await client.check_session()
        assert info.authenticated is False
        assert info.transient is True
        assert info.unreachable is False
        assert "rate limited" in info.reason
        assert "frequency" in info.reason.lower()

    async def test_a_forbidden_probe_is_transient(self, monkeypatch):
        from app.civision.client import CivisionClient, Forbidden

        client = CivisionClient(jar={"m_session_id": "x", "h_uid": "y"})

        async def blocked(self):
            raise Forbidden("blocked by risk control")

        monkeypatch.setattr(CivisionClient, "whoami", blocked)

        info = await client.check_session()
        assert info.authenticated is False
        assert info.transient is True
        assert info.unreachable is False
        assert "blocked" in info.reason

    async def test_an_unknown_business_error_is_transient(self, monkeypatch):
        from app.civision.client import CivisionClient, CivisionError

        client = CivisionClient(jar={"m_session_id": "x", "h_uid": "y"})

        async def broken(self):
            raise CivisionError("weird upstream state", code="upstream_error", status=400)

        monkeypatch.setattr(CivisionClient, "whoami", broken)

        info = await client.check_session()
        assert info.authenticated is False
        assert info.transient is True
        assert "weird upstream state" in info.reason


class TestHttpStatusMapping:
    """Status codes must be classified before the body is parsed."""

    def test_401_with_an_html_body_is_not_authenticated(self):
        from app.civision.client import CivisionClient, NotAuthenticated

        response = httpx.Response(401, text="<html>login required</html>")
        with pytest.raises(NotAuthenticated):
            CivisionClient._unwrap(response)

    def test_403_with_an_html_body_is_forbidden(self):
        from app.civision.client import CivisionClient, Forbidden

        response = httpx.Response(403, text="<html>blocked by risk control</html>")
        with pytest.raises(Forbidden):
            CivisionClient._unwrap(response)

    def test_429_with_a_json_body_is_rate_limited(self):
        from app.civision.client import CivisionClient, RateLimited

        response = httpx.Response(429, json={"message": "slow down"})
        with pytest.raises(RateLimited):
            CivisionClient._unwrap(response)

    def test_a_frequency_business_error_is_rate_limited(self):
        from app.civision.client import CivisionClient, RateLimited

        response = httpx.Response(
            200,
            json={
                "success": False,
                "code": 50001,
                "message": "Request frequency too fast",
            },
        )
        with pytest.raises(RateLimited):
            CivisionClient._unwrap(response)

    def test_invalid_authentication_without_success_is_not_authenticated(self):
        from app.civision.client import CivisionClient, NotAuthenticated

        response = httpx.Response(
            200,
            json={"code": "InvalidAuthentication", "message": "user not logged in"},
        )
        with pytest.raises(NotAuthenticated) as exc:
            CivisionClient._unwrap(response)
        assert exc.value.upstream_code == "InvalidAuthentication"
        assert "user not logged in" in exc.value.message

    def test_http_errors_carry_status_code_and_request_id(self):
        from app.civision.client import CivisionClient, Forbidden

        response = httpx.Response(
            403,
            json={
                "code": "AccessDenied",
                "message": "blocked",
                "RequestId": "rid-1",
            },
        )
        with pytest.raises(Forbidden) as exc:
            CivisionClient._unwrap(response)
        assert exc.value.status == 403
        assert exc.value.upstream_code == "AccessDenied"
        assert exc.value.request_id == "rid-1"
        assert "HTTP 403" in exc.value.diagnostic()
        assert "request_id=rid-1" in exc.value.diagnostic()

    def test_api_code_failures_carry_the_request_id(self):
        from app.civision.client import CivisionClient, NotAuthenticated

        response = httpx.Response(
            200,
            json={
                "Code": 10010101003,
                "Message": "user not logged in",
                "RequestId": "rid-2",
            },
        )
        with pytest.raises(NotAuthenticated) as exc:
            CivisionClient._unwrap(response)
        assert exc.value.upstream_code == 10010101003
        assert exc.value.request_id == "rid-2"
