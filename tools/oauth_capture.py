"""Capture a real ModelScope GitHub OAuth callback on this machine.

This answers the one question static analysis could not: after a successful
GitHub sign-in, HOW does ModelScope hand the session to a third-party callback
URL? Three possibilities are checked here:

  1. the session arrives as a Set-Cookie on the redirect  (cookie handoff)
  2. the session arrives in the redirect query string     (token in URL)
  3. the session does not arrive at all                    (host-only cookie;
     we would then need a browser-side capture instead)

Usage:
    python tools/oauth_capture.py

It will:
  - start a local callback server on port 8899
  - ask ModelScope for a GitHub auth URL whose callback_url points at it
  - print the URL and try to open it in your default browser
  - wait for the redirect, then dump everything that arrived

Then sign in with GitHub in the browser that opens. The script prints a
diagnosis and the cookie string you can paste into verify_session.py.
"""

from __future__ import annotations

import http.server
import json
import socket
import sys
import threading
import urllib.parse
import webbrowser
from http.cookies import SimpleCookie

BASE = "https://www.modelscope.ai"
PORT = 8899
RECEIVED: dict[str, object] = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/auth/github/callback"):
            # Browsers fire /favicon.ico on their own; ignore anything but the
            # registered callback path.
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        headers = {k: v for k, v in self.headers.items()}
        cookies = SimpleCookie()
        if "cookie" in headers:
            cookies.load(headers["cookie"])

        RECEIVED["path"] = parsed.path
        RECEIVED["query"] = query
        RECEIVED["headers"] = headers
        RECEIVED["cookies"] = {k: v.value for k, v in cookies.items()}

        body = (
            b"<html><body style='font:16px system-ui;padding:40px'>"
            b"<h2>Callback captured</h2>"
            b"<p>You can close this tab and return to the terminal.</p>"
            b"</body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # silence stderr spam
        return


def free_port(port: int) -> int:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return port


def probe_exchange(request_id: str) -> None:
    """Try to turn a fresh request_id into a usable credential.

    The callback hands us only `request_id`. If ModelScope exposes any
    server-side exchange for it, the session becomes server-usable and we do
    not need a browser at all. Every candidate path is compared against a
    deliberately-bogus one so we can tell "real endpoint" from "SPA fallback".
    """
    import httpx

    candidates = [
        "/api/v1/auth/login/oauth2/session",
        "/api/v1/auth/login/oauth2/ticket",
        "/api/v1/auth/login/oauth2/token",
        "/api/v1/auth/login/oauth2/status",
        "/api/v1/auth/login/oauth2/result",
        "/api/v1/auth/login/oauth2/exchange",
        "/api/v1/auth/login/oauth2/user",
        "/api/v1/auth/login/oauth2/info",
        "/api/v1/auth/login/oauth2/poll",
        "/api/v1/auth/login/oauth2/check",
    ]

    print("\n" + "=" * 68)
    print("PROBING FOR A SERVER-SIDE EXCHANGE OF request_id")
    print("=" * 68)
    print(f"request_id = {request_id}\n")

    def call(client, path: str, rid: str) -> tuple[int, str]:
        try:
            r = client.get(
                "https://www.modelscope.ai" + path,
                params={"request_id": rid},
                headers={"accept": "application/json"},
                follow_redirects=False,
            )
            body = r.text.strip()
            if len(body) > 180:
                body = body[:180] + "..."
            loc = r.headers.get("location", "")
            return r.status_code, f"{body}" + (f"  -> {loc}" if loc else "")
        except Exception as exc:  # noqa: BLE001
            return 0, f"ERROR {exc}"

    with httpx.Client(timeout=30.0) as c:
        bogus = "00000000-0000-0000-0000-000000000000"
        base_status, base_body = call(c, "/api/v1/auth/login/oauth2/zzzznotreal", bogus)
        print(f"baseline (bogus path, bogus id): HTTP {base_status}  {base_body}\n")

        for path in candidates:
            st, body = call(c, path, request_id)
            differs = "  <== DIFFERS FROM BASELINE" if (st, body) != (base_status, base_body) else ""
            print(f"{path}\n    HTTP {st}  {body}{differs}")

    print("\n" + "-" * 68)
    print("If every candidate matches the baseline, ModelScope exposes no")
    print("server-side exchange: the session can only live in the browser's")
    print("cookie jar, and the browser-capture route is required.")
    print("=" * 68)


def diagnose() -> None:
    query = RECEIVED.get("query") or {}
    cookies = RECEIVED.get("cookies") or {}
    headers = RECEIVED.get("headers") or {}

    print("\n" + "=" * 68)
    print("WHAT ARRIVED AT THE CALLBACK")
    print("=" * 68)
    print(f"path    : {RECEIVED.get('path')}")
    print(f"query   : {json.dumps(query, indent=2) if query else '(none)'}")
    print(f"cookies : {json.dumps(cookies, indent=2) if cookies else '(none)'}")
    print("\nheaders of interest:")
    for k, v in headers.items():
        if k.lower() in ("cookie", "user-agent", "referer", "origin", "authorization"):
            print(f"  {k}: {v}")

    print("\n" + "-" * 68)
    print("DIAGNOSIS")
    print("-" * 68)

    sessionish = {"csrf_session", "csrf_token", "acw_tc", "session", "sessionid"}
    got = sessionish & {k.lower() for k in cookies}

    if query.get("error"):
        print(f"OAuth returned an error: {query.get('error')} {query.get('error_description','')}")
        print("-> check that the GitHub account is authorized on ModelScope.")
    elif query:
        print("Session appears to be in the QUERY STRING.")
        print("-> we can read the credential straight from the redirect. Good.")
        for k, v in query.items():
            print(f"     {k} = {v[:70]}{'...' if len(v) > 70 else ''}")
    elif got:
        print(f"Session cookies arrived at our callback: {sorted(got)}")
        print("-> ModelScope hands the session to third-party callbacks. Good.")
        print("\nPaste this into verify_session.py:")
        print("  " + "; ".join(f"{k}={cookies[k]}" for k in sorted(got)))
    else:
        print("Nothing credential-like arrived.")
        print("-> ModelScope likely set a HOST-ONLY cookie for modelscope.ai.")
        print("   The browser will not send it to our domain, so the server cannot")
        print("   read it from the redirect. We will need one of:")
        print("   a) a headless-browser capture step (Playwright opens the login page,")
        print("      logs in, and exports the full cookie jar) -- most robust;")
        print("   b) SDK access token via POST /v1/users/tokens (needs the session first);")
        print("   c) the user pastes their cookie manually into the admin UI.")
        print("\nAlso note these headers (a proxy/browser may need to forward them):")
        for k in ("user-agent", "referer"):
            if k in headers:
                print(f"  {k}: {headers[k]}")

    rid = (query or {}).get("request_id")
    if rid:
        probe_exchange(rid)

    print("=" * 68)


def main() -> int:
    free_port(PORT)
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    import httpx

    callback = f"http://127.0.0.1:{PORT}/auth/github/callback"
    params = {"callback_url": callback, "provider": "github"}
    with httpx.Client(timeout=30.0) as c:
        resp = c.get(
            f"{BASE}/api/v1/auth/login/generate/oauth2/url",
            params=params,
            headers={"accept": "application/json"},
        )
        data = resp.json()
        if not data.get("success"):
            print("Could not build the auth URL:", json.dumps(data, ensure_ascii=False))
            return 1
        url = data["data"]["github_auth_url"]

    print("Local callback server listening on http://127.0.0.1:%d" % PORT)
    print(f"callback_url registered with ModelScope: {callback}\n")
    print("Opening this URL in your browser -- sign in with GitHub:\n")
    print(f"  {url}\n")
    print("(If the browser does not open, paste the URL above manually.)")
    print("\nWaiting for the callback (Ctrl+C to abort)...\n")

    try:
        webbrowser.open(url)
    except Exception:
        pass

    while not RECEIVED:
        try:
            threading.Event().wait(0.5)
        except KeyboardInterrupt:
            print("\nAborted.")
            return 1

    diagnose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
