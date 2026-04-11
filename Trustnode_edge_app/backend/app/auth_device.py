from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    pad = "=" * ((4 - (len(raw) % 4)) % 4)
    return base64.urlsafe_b64decode((raw + pad).encode("utf-8"))


def _device_secret() -> str:
    secret = os.environ.get("TRUSTNODE_DEVICE_AUTH_SECRET", "").strip()
    if not secret:
        raise RuntimeError("TRUSTNODE_DEVICE_AUTH_SECRET is required for device ingest auth")
    return secret


def create_device_access_token(*, tenant_id: str, gateway_id: str, expires_seconds: int = 3600) -> str:
    now = int(time.time())
    payload = {
        "typ": "device",
        "tenant_id": str(tenant_id or "default"),
        "gateway_id": str(gateway_id or ""),
        "iat": now,
        "exp": now + int(max(60, expires_seconds)),
    }
    header = {"alg": "HS256", "typ": "JWT"}
    part1 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    part2 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{part1}.{part2}".encode("utf-8")
    sig = hmac.new(_device_secret().encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{part1}.{part2}.{_b64url_encode(sig)}"


def decode_device_access_token(token: str) -> Dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) != 3:
        raise ValueError("Invalid token format")
    p1, p2, p3 = parts
    signing_input = f"{p1}.{p2}".encode("utf-8")
    expected = hmac.new(_device_secret().encode("utf-8"), signing_input, hashlib.sha256).digest()
    got = _b64url_decode(p3)
    if not hmac.compare_digest(expected, got):
        raise ValueError("Invalid token signature")
    payload = json.loads(_b64url_decode(p2).decode("utf-8"))
    if payload.get("typ") != "device":
        raise ValueError("Invalid token type")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise ValueError("Token expired")
    if not str(payload.get("tenant_id") or "").strip() or not str(payload.get("gateway_id") or "").strip():
        raise ValueError("Token missing tenant/gateway scope")
    return payload
