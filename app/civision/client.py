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

# Every cookie the browser (or upstream) hands back is persisted; see
# `save_jar` in app/civision/accounts.py. The anti-bot cookies matter as much
# as the login ones, because dropping them makes the service's requests look
# different from the browser that created the session.

CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

FIREFOX_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) "
    "Gecko/20100101 Firefox/135.0"
)

# Kept for callers that only need the default browser.
USER_AGENT = CHROME_USER_AGENT

BROWSER_USER_AGENTS = {
    "chrome": CHROME_USER_AGENT,
    "firefox": FIREFOX_USER_AGENT,
}


def user_agent_for(browser: str | None) -> str:
    """Resolve a browser label (chrome/firefox) to its User-Agent string."""
    return BROWSER_USER_AGENTS.get((browser or "chrome").strip().lower(), USER_AGENT)


class CivisionError(Exception):
    """Base class for upstream failures."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "internal_error",
        status: int = 500,
        upstream_code: Any = None,
        request_id: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        # The upstream's own code/request id, when the body carried them.
        # They are what makes a WAF 403 distinguishable from a dead session.
        self.upstream_code = upstream_code
        self.request_id = request_id

    def diagnostic(self) -> str:
        """Compact operator-facing description of the upstream failure."""
        parts = [f"HTTP {self.status}", f"code={self.code}"]
        if self.upstream_code is not None:
            parts.append(f"upstream_code={self.upstream_code}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        return " ".join(parts) + f": {self.message}"


class NotAuthenticated(CivisionError):
    def __init__(
        self,
        message: str = "not signed in to ModelScope",
        **kwargs: Any,
    ):
        super().__init__(message, code="not_authenticated", status=401, **kwargs)


class InsufficientBalance(CivisionError):
    def __init__(self, message: str = "not enough magicubes", **kwargs: Any):
        super().__init__(message, code="insufficient_quota", status=429, **kwargs)


class RateLimited(CivisionError):
    def __init__(self, message: str = "upstream rate limit hit", **kwargs: Any):
        super().__init__(message, code="rate_limit_exceeded", status=429, **kwargs)


class Forbidden(CivisionError):
    """Upstream refused the request (WAF, risk control, missing permission).

    This is deliberately *not* `NotAuthenticated`: a blocked request does
    not prove that the cookie session is dead.
    """

    def __init__(self, message: str = "upstream denied the request", **kwargs: Any):
        super().__init__(message, code="forbidden", status=403, **kwargs)


class InvalidRequest(CivisionError):
    def __init__(self, message: str, **kwargs: Any):
        super().__init__(message, code="invalid_request_error", status=400, **kwargs)


class UpstreamUnavailable(CivisionError):
    def __init__(self, message: str = "upstream service unavailable", **kwargs: Any):
        super().__init__(message, code="server_error", status=503, **kwargs)


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


def cookie_values(cookies) -> dict[str, str]:
    """Read a cookie collection into one value per name.

    ModelScope sets `csrf_token` more than once in a single exchange, on
    different paths or domains. httpx then refuses `cookies.items()` with
    "Multiple cookies exist with name=csrf_token". The service keeps a flat
    jar, so the value set last is the one the next request sends.
    """
    jar = getattr(cookies, "jar", None)
    if jar is None:
        return dict(cookies.items())
    values: dict[str, str] = {}
    for cookie in jar:
        values[cookie.name] = cookie.value
    return values


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
    # True when the probe got no definitive answer about the session: a
    # rate limit, a WAF/403 rejection, a malformed upstream response, or any
    # other business error. None of these prove the cookie is invalid, so
    # the account must stay in the pool.
    transient: bool = False


class CivisionClient:
    """Thin async wrapper over the endpoints we actually use."""

    def __init__(
        self,
        jar: dict[str, str] | None = None,
        proxy: str | httpx.Proxy | None = None,
        timeout: float | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.jar: dict[str, str] = dict(jar or {})
        s = get_settings()
        self._proxy = proxy
        self._timeout = timeout or s.civision_timeout_s
        self._user_agent = user_agent or USER_AGENT
        self._trust_env = s.trust_env_proxy

    @property
    def user_agent(self) -> str:
        return self._user_agent

    # -- plumbing ---------------------------------------------------------

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        h = {
            "accept": "application/json",
            "accept-language": "en-US,en;q=0.9",
            "referer": f"{BASE}/aigc/videoGeneration",
            "origin": BASE,
            "user-agent": self._user_agent,
            "X-ModelScope-Accept-Language": "en-US",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        if json_body:
            h["content-type"] = "application/json"
        token = self.jar.get("csrf_token")
        if token:
            h["X-CSRF-TOKEN"] = unquote(token)
        # The frontend sends the raw `xsrf` cookie, when it exists, in this
        # header. Mirroring it keeps the request shape the same.
        xsrf = self.jar.get("xsrf")
        if xsrf:
            h["X-XSRF-TOKEN"] = xsrf
        return h

    def _client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "headers": self._headers(),
            "cookies": self.jar,
            "timeout": self._timeout,
            "follow_redirects": False,
            # Do not let a container's HTTP(S)_PROXY silently change the
            # egress IP; an explicit account proxy is the only proxy used.
            "trust_env": self._trust_env,
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

        # ModelScope re-issues csrf_token on almost every response and may
        # rotate other cookies (including m_session_id) at any time. Keep
        # everything it hands back; `save_jar` decides what is persisted.
        for name, value in cookie_values(r.cookies).items():
            self.jar[name] = value

        return self._unwrap(r)

    @classmethod
    def _unwrap(cls, r: httpx.Response) -> Any:
        """ModelScope often returns HTTP 200 with a failure inside the body.

        Status codes are handled before the body is parsed: a WAF or an
        error page often returns HTML, and whether a response means "not
        logged in" must not depend on the body being valid JSON.

        Three envelope shapes are in play:
          /api/v1/*       -> {"Code": 200, "Data": {...}, "Success": true}
          /openapi/v1/*   -> {"success": true, "data": {...}}
          legacy          -> {"Code": ..., "Data": {"code": ..., "success": ...}}
        """
        if r.status_code == 401:
            message, upstream_code, request_id = cls._error_details(r)
            raise NotAuthenticated(
                message,
                upstream_code=upstream_code,
                request_id=request_id,
            )
        if r.status_code == 403:
            message, upstream_code, request_id = cls._error_details(r)
            raise Forbidden(
                message,
                upstream_code=upstream_code,
                request_id=request_id,
            )
        if r.status_code == 429:
            message, upstream_code, request_id = cls._error_details(r)
            raise RateLimited(
                message,
                upstream_code=upstream_code,
                request_id=request_id,
            )
        if r.status_code >= 500:
            message, upstream_code, request_id = cls._error_details(r)
            raise UpstreamUnavailable(
                message,
                upstream_code=upstream_code,
                request_id=request_id,
            )

        try:
            body = r.json()
        except (json.JSONDecodeError, ValueError):
            raise CivisionError(
                f"unexpected upstream response ({r.status_code})", status=502
            )

        # openapi envelope
        if isinstance(body, dict) and "success" in body:
            if body.get("success") is False:
                raise cls._business_error(body)
            if "data" in body:
                return body.get("data")
            return body

        # openapi error envelope without `success`:
        #   {"code": "InvalidAuthentication", "message": "user not logged in"}
        if (
            isinstance(body, dict)
            and "success" not in body
            and "code" in body
            and body.get("code") not in (0, 200, "0", "200", None)
        ):
            raise cls._business_error(body)

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
    def _error_details(r: httpx.Response) -> tuple[str, Any, str | None]:
        """Return (message, upstream_code, request_id) from an error body."""
        try:
            body = r.json()
        except (json.JSONDecodeError, ValueError):
            text = r.text.strip()
            return (
                text[:200] if text else f"upstream returned {r.status_code}",
                None,
                None,
            )
        if not isinstance(body, dict):
            return (f"upstream returned {r.status_code}", None, None)

        message = ""
        for key in ("Message", "message", "msg"):
            if body.get(key):
                message = str(body[key])
                break
        data = body.get("Data") or body.get("data")
        if not message and isinstance(data, dict):
            for key in ("Message", "message"):
                if data.get(key):
                    message = str(data[key])
                    break

        upstream_code = body.get("Code")
        if upstream_code is None:
            upstream_code = body.get("code")
        request_id = (
            body.get("RequestId")
            or body.get("requestId")
            or body.get("request_id")
            or (
                data.get("RequestId") or data.get("request_id")
                if isinstance(data, dict)
                else None
            )
        )
        return (
            message or f"upstream returned {r.status_code}",
            upstream_code,
            request_id,
        )

    @staticmethod
    def _business_error(data: dict) -> CivisionError:
        code = data.get("code")
        msg = data.get("message") or "upstream rejected the request"
        request_id = (
            data.get("RequestId")
            or data.get("requestId")
            or data.get("request_id")
        )
        # The openapi envelope uses a string code for auth failures:
        #   {"code": "InvalidAuthentication", "message": "user not logged in"}
        if (
            str(code).lower() == "invalidauthentication"
            or "not logged in" in str(msg).lower()
        ):
            return NotAuthenticated(
                str(msg), upstream_code=code, request_id=request_id
            )
        if code == 50001 or "frequency" in str(msg).lower():
            return RateLimited(
                str(msg), upstream_code=code, request_id=request_id
            )
        if code == 99999:
            return InvalidRequest(
                str(msg), upstream_code=code, request_id=request_id
            )
        if code == 40000:
            return InvalidRequest(
                str(msg), upstream_code=code, request_id=request_id
            )
        return CivisionError(
            str(msg),
            code="upstream_error",
            status=400,
            upstream_code=code,
            request_id=request_id,
        )

    @staticmethod
    def _map_api_code(code: Any, msg: str, body: dict) -> CivisionError:
        data = body.get("Data")
        request_id = (
            body.get("RequestId")
            or body.get("requestId")
            or body.get("request_id")
            or (
                data.get("RequestId") or data.get("request_id")
                if isinstance(data, dict)
                else None
            )
        )
        if code == 10010101003:
            raise NotAuthenticated(
                msg, upstream_code=code, request_id=request_id
            )
        if code == 10019901001:
            raise Forbidden(
                msg, upstream_code=code, request_id=request_id
            )
        return CivisionError(
            msg,
            code="upstream_error",
            status=400,
            upstream_code=code,
            request_id=request_id,
        )

    # -- endpoints --------------------------------------------------------

    async def whoami(self) -> dict:
        """Return the logged-in user's info, or raise NotAuthenticated."""
        return await self._request("GET", "/api/v1/users/info")

    async def touch_login(self) -> dict:
        """The request the site makes when an already-signed-in browser opens it.

        Upstream treats this read as the day's login and credits the daily
        magicube grant as a side effect. It does not log anything in, and
        calling it again the same day does not grant a second time. It is not
        a session probe: a rejection here says nothing about the cookie.
        """
        return await self._request("GET", "/api/v1/users/login/info")

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
        except NotAuthenticated as exc:
            # The only definitive answer that the session is dead.
            return SessionInfo(
                authenticated=False,
                cookie_jar=jar,
                reason=f"ModelScope rejected the session ({exc.diagnostic()})",
            )
        except UpstreamUnavailable as exc:
            # The network is down, not the session. Reporting this as
            # "not authenticated" would mark the account expired and take it
            # out of the pool for good.
            return SessionInfo(
                authenticated=False,
                cookie_jar=jar,
                reason=f"could not reach ModelScope ({exc.diagnostic()})",
                unreachable=True,
                transient=True,
            )
        except RateLimited as exc:
            return SessionInfo(
                authenticated=False,
                cookie_jar=jar,
                reason=f"upstream rate limited the probe ({exc.diagnostic()})",
                transient=True,
            )
        except Forbidden as exc:
            return SessionInfo(
                authenticated=False,
                cookie_jar=jar,
                reason=f"upstream blocked the probe ({exc.diagnostic()})",
                transient=True,
            )
        except CivisionError as exc:
            # Any other upstream rejection (business error, malformed
            # response, invalid request) is not proof that the cookie is
            # invalid. Keep the account in the pool and let the next probe
            # decide.
            return SessionInfo(
                authenticated=False,
                cookie_jar=jar,
                reason=f"probe failed ({exc.diagnostic()})",
                transient=True,
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


async def probe_pasted_cookies(
    raw: str, proxy: str | httpx.Proxy | None = None, user_agent: str | None = None
) -> SessionInfo:
    """Validate a pasted cookie string without needing an account object.

    Used by the admin UI so the operator gets a precise reason when a paste
    does not work. `user_agent` should match the browser the cookies came
    from, so the probe sees the same identity upstream will see later.
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

    client = CivisionClient(jar=jar, proxy=proxy, user_agent=user_agent)
    return await client.check_session()
