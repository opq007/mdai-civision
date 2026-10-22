"""Tests for the HTTP surface: the OpenAI endpoints and the admin guard.

The app is exercised through FastAPI's test client with the upstream calls
stubbed out, so no magicubes are spent.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

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


async def _noop_enqueue(session, job) -> None:
    """Stand in for the queue so no worker picks the job up."""
    await session.flush()
