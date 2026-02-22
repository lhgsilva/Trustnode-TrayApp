from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.auth import create_access_token, decode_access_token, verify_password
from app.state import app_store

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
            }
        ]
    }


def _public_user(user_row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "username": str(user_row.get("username") or ""),
        "role": str(user_row.get("role") or "viewer"),
        "permissions": user_row.get("permissions") or {},
    }


@router.post("/login")
def login(payload: LoginRequest) -> Dict[str, Any]:
    users_access = _load_users_payload()
    users = users_access.get("users") if isinstance(users_access.get("users"), list) else []
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
        },
    }
