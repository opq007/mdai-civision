"""Tests for the reference-image upload flow.

The upload talks to two different hosts: ModelScope's `/api/v1/rm/*` endpoints
and the pre-signed Aliyun OSS URL. The ModelScope session cookies must go to
the former and must never go to the latter.
"""

from __future__ import annotations

import pytest

from app.civision.client import CivisionClient, NotAuthenticated


class FakeResponse:
    def __init__(self, status_code: int = 200, body=None, text: str = ""):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = text

    def json(self):
        return self._body


class FakeCookies:
    def __init__(self, values=None):
        self._values = values or {}

    def items(self):
        return self._values.items()


class FakeClient:
    def __init__(self, cookies=None, posts=None):
        self.cookies = FakeCookies(cookies)
        self._posts = list(posts or [])
        self.requests = []
        self.put_url = None
        self.put_headers = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None):
        self.requests.append((url, json))
        return self._posts.pop(0)

    async def put(self, url, content=None, headers=None):
        self.put_url = url
        self.put_headers = headers
        return FakeResponse(200)


def test_duplicate_csrf_cookies_collapse_to_one_value():
    # ModelScope sets csrf_token for more than one path. httpx rejects reading
    # that jar by name; the flat session jar must still absorb the latest one.
    import httpx

    from app.civision.client import cookie_values

    cookies = httpx.Cookies()
    cookies.set("csrf_token", "old", domain="www.modelscope.ai", path="/")
    cookies.set("csrf_token", "fresh", domain="www.modelscope.ai", path="/api")
    with pytest.raises(httpx.CookieConflict):
        cookies.get("csrf_token")

    assert cookie_values(cookies)["csrf_token"] == "fresh"


async def test_upload_keeps_session_cookies_off_the_oss_host(monkeypatch):
    from app.civision import upload as upload_mod

    model_scope = FakeClient(
        cookies={"csrf_token": "fresh"},
        posts=[
            FakeResponse(
                200,
                {"Data": {"UploadUrl": "https://bucket.aliyuncs.com/obj?sig=1"}},
            ),
            FakeResponse(200, {"Data": {"DownloadUrl": "https://cdn.example.com/obj"}}),
        ],
    )
    oss = FakeClient()
    clients = [model_scope, oss]
    calls: list[dict] = []

    def factory(**kwargs):
        calls.append(kwargs)
        return clients.pop(0)

    monkeypatch.setattr(upload_mod.httpx, "AsyncClient", factory)

    client = CivisionClient(
        jar={"m_session_id": "x", "h_uid": "1", "csrf_token": "old"}
    )
    url = await upload_mod.upload_image(client, b"img", "ref.png")

    assert url == "https://cdn.example.com/obj"
    # The ModelScope response rotated csrf_token; the client jar has it.
    assert client.jar["csrf_token"] == "fresh"
    # The OSS client is separate and receives no session cookies.
    assert oss.put_url.startswith("https://bucket.aliyuncs.com/")
    assert "cookies" not in calls[1]


def test_upload_auth_failure_is_not_authenticated():
    from app.civision.upload import _parse

    response = FakeResponse(
        200,
        {"Code": 10010101003, "Message": "user not logged in", "Data": {}},
    )
    with pytest.raises(NotAuthenticated):
        _parse(response)
