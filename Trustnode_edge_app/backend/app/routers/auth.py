import logging
import time
from collections import defaultdict
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.auth import create_access_token, decode_access_token, verify_password
from app.state import app_store
from app.tenant import get_current_tenant, normalize_tenant_id

logger = logging.getLogger(__name__)

_login_attempts: dict = defaultdict(list)  # ip -> [timestamp, ...]
_LOGIN_MAX = 10
_LOGIN_WINDOW = 60  # seconds


def _check_rate_limit(ip: str):
    now = time.time()
    attempts = _login_attempts[ip]
    # Remove old entries
    _login_attempts[ip] = [t for t in attempts if now - t < _LOGIN_WINDOW]
    if len(_login_attempts[ip]) >= _LOGIN_MAX:
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again in 60 seconds.")
    _login_attempts[ip].append(now)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


def _load_users_payload() -> Dict[str, Any]:
    data = app_store.get_bootstrap() or {}
    users_access = data.get("users_access") or {}
    if isinstance(users_access, dict) and isinstance(users_access.get("users"), list) and users_access.get("users"):
        return users_access
    return {
        "users": [
            {
                "username": "admin",
                "password": "admin",
                "role": "admin",
                "permissions": {},
                "tenant_id": normalize_tenant_id(get_current_tenant()),
            }
        ]
    }


def _public_user(user_row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "username": str(user_row.get("username") or ""),
        "role": str(user_row.get("role") or "viewer"),
        "permissions": user_row.get("permissions") or {},
        "tenant_id": normalize_tenant_id(str(user_row.get("tenant_id") or get_current_tenant())),
    }


@router.post("/login")
def login(payload: LoginRequest, request: Request) -> Dict[str, Any]:
    client_host = str(getattr(request.client, "host", "") or "unknown")
    _check_rate_limit(client_host)
    users_access = _load_users_payload()
    users = users_access.get("users") if isinstance(users_access.get("users"), list) else []
    if not users:
        raise HTTPException(status_code=503, detail="No users configured. Complete first-run setup.")
    username = str(payload.username or "").strip()
    password = str(payload.password or "")
    hit = None
    for u in users:
        if not isinstance(u, dict):
            continue
        if str(u.get("username") or "").strip() != username:
            continue
        if verify_password(password, str(u.get("password") or "")):
            hit = u
            break
    if not hit:
        logger.warning("Failed login attempt: user=%s ip=%s", username, client_host)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    user_public = _public_user(hit)
    token = create_access_token(user_public)
    return {"ok": True, "token": token, "user": user_public}


@router.get("/me")
def me(request: Request) -> Dict[str, Any]:
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip() if auth.startswith("Bearer ") else ""
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    try:
        payload = decode_access_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc
    return {
        "ok": True,
        "user": {
            "username": str(payload.get("sub") or ""),
            "role": str(payload.get("role") or "viewer"),
            "permissions": payload.get("permissions") or {},
            "tenant_id": normalize_tenant_id(str(payload.get("tenant_id") or get_current_tenant())),
        },
    }
