"""Extract a live ModelScope session from a real Chrome profile via CDP.

Why CDP instead of reading the cookie file
------------------------------------------
On Windows, recent Chrome encrypts cookie values with App-Bound Encryption
(key tied to the browser binary). Reading `Network/Cookies` directly yields
ciphertext. The DevTools protocol, however, hands back *decrypted* values --
because the browser itself is doing the decryption.

The Chrome >= 136 security change also refuses `--remote-debugging-port` when
pointed at the default user-data-dir, so we launch with an explicit
`--user-data-dir` that we manage ourselves and copy the real profile into.

Usage:
    python tools/cdp_capture.py
    python tools/cdp_capture.py --profile "C:/Users/me/AppData/Local/Google/Chrome/User Data"

This is also the shape the server-side service will use: a dedicated,
persistent Chrome profile on the host, driven over CDP, whose cookie jar
supplies the session for the ModelScope account.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "https://www.modelscope.ai"
DEBUG_PORT = 9333
DEFAULT_PROFILE = os.path.expanduser(
    r"~/AppData/Local/Google/Chrome/User Data"
)
# Chrome's own data, minus caches and lock files.
SKIP_DIRS = {
    "Cache", "Code Cache", "GPUCache", "Service Worker", "Service Worker/CacheStorage",
    "Default/Cache", "Default/Code Cache", "Default/GPUCache", "Default/Service Worker",
    "Default/Application Cache", "ShaderCache", "GrShaderCache", "GraphiteDawnCache",
    "Crashpad", "BrowserMetrics", "component_crx_cache", "Safe Browsing",
    "Default/Safe Browsing", "OptimizationHints", "Default/OptimizationHints",
    "blob_storage", "IndexedDB", "Default/IndexedDB", "File System", "Default/File System",
    "DawnGraphiteCache", "DawnWebGPUCache", "Media Cache", "Default/Media Cache",
    "Extension State", "Default/Extension State", "Default/Extension Rules",
}


def http_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def wait_for_devtools(port: int, timeout: float = 45.0) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return http_json(f"http://127.0.0.1:{port}/json/version")
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            time.sleep(0.5)
    return None


def prepare_profile(src: str, dst: str) -> None:
    """Copy the real Chrome profile into a directory Chrome will accept for
    remote debugging."""
    if os.path.isdir(dst):
        shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst, exist_ok=True)

    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if name in ("Default", "Profile 1", "Profile 2", "Local State"):
            continue
        if os.path.isfile(s) and os.path.getsize(s) < 20 * 1024 * 1024:
            try:
                shutil.copy2(s, d)
            except OSError:
                pass

    for prof in ("Default", "Profile 1", "Profile 2"):
        sp = os.path.join(src, prof)
        if not os.path.isdir(sp):
            continue
        dp = os.path.join(dst, prof)
        os.makedirs(dp, exist_ok=True)
        for name in os.listdir(sp):
            s = os.path.join(sp, name)
            d = os.path.join(dp, name)
            if name.startswith("Singleton"):
                continue
            if os.path.isfile(s):
                if os.path.getsize(s) > 20 * 1024 * 1024:
                    continue
                try:
                    shutil.copy2(s, d)
                except OSError:
                    pass
            elif os.path.isdir(s):
                if any(name == skip or skip.endswith("/" + name) for skip in SKIP_DIRS):
                    continue
                try:
                    shutil.copytree(s, d, dirs_exist_ok=True, ignore=shutil.ignore_patterns(
                        "Cache", "Code Cache", "GPUCache", "Service Worker", "CacheStorage",
                        "IndexedDB", "blob_storage", "File System", "Session Storage",
                        "Extension State", "Extension Rules", "Media Cache",
                    ))
                except OSError:
                    pass


def launch_chrome(profile_dir: str, port: int) -> subprocess.Popen:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expanduser(r"~/AppData/Local/Google/Chrome/Application/chrome.exe"),
    ]
    exe = next((c for c in candidates if os.path.exists(c)), None)
    if not exe:
        raise SystemExit("Chrome executable not found")

    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "about:blank",
    ]
    print(f"Launching: {exe}")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cdp_cookies(port: int, domain: str) -> list[dict]:
    """Attach to a page target and pull cookies via Network.getAllCookies."""
    import websocket  # type: ignore

    targets = http_json(f"http://127.0.0.1:{port}/json")
    page = next(
        (t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")),
        None,
    )
    if not page:
        raise SystemExit("no page target available")

    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=30)
    try:
        ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                cookies = msg.get("result", {}).get("cookies", [])
                break
    finally:
        ws.close()

    return [c for c in cookies if domain in c.get("domain", "")]


def probe(cookie_header: str) -> bool:
    import httpx

    cookies: dict[str, str] = {}
    for part in cookie_header.split(";"):
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
    checks = [
        ("GET", "/api/v1/users/info", None),
        ("GET", "/openapi/v1/magicubes/balance", None),
        ("POST", "/openapi/v1/aigc/agentic/estimate-quota", {}),
        ("POST", "/api/v1/muse/predict/task/video/submit",
         {"predictType": "TXT_2_VIDEO", "promptArgs": {"prompt": "probe"}},
         {"X-CSRF-TOKEN": cookies.get("csrf_token", "")}),
    ]
    ok = False
    print("\n" + "=" * 68)
    print("PROBING THE EXTRACTED SESSION")
    print("=" * 68)
    with httpx.Client(headers=headers, cookies=cookies, timeout=30.0) as c:
        for item in checks:
            method, path, body = item[0], item[1], item[2]
            extra = item[3] if len(item) > 3 else None
            h = dict(headers)
            if extra:
                h.update(extra)
            try:
                r = c.request(method, BASE + path, json=body, headers=h)
            except Exception as exc:  # noqa: BLE001
                print(f"[{method:4}] {path}\n        ERROR {exc}")
                continue
            txt = r.text.strip()
            if len(txt) > 220:
                txt = txt[:220] + "..."
            mark = "   <-- ACCEPTED" if r.status_code == 200 else ""
            if r.status_code == 200:
                ok = True
            print(f"[{method:4}] {path}\n        HTTP {r.status_code}  {txt}{mark}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=DEFAULT_PROFILE,
                    help="Chrome User Data directory to copy from")
    ap.add_argument("--workdir", default=os.path.join(os.getcwd(), ".chrome-cdp"))
    ap.add_argument("--keep", action="store_true", help="keep the copied profile")
    ap.add_argument("--no-probe", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(args.profile):
        print(f"Chrome profile not found: {args.profile}")
        return 1

    print(f"Copying profile from: {args.profile}")
    prepare_profile(args.profile, args.workdir)

    proc = launch_chrome(args.workdir, DEBUG_PORT)
    info = wait_for_devtools(DEBUG_PORT)
    if not info:
        print("Chrome did not expose the DevTools port.")
        proc.terminate()
        return 1
    print(f"DevTools ready: {info.get('Browser')}")

    try:
        cookies = cdp_cookies(DEBUG_PORT, "modelscope")
    except Exception as exc:  # noqa: BLE001
        print(f"CDP cookie extraction failed: {exc}")
        proc.terminate()
        return 1

    print("\nmodelscope cookies found:")
    for c in cookies:
        print(f"  {c['name']:<16} domain={c.get('domain','')} "
              f"httpOnly={c.get('httpOnly')} expires={c.get('expires')}")

    if not cookies:
        print("\nNo modelscope cookies. The profile is not signed in to ModelScope,")
        print("or the OAuth flow never completed in it.")
        proc.terminate()
        return 1

    header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    print("\nCookie header:\n  " + header)
    with open("captured_cookies.txt", "w", encoding="utf-8") as fh:
        fh.write(header)
    print("\nSaved to captured_cookies.txt")

    ok = True
    if not args.no_probe:
        ok = probe(header)

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    if not args.keep:
        shutil.rmtree(args.workdir, ignore_errors=True)

    print("\nRESULT:", "session accepted" if ok else "session rejected")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
