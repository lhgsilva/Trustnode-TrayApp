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


def _load_users_payload(prefer_cloud_reads: bool = False) -> Dict[str, Any]:
    """Load the users_access config for login matching.

    Operator 2026-06-18 (offline auth): login MUST work when the cloud is
    unreachable AND when app_store's internal lock is held by a background
    thread doing cloud I/O. The previous implementation called
    app_store.get_bootstrap(), which acquires app_store._lock; on a
    machine with an exhausted Supabase pool the lock could be held for
    minutes, hanging login.

    New shape: read users_access directly from the SQLite file in
    read-only mode. SQLite WAL allows concurrent readers without blocking
    on writers, so this returns even when the app_store's Python lock is
    held by a stuck cloud sync. The `prefer_cloud_reads` flag is honored
    only on its second use (the auth.py login fallback that explicitly
    asks for a cloud-refreshed bootstrap) — at that point the caller has
    already accepted that cloud may fail.
    """
    merged_users: list[Dict[str, Any]] = []
    seen_usernames: set[str] = set()
    unscoped_current_user = ""

    if prefer_cloud_reads:
        # Caller explicitly requested cloud-refreshed read — use the
        # original path with get_bootstrap. This is best-effort; if the
        # lock is held it falls through to the direct-SQLite read below.
        try:
            data = app_store.get_bootstrap(prefer_cloud_reads=True) or {}
            ua = data.get("users_access") or {}
            if isinstance(ua, dict):
                unscoped_current_user = str(ua.get("current_user") or "")
                for u in (ua.get("users") or []):
                    if not isinstance(u, dict): continue
                    uname = str(u.get("username") or "").strip()
                    if not uname or uname in seen_usernames: continue
                    seen_usernames.add(uname)
                    merged_users.append(u)
        except Exception:
            pass

    # Direct SQLite read (lock-free) — works even when app_store._lock is
    # held. Pulls BOTH the unscoped users_access doc AND the scoped rows
    # in a single open. Read-only mode + 3s timeout means it returns
    # promptly even on a busy DB.
    try:
        import sqlite3, json as _json
        db_path = getattr(app_store, "_db_path", None)
        if db_path:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
            try:
                # Unscoped users_access (initial seed + master admin row).
                row = con.execute(
                    "SELECT payload_json FROM config_documents WHERE domain='users_access'"
                ).fetchone()
                if row:
                    try:
                        unscoped = _json.loads(row[0]) if isinstance(row[0], str) else row[0]
                    except Exception:
                        unscoped = None
                    if isinstance(unscoped, dict):
                        if not unscoped_current_user:
                            unscoped_current_user = str(unscoped.get("current_user") or "")
                        for u in (unscoped.get("users") or []):
                            if not isinstance(u, dict): continue
                            uname = str(u.get("username") or "").strip()
                            if not uname or uname in seen_usernames: continue
                            seen_usernames.add(uname)
                            merged_users.append(u)
                # Scoped users_access rows (per-edge user_access docs).
                for row in con.execute(
                    "SELECT payload_json FROM config_documents_scoped WHERE domain='users_access'"
                ).fetchall():
                    raw = row[0]
                    try:
                        scoped_payload = _json.loads(raw) if isinstance(raw, str) else raw
                    except Exception:
                        scoped_payload = None
                    if not isinstance(scoped_payload, dict): continue
                    for u in (scoped_payload.get("users") or []):
                        if not isinstance(u, dict): continue
                        uname = str(u.get("username") or "").strip()
                        if not uname or uname in seen_usernames: continue
                        seen_usernames.add(uname)
                        merged_users.append(u)
            finally:
                con.close()
    except Exception:
        # Best-effort: direct read may fail on a brand-new install where
        # the table doesn't exist yet. The default-admin fallback covers it.
        pass

    if merged_users:
        return {"users": merged_users, "current_user": unscoped_current_user}
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
    # Operator 2026-06-18: lock-free read for tenant_login_realm so login
    # never hangs on app_store._lock contention. Same rationale as
    # _load_users_payload — direct read-only SQLite, falls back silently
    # to "default" if the DB isn't accessible.
    try:
        import sqlite3, json as _json
        db_path = getattr(app_store, "_db_path", None)
        if db_path:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            try:
                row = con.execute(
                    "SELECT payload_json FROM config_documents WHERE domain='app_settings'"
                ).fetchone()
                if row:
                    try:
                        app_settings = _json.loads(row[0]) if isinstance(row[0], str) else row[0]
                    except Exception:
                        app_settings = None
                    if isinstance(app_settings, dict):
                        configured_tenant = normalize_tenant_id(
                            str(app_settings.get("tenant_login_realm") or app_settings.get("tenant_id") or "").strip()
                        )
            finally:
                con.close()
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
        # Last resort: portal/master may have created this user via the
        # cloud control-plane and the local cp_users row arrived from
        # cp_users_puller WITHOUT a usable password_hash (puller sets
        # password=None on the first sync). The user's real credential
        # lives in Supabase Auth as `<username>@trustnode.local`. Verify
        # the password against Auth, and on success accept the login
        # using the cp_users metadata for permissions/role/tenant.
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
