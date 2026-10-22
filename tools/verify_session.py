"""Probe which credential form ModelScope accepts, and whether it opens the video API.

Usage:
    python tools/verify_session.py --cookie "csrf_session=...; csrf_token=..."
    python tools/verify_session.py --cookies-file cookies.txt   # curl cookie-jar format

Get the cookie string:
  1. Open https://www.modelscope.ai/login and sign in with GitHub.
  2. DevTools -> Application (or Storage) -> Cookies -> https://www.modelscope.ai
  3. Select all rows, copy "Name=Value; Name=Value" for the whole domain.

This script only issues read-only probes plus one cheap video validation call;
it never starts a billable generation.
"""

from __future__ import annotations

import argparse
import sys

import httpx

BASE = "https://www.modelscope.ai"

# Read-only probes first. A 200 (or any non-401) means the credential is accepted.
PROBES: list[tuple[str, str, dict | None]] = [
    ("GET", "/api/v1/users/login/info", None),
    ("GET", "/api/v1/users/info", None),
    ("GET", "/openapi/v1/magicubes/balance", None),
    ("GET", "/openapi/v1/magicubes/spend/rates?category_key=AIGC", None),
    ("POST", "/openapi/v1/aigc/agentic/estimate-quota", {}),
]


def parse_cookie_jar(path: str) -> str:
    """Read a curl/net/http.cookiejar Netscape file into a Cookie header value."""
    parts: list[str] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) >= 7:
                parts.append(f"{fields[5]}={fields[6]}")
    return "; ".join(parts)


def brief(resp: httpx.Response) -> str:
    body = resp.text.strip()
    if len(body) > 220:
        body = body[:220] + "..."
    return f"HTTP {resp.status_code}  {body}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookie", help="Raw Cookie header value")
    ap.add_argument("--cookies-file", help="Netscape cookie jar file")
    ap.add_argument("--video-check", action="store_true",
                    help="Also POST the video endpoint to confirm it accepts this session")
    args = ap.parse_args()

    cookie = args.cookie or (parse_cookie_jar(args.cookies_file) if args.cookies_file else "")
    if not cookie:
        ap.error("provide --cookie or --cookies-file")

    names = [p.split("=", 1)[0].strip() for p in cookie.split(";") if "=" in p]
    print(f"Cookie names supplied: {names}\n")

    cookies: dict[str, str] = {}
    for part in cookie.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()

    headers = {
        "accept": "application/json",
        "referer": f"{BASE}/aigc/videoGeneration",
        "origin": BASE,
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }

    ok = False
    with httpx.Client(headers=headers, cookies=cookies, timeout=30.0, follow_redirects=False) as c:
        for method, path, body in PROBES:
            try:
                resp = c.request(method, BASE + path, json=body)
            except Exception as exc:  # noqa: BLE001 - diagnostic tool
                print(f"[{method:4}] {path}\n        ERROR {exc}")
                continue
            flag = ""
            if resp.status_code == 200:
                ok = True
                flag = "   <-- ACCEPTED"
            print(f"[{method:4}] {path}\n        {brief(resp)}{flag}\n")

        if args.video_check:
            # Deliberately incomplete payload: enough to prove auth passed without
            # actually dispatching a billable render.
            payload = {"video_predict_task_request": {}}
            resp = c.post(BASE + "/openapi/v1/aigc/agentic/video/generate", json=payload)
            print(f"[POST] /openapi/v1/aigc/agentic/video/generate\n        {brief(resp)}")
            if "InvalidAuthentication" not in resp.text:
                ok = True
                print("        <-- video endpoint did NOT reject the session")

    print("\nRESULT:", "credential accepted" if ok else "credential REJECTED everywhere")
    print("\nPaste the output above back into the session.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
