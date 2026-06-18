import logging
import time
from collections import defaultdict
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.auth import create_access_token, decode_access_token, verify_password
from app.state import app_store, auth_store, control_plane_store
from app.tenant import get_current_tenant, normalize_tenant_id

logger = logging.getLogger(__name__)

_login_attempts: dict = defaultdict(list)  # ip -> [timestamp, ...]
_LOGIN_MAX = 10
_LOGIN_WINDOW = 60  # seconds


def _verify_against_supabase_auth(username: str, password: str) -> bool:
    """Best-effort check: does Supabase Auth accept this username/password?

    Edge users created by the portal/master flow land in Supabase Auth as
    `<username>@trustnode.local`. The local cp_users row mirrored down by
    cp_users_puller has no password_hash (puller can't read the cloud
    hash), so the user couldn't otherwise log into the edge UI. We exchange
    the credentials for a session via the Auth password grant; on 200 we
    accept the local login, on anything else we reject.

    Requires TRUSTNODE_SUPABASE_URL + a Supabase **anon** key (anon is the
    correct key for /auth/v1 password grant — service_role bypasses Auth).
    Returns False silently on any failure so the login flow continues.
    """
    import os
    import requests
    sb_url = (os.environ.get("TRUSTNODE_SUPABASE_URL") or "").strip().rstrip("/")
    sb_anon = (os.environ.get("TRUSTNODE_SUPABASE_ANON_KEY") or "").strip()
    if not sb_url or not sb_anon:
        return False
    email = f"{username}@trustnode.local"
    try:
        r = requests.post(
            f"{sb_url}/auth/v1/token?grant_type=password",
            headers={"apikey": sb_anon, "Content-Type": "application/json"},
            json={"email": email, "password": password},
            timeout=6,
        )
        return r.status_code == 200
    except Exception:
        return False


def _check_rate_limit(ip: str):
    now = time.time()
    attempts = _login_attempts[ip]
    # Remove old entries
    _login_attempts[ip] = [t for t in attempts if now - t < _LOGIN_WINDOW]
    if len(_login_attempts[ip]) >= _LOGIN_MAX:
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again in 60 seconds.")
    _login_attempts[ip].append(now)

router = APIRouter(prefix="/api/auth", tags=["auth"])


_FULL_PERMISSIONS: Dict[str, bool] = {
    "devices": True,
    "tags": True,
    "triggers_and_limits": True,
    "alarms": True,
    "reporting": True,
    "data_log": True,
    "gateway_configuration": True,
    "gateway_runtime_control": True,
    "database": True,
    "database_overview": True,
    "database_inspector": True,
    "backup_and_retention": True,
    "website_and_env": True,
    "email_and_notifications": True,
    "scheduled_reports": True,
    "frontend_source": True,
    "users_and_access_control": True,
    "control_plane": True,
    "interface": True,
}


def _master_admin_credentials() -> tuple[str, str]:
    user = str(__import__("os").environ.get("TRUSTNODE_MASTER_ADMIN_USER", "admin") or "admin").strip() or "admin"
    pwd = str(__import__("os").environ.get("TRUSTNODE_MASTER_ADMIN_PASSWORD", "admin") or "admin")
    return user, pwd


def _master_admin_user_row() -> Dict[str, Any]:
    user, _ = _master_admin_credentials()
    return {
        "username": user,
        "role": "admin",
        "permissions": dict(_FULL_PERMISSIONS),
        "modules": [],
        "tenant_id": "default",
    }


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


def _public_user(user_row: Dict[str, Any]) -> Dict[str, Any]:
    """Project an AuthStore user row (or master-admin synthetic row)
    into the JWT-signing shape. Admin role gets the full permission set
    OR'd with whatever the row had — same contract as before so the
    frontend's permission gates keep working unchanged.
    """
    role = str(user_row.get("role") or "viewer")
    permissions = user_row.get("permissions") or {}
    if role.lower() == "admin":
        merged = dict(_FULL_PERMISSIONS)
        if isinstance(permissions, dict):
            merged.update({k: bool(v) for k, v in permissions.items()})
        permissions = merged
    return {
        "username": str(user_row.get("username") or ""),
        "role": role,
        "permissions": permissions,
        "modules": user_row.get("modules") or [],
        "tenant_id": normalize_tenant_id(str(user_row.get("tenant_id") or "default")),
        # True when an admin issued a temporary password through the
        # portal. The frontend prompts the user to choose a new password
        # before they reach any application page.
        "must_change_password": bool(user_row.get("must_change_password") or False),
    }


