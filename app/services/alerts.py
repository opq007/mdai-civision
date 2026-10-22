"""Webhook alerts (DingTalk / Feishu / Slack all accept a simple JSON POST)."""

from __future__ import annotations

import logging

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


async def send_alert(message: str) -> None:
    """Post an alert to the configured webhook, if any."""
    url = get_settings().alert_webhook_url
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            await c.post(
                url,
                json={"text": message, "msgtype": "text", "content": message},
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not send alert: %s", exc)
