import logging
import time
from collections import defaultdict
from typing import Any, Dict, Iterable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.auth import create_access_token, decode_access_token, verify_password
from app.state import app_store, control_plane_store
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


def _load_users_payload(prefer_cloud_reads: bool = False) -> Dict[str, Any]:
    # Local-first for speed; caller can request cloud-refreshed bootstrap when needed.
    data = app_store.get_bootstrap(prefer_cloud_reads=prefer_cloud_reads) or {}
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


def _iter_users(users_access: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    users = users_access.get("users") if isinstance(users_access.get("users"), list) else []
    for row in users or []:
        if isinstance(row, dict):
            yield row


def _match_user(users_access: Dict[str, Any], username: str, password: str) -> Dict[str, Any] | None:
    for u in _iter_users(users_access):
        if str(u.get("username") or "").strip() != username:
            continue
        if verify_password(password, str(u.get("password") or "")):
            return u
    return None


def _public_user(user_row: Dict[str, Any]) -> Dict[str, Any]:
    configured_tenant = "default"
    try:
        # Auth path must be fast and deterministic; use local cached bootstrap only.
        bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False)
        app_settings = bootstrap.get("app_settings") if isinstance(bootstrap, dict) else {}
        if isinstance(app_settings, dict):
            configured_tenant = normalize_tenant_id(
                str(app_settings.get("tenant_login_realm") or app_settings.get("tenant_id") or "").strip()
            )
    except Exception:
        configured_tenant = "default"
    fallback_tenant = configured_tenant if configured_tenant != "default" else normalize_tenant_id(get_current_tenant())
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
        "tenant_id": normalize_tenant_id(str(user_row.get("tenant_id") or fallback_tenant)),
        # True when an admin issued a temporary password through the
        # portal. The frontend prompts the user to choose a new password
        # before they reach any application page.
        "must_change_password": bool(user_row.get("must_change_password") or False),
    }


@router.post("/login")
def login(payload: LoginRequest, request: Request) -> Dict[str, Any]:
    client_host = str(getattr(request.client, "host", "") or "unknown")
    _check_rate_limit(client_host)
    users_access = _load_users_payload(prefer_cloud_reads=False)
    local_users = list(_iter_users(users_access))
    if not local_users:
        raise HTTPException(status_code=503, detail="No users configured. Complete first-run setup.")
    username = str(payload.username or "").strip()
    password = str(payload.password or "")
    master_user, master_pass = _master_admin_credentials()
    if username == master_user and verify_password(password, master_pass):
        user_public = _public_user(_master_admin_user_row())
        token = create_access_token(user_public)
        return {"ok": True, "token": token, "user": user_public}
    hit = _match_user(users_access, username, password)
    if not hit:
        # Control-plane users are authoritative for tenant-scoped cloud users.
        # Keep this ahead of cloud-bootstrap refresh to avoid login latency spikes
        # on cloud runtimes where prefer_cloud_reads can be slow.
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
            hit = None
    if not hit:
        # Fallback for customer cloud logins on shared host when user is scoped
        # to a single tenant but host tenant resolution is still default.
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
            hit = None
    if not hit:
        # Retry once with cloud-refreshed bootstrap so newly created legacy
        # users_access users on cloud/local become valid after propagation.
        try:
            cloud_users_access = _load_users_payload(prefer_cloud_reads=True)
            hit = _match_user(cloud_users_access, username, password)
            if hit:
                users_access = cloud_users_access
        except Exception:
            hit = None
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
