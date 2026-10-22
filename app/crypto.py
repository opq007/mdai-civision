"""Symmetric encryption for secrets at rest (cookie jars, proxy passwords).

The key is derived from SECRET_KEY so no extra configuration is needed.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet

from app.config import get_settings


def _key() -> bytes:
    secret = get_settings().secret_key.encode("utf-8")
    return base64.urlsafe_b64encode(hashlib.sha256(secret).digest())


def _fernet() -> Fernet:
    return Fernet(_key())


def encrypt(value: str) -> str:
    if not value:
        return ""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except Exception:
        # A key change (or a value written by an older build) must not take
        # the service down; treat it as "no credential stored".
        return ""


def hash_secret(value: str) -> str:
    """One-way hash for API keys."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_api_key() -> str:
    return "sk-civ-" + base64.urlsafe_b64encode(os.urandom(24)).decode("ascii").rstrip("=")
