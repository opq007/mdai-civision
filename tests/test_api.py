"""Tests for the HTTP surface: the OpenAI endpoints and the admin guard.

The app is exercised through FastAPI's test client with the upstream calls
stubbed out, so no magicubes are spent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import VideoJob

# A key that exists in the test database. The service hashes the key before
# storing it, so the plaintext has to be created once and looked up by hash.
TEST_API_KEY = "sk-civ-unit-test-key"


@pytest.fixture
def api_key():
    """The API key the tests authenticate with.

    The router binds `require_api_key` when it is created, so the dependency
    is overridden on the app rather than monkey-patched.
    """
    return TEST_API_KEY


@pytest.fixture
def client(monkeypatch, api_key):
    """A test client whose upstream calls never leave the machine."""

    async def fake_catalog(session=None):
        from app.civision.mapping import build_catalog
        from app.services.catalog import Catalog

        from tests.test_mapping import SAMPLE_CATALOG

        models = build_catalog(
            SAMPLE_CATALOG,
            {"sora-2": "WAN_VIDEO_2_1_T2V_1_3_B", "sora-2-pro": "MINIMAX_H3"},
        )
        return Catalog(models=models, fetched_at=float("inf"), source="test")

    monkeypatch.setattr("app.services.catalog.catalog_service.get", fake_catalog)
    from app.security import require_api_key

    app.dependency_overrides[require_api_key] = lambda: None
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def anon_client():
    """A client with **no** API-key override, so the guard really runs.

    The test database holds no API keys, so any `Authorization` header is
    rejected; a request without one is rejected for the missing key.
    """
    return TestClient(app, follow_redirects=False)


def _make_job(
    video_id: str,
    *,
    status: str = "completed",
    filename: str | None = None,
    upstream_url: str | None = None,
):
    """Insert a job row straight into the test database.

    The route reads the row and, for a completed job, the file it points at.
    No worker is involved, so no generation is started.
    """
    from app.db import SessionLocal, _engine
    from app.models import utcnow

    async def insert():
        async with SessionLocal() as session:
            now = utcnow()
            job = VideoJob(
                id=video_id,
                model="minimax-h3-turbo",
                prompt="a bird",
                seconds="4",
                size="720x1280",
                width=720,
                height=1280,
                status=status,
                progress=100 if status == "completed" else 0,
                local_path=filename,
                upstream_url=upstream_url,
                created_at=now,
                created_at_ts=int(now.timestamp()),
            )
            session.add(job)
            await session.commit()
        # The pool would otherwise hand the connection to the test client's
        # event loop, and aiosqlite connections cannot cross loops.
        await _engine.dispose()

    asyncio.run(insert())


class TestAdminGuard:
    """The admin UI is HTML, so an unauthenticated browser is redirected."""

    def test_browser_is_redirected_to_the_login_page(self, client):
        r = client.get("/admin", headers={"Accept": "text/html,application/xhtml+xml"})
        assert r.status_code == 303
        assert r.headers["location"] == "/admin/login"

    def test_api_client_gets_the_openai_error_envelope(self, client):
        r = client.get("/admin", headers={"Accept": "application/json"})
        assert r.status_code == 401
        assert r.json()["error"]["message"] == "admin sign-in required"

    def test_login_page_is_reachable_without_a_session(self, client):
        r = client.get("/admin/login", headers={"Accept": "text/html"})
        assert r.status_code == 200
        assert "Sign in" in r.text

    def test_index_page_links_to_the_sign_in_page(self, client):
        r = client.get("/", headers={"Accept": "text/html"})
        assert r.status_code == 200
        assert "/admin/login" in r.text


class TestVideoCreation:
    """Validation of the two-image (first-to-last-frame) request."""

    def _headers(self) -> dict:
        return {
            "Authorization": "Bearer sk-civ-test",
            "content-type": "application/json",
        }

    def test_two_images_are_accepted_for_minimax(self, client, monkeypatch):
        # The job must never reach the workers: a real job would start a real
        # generation and spend magicubes. The router holds a direct reference
        # to `enqueue_job`, so that binding is what has to be replaced.
        monkeypatch.setattr("app.routers.videos.enqueue_job", _noop_enqueue)
        r = client.post(
            "/v1/videos",
            headers=self._headers(),
            json={
                "model": "minimax-h3",
                "prompt": "a bird",
                "seconds": "4",
                "size": "720x1280",
                "input_reference": {"image_url": "https://example.com/a.png"},
                "input_reference_end": {"image_url": "https://example.com/b.png"},
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "queued"

    def test_wan_defaults_to_twenty_steps(self, client, monkeypatch):
        seen: dict = {}

        async def capture(session, job):
            seen["steps"] = job.num_inference_steps

        monkeypatch.setattr("app.routers.videos.enqueue_job", capture)
        r = client.post(
            "/v1/videos",
            headers=self._headers(),
            json={"model": "wan2.1-t2v-1.3b", "prompt": "a bird", "seconds": "4"},
        )
        assert r.status_code == 200, r.text
        assert seen["steps"] == 20

    def test_wan_steps_can_be_overridden(self, client, monkeypatch):
        seen: dict = {}

        async def capture(session, job):
            seen["steps"] = job.num_inference_steps

        monkeypatch.setattr("app.routers.videos.enqueue_job", capture)
        r = client.post(
            "/v1/videos",
            headers=self._headers(),
            json={
                "model": "wan2.1-t2v-1.3b",
                "prompt": "a bird",
                "seconds": "4",
                "num_inference_steps": 25,
            },
        )
        assert r.status_code == 200, r.text
        assert seen["steps"] == 25

    def test_accelerated_variant_pins_steps_to_four(self, client, monkeypatch):
        seen: dict = {}

        async def capture(session, job):
            seen["steps"] = job.num_inference_steps

        monkeypatch.setattr("app.routers.videos.enqueue_job", capture)
        r = client.post(
            "/v1/videos",
            headers=self._headers(),
            json={
                "model": "minimax-h3-turbo",
                "prompt": "a bird",
                "seconds": "4",
                "num_inference_steps": 25,
            },
        )
        assert r.status_code == 200, r.text
        assert seen["steps"] == 4

    def test_a_bare_image_string_is_stored_on_the_job(self, client, monkeypatch):
        # new-api's OpenAI video channel declares input_reference as a string
        # and forwards that string. The object form above must keep working too.
        seen: dict = {}

        async def capture(session, job):
            seen["url"] = job.input_reference_url
            seen["seconds"] = job.seconds

        monkeypatch.setattr("app.routers.videos.enqueue_job", capture)
        r = client.post(
            "/v1/videos",
            headers=self._headers(),
            json={
                "model": "wan2.1-i2v-14b",
                "prompt": "a bird",
                "seconds": 4,
                "size": "720x1280",
                "input_reference": "data:image/png;base64,aaaa",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "queued"
        assert seen["url"] == "data:image/png;base64,aaaa"
        assert seen["seconds"] == "3"

    def test_new_api_image_aliases_fill_the_two_frames(self, client, monkeypatch):
        seen: dict = {}

        async def capture(session, job):
            seen["start"] = job.input_reference_url
            seen["end"] = job.input_reference_end_url
            seen["seconds"] = job.seconds

        monkeypatch.setattr("app.routers.videos.enqueue_job", capture)
        r = client.post(
            "/v1/videos",
            headers=self._headers(),
            json={
                "model": "minimax-h3",
                "prompt": "a bird",
                "duration": 8,
                "image": "https://example.com/a.png",
                "images": ["https://example.com/b.png"],
            },
        )
        assert r.status_code == 200, r.text
        assert seen == {
            "start": "https://example.com/a.png",
            "end": "https://example.com/b.png",
            "seconds": "5",
        }

    def test_n8n_multipart_reference_is_stored_for_the_worker(self, client, monkeypatch):
        # n8n's OpenAI video node posts multipart/form-data, and new-api's
        # OpenAI video channel forwards that body, file part included.
        seen: dict = {}

        async def capture(session, job):
            seen["url"] = job.input_reference_url
            seen["seconds"] = job.seconds

        monkeypatch.setattr("app.routers.videos.enqueue_job", capture)
        r = client.post(
            "/v1/videos",
            headers={"Authorization": "Bearer sk-civ-test"},
            data={
                "model": "wan2.1-i2v-14b",
                "prompt": "a bird",
                "seconds": "4",
                "size": "1280x720",
            },
            files={"input_reference": ("cat.png", b"\x89PNG-fake", "image/png")},
        )
        assert r.status_code == 200, r.text
        assert seen["seconds"] == "3"
        stored = Path(seen["url"])
        assert stored.read_bytes() == b"\x89PNG-fake"
        stored.unlink()

    def test_a_bad_end_frame_names_the_end_frame(self, client):
        r = client.post(
            "/v1/videos",
            headers=self._headers(),
            json={
                "model": "minimax-h3",
                "prompt": "a bird",
                "input_reference": "https://example.com/a.png",
                "input_reference_end": 12,
            },
        )
        assert r.status_code == 400
        error = r.json()["error"]
        assert error["param"] == "input_reference_end"
        assert "input_reference_end" in error["message"]

    def test_two_images_are_rejected_for_a_model_without_flf(self, client):
        r = client.post(
            "/v1/videos",
            headers=self._headers(),
            json={
                "model": "wan2.1-i2v-14b",
                "prompt": "a bird",
                "input_reference": {"image_url": "https://example.com/a.png"},
                "input_reference_end": {"image_url": "https://example.com/b.png"},
            },
        )
        assert r.status_code == 400
        error = r.json()["error"]
        assert error["param"] == "input_reference_end"
        assert "first-to-last-frame" in error["message"]

    def test_end_frame_without_a_start_frame_is_rejected(self, client):
        r = client.post(
            "/v1/videos",
            headers=self._headers(),
            json={
                "model": "minimax-h3",
                "prompt": "a bird",
                "input_reference_end": {"image_url": "https://example.com/b.png"},
            },
        )
        assert r.status_code == 400
        error = r.json()["error"]
        assert error["param"] == "input_reference"

    def test_a_text_only_model_still_rejects_an_image(self, client):
        r = client.post(
            "/v1/videos",
            headers=self._headers(),
            json={
                "model": "wan2.1-t2v-1.3b",
                "prompt": "a bird",
                "input_reference": {"image_url": "https://example.com/a.png"},
            },
        )
        assert r.status_code == 400
        assert r.json()["error"]["param"] == "input_reference"


class TestPublicVideoAccess:
    """The status and download routes must not require an API key.

    A video URL gets handed to a browser, a player or a third-party page,
    none of which can present a key. The job id is the capability.
    """

    def test_status_is_readable_without_an_api_key(self, anon_client):
        _make_job("video_public_status")
        r = anon_client.get("/v1/videos/video_public_status")
        assert r.status_code == 200, r.text
        assert r.json()["id"] == "video_public_status"
        assert r.json()["status"] == "completed"

    def test_content_is_downloadable_without_an_api_key(self, anon_client):
        from app.config import get_settings

        settings = get_settings()
        name = "video_public_download.mp4"
        (settings.storage_dir / name).write_bytes(b"fake-mp4-bytes")
        _make_job("video_public_download", filename=name)

        r = anon_client.get("/v1/videos/video_public_download/content")
        assert r.status_code == 200, r.text
        assert r.content == b"fake-mp4-bytes"
        assert r.headers["content-type"] == "video/mp4"
        # Served inline so the URL can be used as a <video> source.
        assert r.headers["content-disposition"].startswith("inline")

    def test_content_can_be_forced_to_download(self, anon_client):
        from app.config import get_settings

        settings = get_settings()
        name = "video_public_forced_download.mp4"
        (settings.storage_dir / name).write_bytes(b"fake-mp4-bytes")
        _make_job("video_public_forced_download", filename=name)

        r = anon_client.get(
            "/v1/videos/video_public_forced_download/content?download=true"
        )
        assert r.status_code == 200, r.text
        assert r.headers["content-disposition"].startswith("attachment")

    def test_content_answers_a_range_request(self, anon_client):
        from app.config import get_settings

        settings = get_settings()
        name = "video_public_range.mp4"
        (settings.storage_dir / name).write_bytes(b"0123456789")
        _make_job("video_public_range", filename=name)

        r = anon_client.get(
            "/v1/videos/video_public_range/content",
            headers={"Range": "bytes=2-5"},
        )
        assert r.status_code == 206
        assert r.content == b"2345"

    def test_an_invalid_api_key_still_cannot_create_a_job(self, anon_client):
        r = anon_client.post(
            "/v1/videos",
            headers={"Authorization": "Bearer sk-not-a-real-key"},
            json={"model": "minimax-h3-turbo", "prompt": "a bird"},
        )
        assert r.status_code == 401

    def test_listing_still_requires_an_api_key(self, anon_client):
        r = anon_client.get("/v1/videos")
        assert r.status_code == 401

    def test_a_missing_job_is_a_404_not_a_401(self, anon_client):
        r = anon_client.get("/v1/videos/video_does_not_exist/content")
        assert r.status_code == 404
        assert r.json()["error"]["type"] == "invalid_request_error"

    def test_a_job_that_is_not_ready_is_a_409(self, anon_client):
        _make_job("video_public_pending", status="queued")
        r = anon_client.get("/v1/videos/video_public_pending/content")
        assert r.status_code == 409
        assert "not ready" in r.json()["error"]["message"]

    def test_content_falls_back_to_the_upstream_url(self, anon_client, monkeypatch):
        """The local copy is gone after TTL cleanup; the CDN still has it."""
        _make_job("video_public_upstream", upstream_url="https://cdn.example.com/clip.mp4")

        class FakeResponse:
            status_code = 200
            content = b"upstream-bytes"
            headers = {"content-length": "14"}

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None):
                assert headers is None or "Range" not in headers
                return FakeResponse()

        monkeypatch.setattr("httpx.AsyncClient", FakeClient)

        r = anon_client.get("/v1/videos/video_public_upstream/content")
        assert r.status_code == 200, r.text
        assert r.content == b"upstream-bytes"
        assert r.headers["content-disposition"].startswith("inline")


async def _noop_enqueue(session, job) -> None:
    """Stand in for the queue so no worker picks the job up."""
    await session.flush()
