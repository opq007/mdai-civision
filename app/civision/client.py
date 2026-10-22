"""Client for ModelScope Civision's web endpoints.

Everything in here was verified against the live site (see SPEC.md §1).
Two things are worth remembering:

* The session is a **cookie** session, and the login state lives in
  `m_session_id` + `h_uid`. `csrf_session` / `csrf_token` are handed out to
  anonymous visitors too, so they prove nothing on their own.
* Writes need an `X-CSRF-TOKEN` header carrying the URL-decoded
  `csrf_token` cookie.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx

from app.config import get_settings

BASE = get_settings().civision_base

# Cookies that must be present for the session to be considered logged in.
SESSION_COOKIES = ("m_session_id", "h_uid")

# Cookies worth keeping when persisting a session.
KEEP_COOKIES = (
    "m_session_id",
    "h_uid",
    "csrf_session",
    "csrf_token",
    "acw_tc",
    "auth_sid",
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class CivisionError(Exception):
    """Base class for upstream failures."""

    def __init__(self, message: str, *, code: str = "internal_error", status: int = 500):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class NotAuthenticated(CivisionError):
    def __init__(self, message: str = "not signed in to ModelScope"):
        super().__init__(message, code="not_authenticated", status=401)


class InsufficientBalance(CivisionError):
    def __init__(self, message: str = "not enough magicubes"):
        super().__init__(message, code="insufficient_quota", status=429)


class RateLimited(CivisionError):
    def __init__(self, message: str = "upstream rate limit hit"):
        super().__init__(message, code="rate_limit_exceeded", status=429)


class InvalidRequest(CivisionError):
    def __init__(self, message: str):
        super().__init__(message, code="invalid_request_error", status=400)


class UpstreamUnavailable(CivisionError):
    def __init__(self, message: str = "upstream service unavailable"):
        super().__init__(message, code="server_error", status=503)


def parse_cookie_header(raw: str) -> dict[str, str]:
    """Parse a pasted Cookie header into a mapping.

    Tolerates the `#HttpOnly_` prefix DevTools adds, blank lines, and
    `Cookie: ` prefixes.
    """
    jar: dict[str, str] = {}
    if not raw:
        return jar
    for line in raw.replace("\r", "").split("\n"):
        line = line.strip()
        if not line or line.lower().startswith("cookie:"):
            line = line.split(":", 1)[1].strip() if ":" in line else ""
        if not line:
            continue
        for part in line.split(";"):
            part = part.strip()
            if part.startswith("#HttpOnly_"):
                part = part[len("#HttpOnly_"):]
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            name = name.strip()
            if name:
                jar[name] = value.strip()
    return jar


def cookie_header(jar: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in jar.items())


@dataclass
class SessionInfo:
    """What we know about a session after probing it."""

    authenticated: bool
    username: str = ""
    cookie_jar: dict[str, str] = field(default_factory=dict)
    reason: str = ""
    # True when the probe failed because ModelScope could not be reached,
    # rather than because the session was rejected. A network blip must not
    # mark the account expired.
    unreachable: bool = False


class CivisionClient:
    """Thin async wrapper over the endpoints we actually use."""

    def __init__(
        self,
        jar: dict[str, str] | None = None,
        proxy: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.jar: dict[str, str] = dict(jar or {})
        s = get_settings()
        self._proxy = proxy
        self._timeout = timeout or s.civision_timeout_s

    # -- plumbing ---------------------------------------------------------

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        h = {
            "accept": "application/json",
            "referer": f"{BASE}/aigc/videoGeneration",
            "origin": BASE,
            "user-agent": USER_AGENT,
            "X-ModelScope-Accept-Language": "en-US",
        }
        if json_body:
            h["content-type"] = "application/json"
        token = self.jar.get("csrf_token")
        if token:
            h["X-CSRF-TOKEN"] = unquote(token)
        return h

    def _client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "headers": self._headers(),
            "cookies": self.jar,
            "timeout": self._timeout,
            "follow_redirects": False,
        }
        if self._proxy:
            kwargs["proxy"] = self._proxy
        return httpx.AsyncClient(**kwargs)

    async def _request(
        self, method: str, path: str, *, json_body: Any = None, params: dict | None = None
    ) -> Any:
        async with self._client() as c:
            try:
                r = await c.request(
                    method, BASE + path, json=json_body, params=params
                )
            except httpx.HTTPError as exc:
                raise UpstreamUnavailable(f"cannot reach ModelScope: {exc}") from exc

        # ModelScope re-issues csrf_token on every response; keep it fresh.
        for value in r.cookies.values():
            if "csrf_token" in r.cookies:
                self.jar["csrf_token"] = r.cookies["csrf_token"]
        for name in ("acw_tc", "csrf_session"):
            if name in r.cookies:
                self.jar[name] = r.cookies[name]

        return self._unwrap(r)

    @classmethod
    def _unwrap(cls, r: httpx.Response) -> Any:
        """ModelScope often returns HTTP 200 with a failure inside the body.

        Three envelope shapes are in play:
          /api/v1/*       -> {"Code": 200, "Data": {...}, "Success": true}
          /openapi/v1/*   -> {"success": true, "data": {...}}
          legacy          -> {"Code": ..., "Data": {"code": ..., "success": ...}}
        """
        try:
            body = r.json()
        except (json.JSONDecodeError, ValueError):
            if r.status_code >= 500:
                raise UpstreamUnavailable(f"upstream returned {r.status_code}")
            raise CivisionError(
                f"unexpected upstream response ({r.status_code})", status=502
            )

        if r.status_code == 401:
            raise NotAuthenticated()
        if r.status_code == 403:
            raise CivisionError("upstream denied the request", code="forbidden", status=403)
        if r.status_code >= 500:
            raise UpstreamUnavailable(f"upstream returned {r.status_code}")

        # openapi envelope
        if isinstance(body, dict) and "success" in body:
            if body.get("success") is False:
                raise cls._business_error(body)
            if "data" in body:
                return body.get("data")
            return body

        # api/v1 envelope
        if isinstance(body, dict) and "Code" in body:
            code = body.get("Code")
            if code not in (200, 0, None):
                msg = body.get("Message") or body.get("message") or "request rejected"
                raise cls._map_api_code(code, msg, body)
            data = body.get("Data")
            # Some /api/v1 endpoints nest the real result one level deeper.
            if isinstance(data, dict) and "code" in data and "success" in data:
                if data.get("success") is False:
                    raise cls._business_error(data)
                return data.get("data")
            return data

        return body

    @staticmethod
    def _business_error(data: dict) -> CivisionError:
        code = data.get("code")
        msg = data.get("message") or "upstream rejected the request"
        if code == 50001 or "frequency" in str(msg).lower():
            return RateLimited(str(msg))
        if code == 99999:
            return InvalidRequest(str(msg))
        if code == 40000:
            return InvalidRequest(str(msg))
        return CivisionError(str(msg), code="upstream_error", status=400)

    @staticmethod
    def _map_api_code(code: Any, msg: str, body: dict) -> CivisionError:
        if code == 10010101003:
            raise NotAuthenticated(msg)
        if code == 10019901001:
            raise CivisionError(msg, code="forbidden", status=403)
        return CivisionError(msg, code="upstream_error", status=400)

    # -- endpoints --------------------------------------------------------

    async def whoami(self) -> dict:
        """Return the logged-in user's info, or raise NotAuthenticated."""
        return await self._request("GET", "/api/v1/users/info")

    async def check_session(self) -> SessionInfo:
        """Probe whether the current jar is a real, logged-in session."""
        jar = self.jar
        missing = [c for c in SESSION_COOKIES if c not in jar]
        if missing:
            return SessionInfo(
                authenticated=False,
                cookie_jar=jar,
                reason=f"cookie jar is missing {', '.join(missing)}",
            )
        try:
            info = await self.whoami()
        except NotAuthenticated:
            return SessionInfo(
                authenticated=False, cookie_jar=jar, reason="ModelScope rejected the session"
            )
        except UpstreamUnavailable as exc:
            # The network is down, not the session. Reporting this as
            # "not authenticated" would mark the account expired and take it
            # out of the pool for good.
            return SessionInfo(
                authenticated=False,
                cookie_jar=jar,
                reason=f"could not reach ModelScope: {exc.message}",
                unreachable=True,
            )
        except CivisionError as exc:
            return SessionInfo(
                authenticated=False, cookie_jar=jar, reason=exc.message
            )
        data = info.get("Data", info) if isinstance(info, dict) else {}
        username = (
            data.get("UserName")
            or data.get("Name")
            or data.get("NickName")
            or ""
        )
        return SessionInfo(
            authenticated=True, username=str(username), cookie_jar=self.jar
        )

    async def balance(self) -> dict:
        data = await self._request("GET", "/openapi/v1/magicubes/balance")
        if isinstance(data, dict):
            return {
                "total": float(data.get("total_balance", 0) or 0),
                "available": float(data.get("available_balance", 0) or 0),
                "frozen": float(data.get("frozen_amount", 0) or 0),
            }
        return {"total": 0.0, "available": 0.0, "frozen": 0.0}

    async def spend_rates(self) -> list[dict]:
        data = await self._request("GET", "/openapi/v1/magicubes/spend/rates")
        if isinstance(data, dict):
            return list(data.get("rates") or [])
        return []

    async def transactions(self, page: int = 1, page_size: int = 20) -> list[dict]:
        data = await self._request(
            "GET",
            "/openapi/v1/magicubes/transactions",
            params={"page": page, "page_size": page_size},
        )
        if isinstance(data, dict):
            for key in ("transactions", "list", "items", "records"):
                if isinstance(data.get(key), list):
                    return data[key]
            return []
        return data or []

    async def model_catalog(self) -> list[dict]:
        """The live list of video models civision currently offers."""
        data = await self._request(
            "GET", "/api/v1/muse/predict/unauth/defaultTemplateV2"
        )
        if not isinstance(data, dict):
            return []
        video = (data.get("VIDEO") or {})
        return list(video.get("supportSDVersionList") or [])

    async def estimate_quota(
        self,
        *,
        version_id: int,
        width: int,
        height: int,
        seconds: int,
        predict_type: str = "TXT_2_VIDEO",
        prompt: str = "",
        guidance_scale: float = 7.5,
        num_inference_steps: int = 50,
        accelerate_lora: bool | None = None,
        image_input: str | None = None,
        image_input_end: str | None = None,
    ) -> float:
        """Ask upstream how many magicubes a request would cost.

        This endpoint uses snake_case field names, unlike the submit endpoint.
        The arguments must mirror the submit body exactly, otherwise the
        estimate is computed for a different job: MiniMax-H3 with LoRA
        acceleration costs half of what its published step count suggests.
        """
        inner: dict[str, Any] = {
            "predict_type": predict_type,
            "prompt_args": {"prompt": prompt},
            "basic_diffusion_args": {
                "width": width,
                "height": height,
                "total_seconds": seconds,
                "guidance_scale": guidance_scale,
                "num_inference_steps": num_inference_steps,
                "seed": -1,
            },
            "model_args": {"checkpoint_model_version_id": version_id},
        }
        if accelerate_lora is not None:
            inner["video_inference_args"] = {
                # The civision page sends nothing but the acceleration switch
                # here. `mode` and `references` are accepted by upstream, but
                # the page never sets them, so the estimate mirrors the page.
                "accelerate_lora": accelerate_lora,
            }
        if image_input:
            # Image-to-video needs the first frame, otherwise upstream rejects
            # the request with "IMG_2_VIDEO 必须提供首帧图片".
            inner["image_input_front_args"] = {"image": image_input, "stength": 1}
        if image_input_end:
            # First-to-last-frame mode: the end frame rides along in a second
            # block, exactly as the civision page sends it.
            inner["extra_image_input_front_args"] = {
                "image": image_input_end,
                "stength": 1,
            }

        body = {"type": "VIDEO", "mode": "custom", "video_predict_task_request": inner}
        data = await self._request(
            "POST", "/openapi/v1/aigc/agentic/estimate-quota", json_body=body
        )
        if isinstance(data, dict):
            quota = data.get("quota")
            if isinstance(quota, (int, float)):
                return float(quota)
        return 0.0

    async def submit_video(
        self,
        *,
        version_id: int,
        prompt: str,
        negative_prompt: str = "",
        width: int = 480,
        height: int = 480,
        seconds: int = 4,
        guidance_scale: float = 7.5,
        num_inference_steps: int = 50,
        seed: int = -1,
        predict_type: str = "TXT_2_VIDEO",
        image_input: dict | None = None,
        image_input_end: dict | None = None,
        accelerate_lora: bool | None = None,
    ) -> str:
        """Submit a video task and return the upstream task id."""
        body: dict[str, Any] = {
            "predictType": predict_type,
            "promptArgs": {"prompt": prompt, "negativePrompt": negative_prompt},
            "basicDiffusionArgs": {
                "width": width,
                "height": height,
                "totalSeconds": seconds,
                "guidanceScale": guidance_scale,
                "numInferenceSteps": num_inference_steps,
                "seed": seed,
            },
            "modelArgs": {"checkpointModelVersionId": version_id, "loraArgs": []},
        }
        if image_input:
            # A single object with an "image" key, not a list. Upstream rejects
            # the array form outright. `stength` is upstream's own spelling
            # of "strength"; the civision page sends it, so we do too.
            body["imageInputFrontArgs"] = {"image": image_input, "stength": 1}
        if image_input_end:
            # First-to-last-frame mode. The civision page puts the end frame in
            # a second "imageInputFrontArgs"-shaped block; upstream has no
            # "FLF_2_VIDEO" predict type, so the second frame is what makes
            # the task a first/last-frame one.
            body["extraImageInputFrontArgs"] = {"image": image_input_end, "stength": 1}
        if accelerate_lora is not None:
            # Only MiniMax-H3 accepts this block; other models reject it with
            # "videoInferenceArgs 仅支持 MiniMax-H3".
            body["videoInferenceArgs"] = {"accelerateLora": accelerate_lora}

        data = await self._request(
            "POST", "/api/v1/muse/predict/task/video/submit", json_body=body
        )
        if isinstance(data, dict):
            task_id = data.get("taskId") or data.get("task_id")
            if task_id is not None:
                return str(task_id)
        raise CivisionError("upstream did not return a task id", status=502)

    async def task_status(self, task_id: str) -> dict:
        """Poll a task. Statuses: PROCESSING, UNDER_REVIEW, SUCCEED, FAILED, CANCELED."""
        data = await self._request(
            "GET", "/api/v1/muse/predict/task/video/status", params={"taskId": task_id}
        )
        return data if isinstance(data, dict) else {}

    async def get_image_download_url(self, image_id: str) -> str:
        data = await self._request(
            "GET", "/api/v1/muse/image/getImageDownloadUrl", params={"imageId": image_id}
        )
        if isinstance(data, dict):
            for key in ("url", "Url", "downloadUrl", "DownloadUrl"):
                if data.get(key):
                    return str(data[key])
        return ""


async def probe_pasted_cookies(raw: str, proxy: str | None = None) -> SessionInfo:
    """Validate a pasted cookie string without needing an account object.

    Used by the admin UI so the operator gets a precise reason when a paste
    does not work.
    """
    jar = parse_cookie_header(raw)
    if not jar:
        return SessionInfo(authenticated=False, reason="no cookies found in the pasted text")

    missing = [c for c in SESSION_COOKIES if c not in jar]
    if missing:
        present = ", ".join(sorted(jar)) or "none"
        return SessionInfo(
            authenticated=False,
            cookie_jar=jar,
            reason=(
                f"missing {', '.join(missing)} — these carry the login state. "
                f"cookies present: {present}"
            ),
        )

    client = CivisionClient(jar=jar, proxy=proxy)
    return await client.check_session()
