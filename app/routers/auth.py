"""OAuth callback endpoint."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.services.oauth import capture_session

log = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])


@router.get("/auth/github/callback", response_class=HTMLResponse)
async def github_callback(request: Request) -> HTMLResponse:
    """Where ModelScope sends the browser after a GitHub sign-in.

    The callback carries only `request_id`; the session itself is captured by
    driving the same OAuth flow in our own browser.
    """
    error = request.query_params.get("error")
    if error:
        return HTMLResponse(
            f"<h3>Sign-in failed</h3><p>ModelScope returned: {error}</p>",
            status_code=400,
        )

    try:
        jar = await capture_session()
    except Exception as exc:  # noqa: BLE001
        log.warning("session capture failed: %s", exc)
        return HTMLResponse(
            f"<h3>Could not capture the session</h3><p>{exc}</p>"
            "<p>Open the admin panel and use <b>paste cookies</b> instead.</p>",
            status_code=500,
        )

    from app.civision.client import cookie_header

    header = cookie_header(jar)
    return HTMLResponse(
        "<h3>Signed in</h3>"
        "<p>The session was captured. Paste this into the admin panel "
        "under <b>Add account &rarr; paste cookies</b>:</p>"
        f"<textarea rows='8' style='width:100%'>{header}</textarea>"
    )