@router.post("/login")
def login(payload: LoginRequest, request: Request) -> Dict[str, Any]:
    """Operator 2026-06-18 — clean auth path.

    Hot path uses AuthStore EXCLUSIVELY: a dedicated SQLite file with no
    shared locks and no cloud I/O. Result: login completes in single-
    digit milliseconds regardless of cloud state, Supabase pool, or
    background sync threads. Offline-safe by construction.

    Order of checks:
      1. Rate limit (DoS guard, in-memory)
      2. Master-admin (env-configurable break-glass account)
      3. AuthStore lookup + bcrypt/pbkdf2 verify
      4. control_plane_store (local SQLite, separate lock, fast)
      5. Supabase Auth verify (already has a 6 s HTTP timeout)

    Cloud-bootstrap refresh fallback was REMOVED. cp_users_puller runs
    in the background and writes new portal-created users straight into
    AuthStore within ~30 s, so the convenience-retry was buying very
    little and historically caused indefinite hangs when Supabase
    misbehaved.
    """
    client_host = str(getattr(request.client, "host", "") or "unknown")
    _check_rate_limit(client_host)

    username = str(payload.username or "").strip()
    password = str(payload.password or "")
    if not username:
        raise HTTPException(status_code=400, detail="Username required")

    # 1. Master-admin break-glass account. Works on every install, even
    #    a brand-new one where AuthStore is empty. ENV-overridable.
    master_user, master_pass = _master_admin_credentials()
    if username == master_user and verify_password(password, master_pass):
        user_public = _public_user(_master_admin_user_row())
        token = create_access_token(user_public)
        auth_store.record_login(username, ok=True, remote_ip=client_host, detail="master")
        return {"ok": True, "token": token, "user": user_public}

    # 2. AuthStore — the dedicated, lock-free, offline-safe path. This is
    #    where 99% of customer logins resolve.
    hit: Dict[str, Any] | None = None
    try:
        u = auth_store.get_user(username)
        if u and str(u.get("status") or "active") == "active":
            if verify_password(password, str(u.get("password_hash") or "")):
                hit = u
    except Exception as exc:
        logger.warning("AuthStore lookup failed for user=%s: %s", username, exc)

    # 3. Control-plane local SQLite (separate DB, separate lock). Covers
    #    portal-pushed users the puller mirrored down. Fast — no network.
    if not hit:
        try:
            cp_hit = control_plane_store.authenticate_user(
                tenant_id=normalize_tenant_id(get_current_tenant()),
                username=username,
                password=password,
            )
            if cp_hit:
                hit = {
                    "username": cp_hit.get("username"),
                    "role": cp_hit.get("role"),
                    "permissions": cp_hit.get("permissions") or {},
                    "modules": cp_hit.get("modules") or [],
                    "tenant_id": cp_hit.get("tenant_id"),
                    "must_change_password": cp_hit.get("must_change_password"),
                }
        except Exception:
            pass
    if not hit:
        try:
            cp_any = control_plane_store.authenticate_user_any_tenant(
                username=username,
                password=password,
            )
            if cp_any:
                hit = {
                    "username": cp_any.get("username"),
                    "role": cp_any.get("role"),
                    "permissions": cp_any.get("permissions") or {},
                    "modules": cp_any.get("modules") or [],
                    "tenant_id": cp_any.get("tenant_id"),
                    "must_change_password": cp_any.get("must_change_password"),
                }
        except Exception:
            pass

    # 4. Supabase Auth verify — for portal-created users whose hash
    #    didn't come down with the puller. Already has a 6 s HTTP timeout
    #    inside _verify_against_supabase_auth.
    if not hit:
        try:
            tid = normalize_tenant_id(get_current_tenant())
            cp_rows = control_plane_store.list_users(tenant_id=tid)
            cp_local = next(
                (u for u in (cp_rows or [])
                 if str(u.get("username") or "").lower() == username.lower()),
                None,
            )
            if cp_local and _verify_against_supabase_auth(username, password):
                hit = {
                    "username": cp_local.get("username"),
                    "role": cp_local.get("role"),
                    "permissions": cp_local.get("permissions") or {},
                    "modules": cp_local.get("modules") or [],
                    "tenant_id": cp_local.get("tenant_id"),
                    "must_change_password": cp_local.get("must_change_password"),
                }
        except Exception:
            pass

    if not hit:
        logger.warning("Failed login attempt: user=%s ip=%s", username, client_host)
        try:
            auth_store.record_login(username, ok=False, remote_ip=client_host, detail="invalid_credentials")
        except Exception:
            pass
        raise HTTPException(status_code=401, detail="Invalid username or password")

    user_public = _public_user(hit)
    token = create_access_token(user_public)
    try:
        auth_store.record_login(user_public["username"], ok=True, remote_ip=client_host)
    except Exception:
        pass
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
    role = str(payload.get("role") or "viewer")
    permissions = payload.get("permissions") or {}
    if role.lower() == "admin":
        merged = dict(_FULL_PERMISSIONS)
        if isinstance(permissions, dict):
            merged.update({k: bool(v) for k, v in permissions.items()})
        permissions = merged
    return {
        "ok": True,
        "user": {
            "username": str(payload.get("sub") or ""),
            "role": role,
            "permissions": permissions,
            "modules": payload.get("modules") or [],
            "tenant_id": normalize_tenant_id(str(payload.get("tenant_id") or get_current_tenant())),
        },
    }


