import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict

from app.state import app_store
from app.tenant import get_current_tenant, normalize_tenant_id


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    pad = "=" * ((4 - (len(raw) % 4)) % 4)
    return base64.urlsafe_b64decode((raw + pad).encode("utf-8"))


def hash_password(password: str, iterations: int = 120_000) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64url_encode(salt)}${_b64url_encode(digest)}"


def verify_password(password: str, stored: str) -> bool:
    raw = str(stored or "")
    if raw.startswith("pbkdf2_sha256$"):
        try:
            _, iter_txt, salt_txt, hash_txt = raw.split("$", 3)
            iterations = int(iter_txt)
            try:
                salt = bytes.fromhex(salt_txt)
            except Exception:
                salt = _b64url_decode(salt_txt)
            try:
                expected = bytes.fromhex(hash_txt)
            except Exception:
                expected = _b64url_decode(hash_txt)
            digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
            return hmac.compare_digest(digest, expected)
        except Exception:
            return False
    return hmac.compare_digest(password, raw)


def _secret_key() -> str:
    env = os.environ.get("TRUSTNODE_AUTH_SECRET", "").strip()
    if env:
        return env
    return app_store.get_or_create_auth_secret()


def create_access_token(user: Dict[str, Any], expires_seconds: int = 12 * 3600) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user.get("username") or ""),
        "role": str(user.get("role") or "viewer"),
        "permissions": user.get("permissions") or {},
        "modules": user.get("modules") or [],
        "tenant_id": normalize_tenant_id(str(user.get("tenant_id") or get_current_tenant())),
        "iat": now,
        "exp": now + int(expires_seconds),
    }
    header = {"alg": "HS256", "typ": "JWT"}
    part1 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    part2 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{part1}.{part2}".encode("utf-8")
    sig = hmac.new(_secret_key().encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{part1}.{part2}.{_b64url_encode(sig)}"


def decode_access_token(token: str) -> Dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) != 3:
        raise ValueError("Invalid token format")
    part1, part2, sig_txt = parts
    signing_input = f"{part1}.{part2}".encode("utf-8")
    expected = hmac.new(_secret_key().encode("utf-8"), signing_input, hashlib.sha256).digest()
    got = _b64url_decode(sig_txt)
    if not hmac.compare_digest(expected, got):
        raise ValueError("Invalid token signature")
    payload = json.loads(_b64url_decode(part2).decode("utf-8"))
    if int(payload.get("exp", 0)) < int(time.time()):
        raise ValueError("Token expired")
    return payload
