"""Establish and capture a ModelScope session using a persistent browser profile.

Design
------
The ModelScope session lives in a cookie scoped to `modelscope.ai` and the
OAuth callback to a third-party URL carries no credential at all (verified:
every candidate server-side exchange endpoint returns an identical 302 to
/login, matching a deliberately bogus path). So the session can only be
obtained from a real browser.

The trick that makes this unattended: use a **persistent** browser profile.
The first run needs a human to sign in to GitHub once; after that the profile
keeps both the GitHub session and the ModelScope session, so every later run
-- including unattended renewal on the server -- is fully automatic.

The OAuth `callback_url` points back at ModelScope itself
(`/aigc/videoGeneration`) rather than at a local server. That keeps the whole
redirect chain on the modelscope.ai domain, so the session cookie is issued
normally and is readable from the browser context.

Usage:
    python tools/browser_capture.py                 # interactive, first run
    python tools/browser_capture.py --headless      # later runs, unattended
    python tools/browser_capture.py --reset         # start from a clean profile

The persistent profile lives in ./.browser-profile by default.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

import httpx

BASE = "https://www.modelscope.ai"
CALLBACK_PATH = "/aigc/videoGeneration"
DEFAULT_PROFILE = os.path.join(os.getcwd(), ".browser-profile")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def build_auth_url() -> str:
    params = {
        "callback_url": f"{BASE}{CALLBACK_PATH}",
        "provider": "github",
    }
    with httpx.Client(timeout=30.0) as c:
        r = c.get(
            f"{BASE}/api/v1/auth/login/generate/oauth2/url",
            params=params,
            headers={"accept": "application/json"},
        )
        data = r.json()
        if not data.get("success"):
            raise SystemExit(f"could not build auth url: {data}")
        return data["data"]["github_auth_url"]


def session_alive(cookies: list[dict]) -> bool:
    """A live session has csrf_session; csrf_token alone is not enough."""
    names = {c["name"] for c in cookies}
    return "csrf_session" in names


def browser_is_logged_in(page) -> tuple[bool, str]:
    """Ask ModelScope, from inside the browser, whether this session is signed in.

    `csrf_session` is issued to anonymous visitors too, so its presence proves
    nothing. The only reliable signal is the API's own answer to
    /api/v1/users/info, asked with the browser's credentials.
    """
    try:
        result = page.evaluate(
            """async () => {
                const r = await fetch('/api/v1/users/info', {
                    credentials: 'include',
                    headers: {'accept': 'application/json'},
                });
                const t = await r.text();
                return { status: r.status, body: t.slice(0, 400) };
            }"""
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"probe failed: {exc}"
    return result.get("status") == 200, json.dumps(result, ensure_ascii=False)


def probe(cookies: list[dict]) -> bool:
    jar = {c["name"]: c["value"] for c in cookies}
    headers = {
        "accept": "application/json",
        "referer": f"{BASE}{CALLBACK_PATH}",
        "origin": BASE,
        "user-agent": UA,
    }
    csrf = jar.get("csrf_token", "")
    if csrf:
        from urllib.parse import unquote
        headers["X-CSRF-TOKEN"] = unquote(csrf)

    checks = [
        ("GET", "/api/v1/users/info", None, "who am I"),
        ("GET", "/openapi/v1/magicubes/balance", None, "magicube balance"),
        ("POST", "/openapi/v1/aigc/agentic/estimate-quota", {}, "estimate quota"),
        ("POST", "/api/v1/muse/predict/task/video/submit",
         {"predictType": "TXT_2_VIDEO", "promptArgs": {"prompt": "probe"}},
         "video submit (invalid body)"),
    ]

    print("\n" + "=" * 70)
    print("PROBING THE CAPTURED SESSION")
    print("=" * 70)
    ok = False
    with httpx.Client(headers=headers, cookies=jar, timeout=30.0) as c:
        for method, path, body, label in checks:
            try:
                r = c.request(method, BASE + path, json=body)
            except Exception as exc:  # noqa: BLE001
                print(f"[{method:4}] {label:<28} ERROR {exc}")
                continue
            txt = r.text.strip()
            if len(txt) > 200:
                txt = txt[:200] + "..."
            mark = "   <-- ACCEPTED" if r.status_code == 200 else ""
            if r.status_code == 200:
                ok = True
            print(f"[{method:4}] {label:<28} HTTP {r.status_code}  {txt}{mark}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--reset", action="store_true", help="wipe the persistent profile")
    ap.add_argument("--no-probe", action="store_true")
    ap.add_argument("--channel", default=None, help="chrome / msedge / chromium")
    args = ap.parse_args()

    if args.reset and os.path.isdir(args.profile):
        shutil.rmtree(args.profile, ignore_errors=True)
        print(f"removed profile {args.profile}")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright missing. Run: pip install playwright && playwright install chromium")
        return 1

    os.makedirs(args.profile, exist_ok=True)
    url = build_auth_url()
    state_path = os.path.join(args.profile, "storage_state.json")

    with sync_playwright() as p:
        launch = {"user_data_dir": args.profile, "headless": args.headless,
                  "args": ["--disable-blink-features=AutomationControlled"]}
        if args.channel:
            launch["channel"] = args.channel
        try:
            ctx = p.chromium.launch_persistent_context(**launch)
        except Exception as exc:  # noqa: BLE001
            print(f"could not launch persistent context: {exc}")
            return 1

        # Restore the last known-good session. Chromium's own cookie database
        # proved unreliable for persisting the ModelScope session across
        # restarts, so we keep an explicit snapshot and re-apply it. This also
        # carries the GitHub session, which is what makes unattended renewal
        # possible.
        if os.path.exists(state_path):
            try:
                cookies = json.load(open(state_path, encoding="utf-8")).get("cookies", [])
                ctx.add_cookies(cookies)
                print(f"restored {len(cookies)} cookies from {state_path}")
            except Exception as exc:  # noqa: BLE001
                print(f"could not restore saved state: {exc}")

        ctx.set_default_timeout(60_000)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # First: is a ModelScope session already sitting in the profile?
        try:
            page.goto(f"{BASE}/aigc/videoGeneration", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
        except Exception as exc:  # noqa: BLE001
            print(f"warmup note: {exc}")

        cookies = ctx.cookies(BASE)
        logged_in, detail = browser_is_logged_in(page)
        if logged_in:
            print("profile already holds a live ModelScope session")
            print(f"  browser says: {detail[:200]}")
        else:
            print("no authenticated session in the profile -- running the GitHub OAuth flow")
            print(f"  browser says: {detail[:200]}")
            print("if the browser asks you to sign in to GitHub, do it once;")
            print("this profile will remember it for every later run.\n")
            try:
                page.goto(url, wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001
                print(f"navigation note: {exc}")

            deadline = time.time() + 180
            last_note = ""
            while time.time() < deadline:
                time.sleep(1.0)
                try:
                    cur = page.url
                except Exception:
                    continue
                logged_in, detail = browser_is_logged_in(page)
                if logged_in:
                    print("session established")
                    break
                if "github.com/login" in cur and last_note != "gh":
                    last_note = "gh"
                    print("  -> GitHub is asking you to sign in. "
                          "Complete it in the browser window.")
                elif "accounts.google.com" in cur and last_note != "g":
                    last_note = "g"
                    print("  -> Google sign-in requested. Complete it in the browser window.")
                elif cur.startswith(BASE) and last_note != "ms":
                    last_note = "ms"
                    print("  -> back on ModelScope, waiting for the session to settle...")
            else:
                print("timed out waiting for an authenticated session")

        cookies = ctx.cookies(BASE)
        logged_in, detail = browser_is_logged_in(page)
        print(f"\nauthenticated (per the browser's own API answer): {logged_in}")
        print("\nmodelscope cookies:")
        for c in cookies:
            print(f"  {c['name']:<16} httpOnly={c.get('httpOnly')} "
                  f"expires={c.get('expires')}")

        if not logged_in:
            print("\nFAILED: the browser session is not authenticated.")
            print("Common causes:")
            print("  - the GitHub account is not registered on ModelScope yet")
            print("    (ModelScope lands on /binding/third to create the account)")
            print("  - the headless browser has no GitHub session (use --headless only")
            print("    after a successful interactive run)")
            ctx.close()
            return 1

        # Persist the full cookie set (ModelScope + GitHub) so that later runs,
        # including unattended ones, can restore the session deterministically.
        try:
            all_cookies = ctx.cookies()
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump({"cookies": all_cookies}, fh, indent=1)
            print(f"\nsaved browser state -> {state_path} ({len(all_cookies)} cookies)")
        except Exception as exc:  # noqa: BLE001
            print(f"could not save browser state: {exc}")

        header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        with open("captured_cookies.txt", "w", encoding="utf-8") as fh:
            fh.write(header)
        print("\nsaved -> captured_cookies.txt")

        ok = True
        if not args.no_probe:
            ok = probe(cookies)

        ctx.close()

    print("\nRESULT:", "session accepted" if ok else "session rejected")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