@router.post("/change-password")
def change_password(payload: ChangePasswordRequest, request: Request) -> Dict[str, Any]:
    """User changes their own password (typically after being issued a
    temporary password). Clears the must_change_password flag, mirrors
    the new credential to Supabase Auth so Lite stays in sync, and
    returns a fresh JWT (with must_change_password=False)."""
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip() if auth.startswith("Bearer ") else ""
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    try:
        jwt_payload = decode_access_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc

    username = str(jwt_payload.get("sub") or "").strip()
    tenant_id = normalize_tenant_id(str(jwt_payload.get("tenant_id") or get_current_tenant()))
    if not username:
        raise HTTPException(status_code=401, detail="No subject in token")
    if not str(payload.new_password or "").strip():
        raise HTTPException(status_code=400, detail="new_password_required")

    # Verify the current password through the same path that issued the
    # token. Force-change-password sessions still know their current
    # (temporary) password, so this guards against stolen tokens.
    auth_ok = control_plane_store.authenticate_user(
        tenant_id=tenant_id, username=username, password=payload.current_password,
    )
    if not auth_ok:
        # Try the any-tenant fallback used during login.
        auth_ok = control_plane_store.authenticate_user_any_tenant(
            username=username, password=payload.current_password,
        )
    if not auth_ok:
        raise HTTPException(status_code=401, detail="current_password_incorrect")

    row = control_plane_store.set_user_password(
        tenant_id=tenant_id, username=username,
        password=payload.new_password, must_change=False,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="user_not_found")

    # Mirror the rotated password to Supabase Auth so Lite stays in sync.
    try:
        from app.services.lite_user_mirror import mirror_user_upsert
        mirror_user_upsert(
            tenant_id=tenant_id, username=username,
            password=payload.new_password,
            role=str(row.get("role") or "viewer"),
            email=str(row.get("email") or ""),
        )
    except Exception:
        pass

    # Re-mint the JWT with must_change_password cleared so the frontend
    # can drop the change-password modal.
    new_user = {
        "username": username,
        "role": str(row.get("role") or "viewer"),
        "permissions": auth_ok.get("permissions") or {},
        "modules": auth_ok.get("modules") or [],
        "tenant_id": tenant_id,
        "must_change_password": False,
    }
    new_token = create_access_token(new_user)
    return {"ok": True, "token": new_token, "user": new_user}
