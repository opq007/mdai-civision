"""Global request rate limiting (token bucket)."""

from __future__ import annotations

import time
from collections import deque

from fastapi import Request

from app.config import get_settings
from app.services.errors import openai_error


class RateLimiter:
    """A simple sliding-window limiter keyed by API key / client address."""

    def __init__(self, max_per_minute: int) -> None:
        self.max_per_minute = max_per_minute
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._hits.setdefault(key, deque())
        while bucket and now - bucket[0] > 60.0:
            bucket.popleft()
        if len(bucket) >= self.max_per_minute:
            return False
        bucket.append(now)
        return True

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


limiter = RateLimiter(get_settings().rate_limit_rpm)


async def enforce_rate_limit(request: Request) -> None:
    key = request.headers.get("authorization") or request.client.host or "anon"
    if not limiter.check(key):
        raise openai_error(
            429,
            f"rate limit exceeded: at most {limiter.max_per_minute} requests per minute",
            "rate_limit_error",
        )
