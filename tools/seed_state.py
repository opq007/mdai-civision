"""Seed the browser profile's saved state from a captured cookie string.

The Chromium cookie database proved unreliable for keeping the ModelScope
session across restarts (m_session_id / h_uid were dropped), so the capture
tool keeps an explicit `storage_state.json` snapshot instead. This script
builds that snapshot from a cookie string -- e.g. one obtained by hand from
DevTools, or from a previous successful capture.

Usage:
    python tools/seed_state.py --cookie-file captured_cookies.txt
    python tools/seed_state.py --cookie "m_session_id=...; h_uid=...; csrf_session=..."
"""

from __future__ import annotations

import argparse
import json
import os

BASE = "https://www.modelscope.ai"
DEFAULT_PROFILE = os.path.join(os.getcwd(), ".browser-profile")


def to_cookie_objects(cookie_header: str) -> list[dict]:
    out: list[dict] = []
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        out.append({
            "name": name,
            "value": value,
            "domain": ".modelscope.ai" if not name.startswith(("acw_", "csrf_", "auth_")) else "www.modelscope.ai",
            "path": "/",
            "expires": -1,
            "httpOnly": name in {"m_session_id", "h_uid", "auth_sid", "acw_tc"},
            "secure": True,
            "sameSite": "Lax",
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookie")
    ap.add_argument("--cookie-file")
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    args = ap.parse_args()

    if args.cookie_file:
        raw = open(args.cookie_file, encoding="utf-8").read()
    elif args.cookie:
        raw = args.cookie
    else:
        ap.error("need --cookie or --cookie-file")

    cookies = to_cookie_objects(raw)
    os.makedirs(args.profile, exist_ok=True)
    path = os.path.join(args.profile, "storage_state.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"cookies": cookies, "origins": []}, fh, indent=1)

    print(f"wrote {len(cookies)} cookies -> {path}")
    print("names:", sorted(c["name"] for c in cookies))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
