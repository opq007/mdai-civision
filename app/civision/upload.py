"""Uploading a reference image for image-to-video.

The civision page does not send a local file. It asks ModelScope for a
pre-signed OSS upload URL, PUTs the bytes there, and then asks for the
public download URL of what it just uploaded. That download URL is what
goes into the generation request.

The sequence, as the page performs it:

    POST /api/v1/rm/uploadUrl   {FileName, Type}  -> data.UploadUrl
    PUT  <UploadUrl>            raw bytes          (x-oss-meta-author: aliy)
    POST /api/v1/rm/downloadUrl {FileUrl, Type}    -> data.DownloadUrl

`Type` is the storage zone; the page uses `AIGC_MUSE_IMG_PRIVATEZONE` for
reference images.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from app.civision.client import (
    BASE,
    CivisionClient,
    CivisionError,
    NotAuthenticated,
)
from app.config import get_settings

IMAGE_ZONE = "AIGC_MUSE_IMG_PRIVATEZONE"

# The page refuses an upload URL that is not on one of these hosts.
ALLOWED_UPLOAD_HOSTS = (".aliyuncs.com",)


def _is_allowed_upload_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = parsed.hostname or ""
    return any(host == h[1:] or host.endswith(h) for h in ALLOWED_UPLOAD_HOSTS)


async def upload_image(
    client: CivisionClient, data: bytes, filename: str = "reference.png"
) -> str:
    """Upload image bytes and return the public URL upstream can fetch.

    Raises CivisionError when ModelScope refuses the upload or hands back a
    URL that is not on the expected host.
    """
    if not data:
        raise CivisionError("the reference image is empty", code="invalid_request_error",
                            status=400)

    name = Path(filename).name or "reference.png"
    if "." not in name:
        name += mimetypes.guess_extension("image/png") or ".png"

    settings = get_settings()
    headers = client._headers(json_body=True)
    headers.pop("content-type", None)

    async with httpx.AsyncClient(
        headers=headers,
        cookies=client.jar,
        timeout=settings.civision_timeout_s,
        trust_env=settings.trust_env_proxy,
    ) as http:
        try:
            # 1. Ask for an upload URL.
            resp = await http.post(
                f"{BASE}/api/v1/rm/uploadUrl",
                json={"FileName": name, "Type": IMAGE_ZONE},
            )
            body = _parse(resp)
            upload_url = body.get("UploadUrl") or ""
            if not upload_url:
                message = body.get("message") or body.get("Message") or "no upload URL"
                raise CivisionError(
                    f"could not get an upload URL: {message}", status=502
                )

            if not _is_allowed_upload_url(upload_url):
                raise CivisionError(
                    f"refusing to upload to an unexpected host: {upload_url}",
                    status=400,
                )

            # 2. PUT the bytes. This goes to Aliyun OSS, not ModelScope, so
            # it gets its own client: the pre-signed URL does not need the
            # session, and sending the cookie jar to another host would
            # leak it.
            async with httpx.AsyncClient(
                timeout=settings.civision_timeout_s,
                trust_env=settings.trust_env_proxy,
            ) as oss:
                put = await oss.put(
                    upload_url,
                    content=data,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "x-oss-meta-author": "aliy",
                    },
                )
            if put.status_code >= 400:
                raise CivisionError(
                    f"upload failed with status {put.status_code}", status=502
                )

            # 3. Ask for the public download URL.
            file_url = unquote(upload_url.split("?")[0])
            resp = await http.post(
                f"{BASE}/api/v1/rm/downloadUrl",
                json={"FileUrl": file_url, "Type": IMAGE_ZONE},
            )
            body = _parse(resp)
            download_url = body.get("DownloadUrl") or ""
            if not download_url:
                message = body.get("message") or body.get("Message") or "no download URL"
                raise CivisionError(
                    f"could not get a download URL: {message}", status=502
                )
        finally:
            # The upload endpoints can rotate csrf/token cookies too.
            for name, value in http.cookies.items():
                client.jar[name] = value

    return download_url


def _parse(resp: httpx.Response) -> dict:
    """Read a /api/v1/rm/* response.

    These endpoints use the api/v1 envelope, so the payload is under `Data`.
    An authentication failure here must be `NotAuthenticated`, not a generic
    upload error, or the account never gets marked expired.
    """
    try:
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        if resp.status_code == 401:
            message, upstream_code, request_id = CivisionClient._error_details(resp)
            raise NotAuthenticated(
                message,
                upstream_code=upstream_code,
                request_id=request_id,
            ) from exc
        raise CivisionError(
            f"unexpected upload response ({resp.status_code})", status=502
        ) from exc
    if not isinstance(body, dict):
        if resp.status_code >= 400:
            raise CivisionError(
                f"upload failed ({resp.status_code})", status=502
            )
        return {}

    code = body.get("Code")
    if code is None:
        code = body.get("code")
    message = body.get("Message") or body.get("message")
    request_id = (
        body.get("RequestId")
        or body.get("requestId")
        or body.get("request_id")
    )
    failed = (
        resp.status_code >= 400
        or code not in (0, 200, "0", "200", None)
        or body.get("Success") is False
        or body.get("success") is False
    )
    if failed:
        if (
            resp.status_code == 401
            or code == 10010101003
            or str(code).lower() == "invalidauthentication"
            or "not logged in" in str(message or "").lower()
        ):
            raise NotAuthenticated(
                str(message or f"upload failed ({resp.status_code})"),
                upstream_code=code,
                request_id=request_id,
            )
        raise CivisionError(
            f"upload failed: {message or resp.text[:200]}",
            status=502,
            upstream_code=code,
            request_id=request_id,
        )
    for key in ("Data", "data"):
        value = body.get(key)
        if isinstance(value, dict):
            return value
    return body


async def upload_image_from_url(
    client: CivisionClient, url: str, *, timeout: float = 60.0
) -> str:
    """Download an image and re-upload it so ModelScope can fetch it.

    An image that is already hosted somewhere ModelScope can reach is used
    as-is; anything else (including a locally stored file) is round-tripped
    through the upload flow.
    """
    if url.startswith("https://"):
        # ModelScope can usually fetch a public https URL directly, so skip
        # the upload unless it fails.
        return url

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"user-agent": client.user_agent},
        trust_env=get_settings().trust_env_proxy,
    ) as http:
        resp = await http.get(url)
        resp.raise_for_status()
        data = resp.content

    name = Path(urlparse(url).path).name or "reference.png"
    return await upload_image(client, data, name)


async def upload_image_bytes(
    client: CivisionClient, data: bytes, filename: str = "reference.png"
) -> str:
    return await upload_image(client, data, filename)
