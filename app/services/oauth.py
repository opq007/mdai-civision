"""GitHub OAuth: building the sign-in URL and capturing the resulting session.

The ModelScope session is a cookie scoped to modelscope.ai, and the OAuth
callback to a third-party URL carries no credential at all (verified: every
candidate server-side exchange endpoint returns an identical 302 to /login).
So the capture step needs a real browser.

The browser runs with a persistent profile, and the session is additionally
mirrored into an explicit `storage_state.json` snapshot, because Chromium's
own cookie database proved unreliable at keeping `m_session_id` / `h_uid`
across restarts.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

BASE = "https://www.modelscope.ai"


async def build_github_auth_url(callback_url: str) -> str:
    """Ask ModelScope for a GitHub sign-in URL bound to our callback."""
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(
            f"{BASE}/api/v1/auth/login/generate/oauth2/url",
            params={"callback_url": callback_url, "provider": "github"},
            headers={"accept": "application/json"},
        )
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"could not build auth url: {data}")
        return data["data"]["github_auth_url"]


def _state_path() -> Path:
    profile = get_settings().browser_profile_dir
    profile.mkdir(parents=True, exist_ok=True)
    return profile / "storage_state.json"


def load_saved_cookies() -> list[dict]:
    path = _state_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("cookies", [])
    except Exception:  # noqa: BLE001
        return []


def save_cookies(cookies: list[dict]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cookies": cookies}, indent=1), encoding="utf-8")


async def capture_session() -> dict[str, str]:
    """Run the OAuth flow in a browser and return the resulting cookies.

    Requires the persistent profile to already hold a signed-in GitHub
    session, otherwise the flow stops at GitHub's sign-in page and this
    raises.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "playwright is not installed; run 'pip install playwright && "
            "playwright install chromium'"
        ) from exc

    settings = get_settings()
    profile = settings.browser_profile_dir
    profile.mkdir(parents=True, exist_ok=True)

    auth_url = await build_github_auth_url(f"{BASE}/aigc/videoGeneration")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile),
            headless=settings.playwright_headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            saved = load_saved_cookies()
            if saved:
                try:
                    ctx.add_cookies(saved)
                except Exception:  # noqa: BLE001
                    pass

            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(auth_url, wait_until="domcontentloaded", timeout=60_000)

            for _ in range(120):
                page.wait_for_timeout(1000)
                result = page.evaluate(
                    """async () => {
                        const r = await fetch('/api/v1/users/info', {
                            credentials: 'include',
                            headers: {'accept': 'application/json'},
                        });
                        return { status: r.status, body: (await r.text()).slice(0, 300) };
                    }"""
                )
                if result.get("status") == 200:
                    break
            else:
                raise RuntimeError(
                    "the OAuth flow did not produce a signed-in session. "
                    "Run tools/browser_capture.py once, interactively, so the "
                    "browser profile learns the GitHub sign-in."
                )

            cookies = ctx.cookies(BASE)
            try:
                save_cookies(ctx.cookies())
            except Exception:  # noqa: BLE001
                pass
            return {c["name"]: c["value"] for c in cookies}
        finally:
            ctx.close()
