import logging
import time
from collections import defaultdict
from typing import Any, Dict

from fastapi import APIRouter, Body, HTTPException, Request, Response
from app.services import access_policy as _access
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


def _client_is_remote(host: str) -> bool:
    try:
        return not _access.is_loopback_host(host)
    except Exception:
        return False


def _password_policy_error(password: str, role: str) -> str:
    """Operator 2026-08-21: admin/engineer >= 12 chars with letters AND digits;
    everyone else >= 8 chars. Returns '' when acceptable."""
    pw = str(password or "")
    r = str(role or "").strip().lower()
    if r in ("admin", "engineer", "super"):
        if len(pw) < 12 or not any(c.isalpha() for c in pw) or not any(c.isdigit() for c in pw):
            return "Password policy: admin/engineer passwords need at least 12 characters with letters and digits"
        return ""
    if len(pw) < 8:
        return "Password policy: at least 8 characters"
    return ""


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
def login(payload: LoginRequest, request: Request, response: Response) -> Dict[str, Any]:
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

    # Operator 2026-08-21 (Remote Access hardening): per-account lockout
    # (5 failures / 15 min -> 15 min lock) in addition to the per-IP limiter.
    _locked_until = auth_store.locked_until(username)
    if _locked_until:
        try:
            auth_store.record_login(username, ok=False, remote_ip=client_host, detail="locked_out")
        except Exception:
            pass
        raise HTTPException(status_code=423, detail=f"Account temporarily locked after repeated failures. Try again after {_locked_until} UTC.")
    _remote = _client_is_remote(client_host)

    # 1. Master-admin break-glass account. Works on every install, even
    #    a brand-new one where AuthStore is empty. ENV-overridable.
    master_user, master_pass = _master_admin_credentials()
    if username == master_user and verify_password(password, master_pass):
        # 2026-08-21: the break-glass account with its DEFAULT password never
        # works from the network — only from the edge desktop itself.
        _master_default = not bool(str(__import__("os").environ.get("TRUSTNODE_MASTER_ADMIN_PASSWORD", "") or "").strip())
        if _remote and _master_default:
            try:
                auth_store.record_login(username, ok=False, remote_ip=client_host, detail="master_default_remote_blocked")
            except Exception:
                pass
            raise HTTPException(status_code=403, detail="The master account with its default password cannot be used from the network. Set TRUSTNODE_MASTER_ADMIN_PASSWORD or sign in with a named admin account.")
        user_public = _public_user(_master_admin_user_row())
        _ttl = 4 * 3600 if _remote else 12 * 3600
        token = create_access_token(user_public, expires_seconds=_ttl)
        try:
            response.set_cookie(_access.SESSION_COOKIE, token, **_access.cookie_kwargs(request, _ttl))
        except Exception:
            pass
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
        _until = auth_store.record_failed_attempt(username)
        if _until:
            raise HTTPException(status_code=423, detail=f"Account temporarily locked after repeated failures. Try again after {_until} UTC.")
        raise HTTPException(status_code=401, detail="Invalid username or password")

    user_public = _public_user(hit)
    # Operator 2026-06-23: enforce `max_view_users` for view-role logins.
    # Admins, engineers, and master-admin are NOT counted. Failure of
    # the helper itself fails OPEN (login proceeds).
    try:
        from app.services import view_sessions
        ok_login, why = view_sessions.check_view_login_allowed(
            username=user_public.get("username", ""),
            role=str(user_public.get("role") or ""),
        )
        if not ok_login:
            try:
                auth_store.record_login(username, ok=False, remote_ip=client_host, detail="view_limit")
            except Exception:
                pass
            raise HTTPException(status_code=429, detail=why)
    except HTTPException:
        raise
    except Exception:
        pass
    # Operator 2026-08-21: shorter sessions for remote (LAN) logins; the
    # HttpOnly cookie lets browser-native loads of the LAN surfaces
    # (/trustnode/*/app/) pass the static-bundle guard.
    _ttl = 4 * 3600 if _remote else 12 * 3600
    token = create_access_token(user_public, expires_seconds=_ttl)
    try:
        auth_store.clear_failed_attempts(user_public["username"])
    except Exception:
        pass
    try:
        response.set_cookie(_access.SESSION_COOKIE, token, **_access.cookie_kwargs(request, _ttl))
    except Exception:
        pass
    if _remote:
        _access.audit("login.remote", outcome="ok", request=request,
                      payload={"sub": user_public.get("username"), "role": user_public.get("role"),
                               "tenant_id": user_public.get("tenant_id")}, details={})
    try:
        auth_store.record_login(user_public["username"], ok=True, remote_ip=client_host)
    except Exception:
        pass
    try:
        from app.services import view_sessions
        view_sessions.mark_active(user_public.get("username", ""), str(user_public.get("role") or ""),
                                  ip=client_host, surface="login")
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
    _policy_err = _password_policy_error(str(payload.new_password or ""), str(jwt_payload.get("role") or ""))
    if _policy_err:
        raise HTTPException(status_code=400, detail=_policy_err)

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


# =====================================================================
# Password reset (Operator 2026-06-24)
# =====================================================================
# Two endpoints:
#   POST /api/auth/forgot-password  { identifier }  → emails a one-time
#       reset link to the user's registered email.
#   POST /api/auth/reset-password   { token, new_password } → consumes
#       the token and sets the new password hash.
#
# Privacy: forgot-password always returns ok=True, EVEN IF the user
# doesn't exist or has no email. Otherwise the endpoint becomes a
# user-enumeration oracle. The customer sees the same "if an account
# matches, an email is on the way" message either way.

class _ForgotPasswordPayload(BaseModel):
    identifier: str = ""


class _ResetPasswordPayload(BaseModel):
    token: str = ""
    new_password: str = ""


def _send_password_reset_email(email_to: str, username: str, token: str) -> tuple[bool, str]:
    """Build + send the reset email using the customer's configured
    SMTP server. Returns (ok, reason). Reason is operator-facing so
    we can surface why a send failed in the audit log (NEVER in the
    API response — see the privacy note above)."""
    if not str(email_to or "").strip():
        return False, "no_email_for_user"
    try:
        bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
    except Exception:
        bootstrap = {}
    email_cfg = (bootstrap.get("email_notifications") or {}) if isinstance(bootstrap, dict) else {}
    smtp = email_cfg.get("smtp") if isinstance(email_cfg, dict) else {}
    if not isinstance(smtp, dict) or not str(smtp.get("host") or "").strip():
        return False, "no_smtp_configured"
    # The reset link points at the edge's own login page with a query
    # parameter the frontend interprets. Operator can change the host
    # via TRUSTNODE_PUBLIC_URL if they front the edge with nginx.
    import os as _os
    public_url = _os.environ.get("TRUSTNODE_PUBLIC_URL", "").strip().rstrip("/")
    if not public_url:
        host = str(smtp.get("from_origin_host") or "").strip().rstrip("/")
        public_url = host or "http://localhost:8000"
    reset_link = f"{public_url}/?reset_token={token}"
    subject = "TrustNode Edge — password reset"
    text_body = (
        f"Hello {username},\n\n"
        f"A password reset was requested for your TrustNode Edge account.\n"
        f"If this was you, open the link below within 30 minutes to set a new password:\n\n"
        f"  {reset_link}\n\n"
        f"If you didn't request this, ignore this email — your account is unchanged.\n\n"
        f"— TrustNode Edge"
    )
    html_body = (
        f"<p>Hello <strong>{username}</strong>,</p>"
        f"<p>A password reset was requested for your TrustNode Edge account.</p>"
        f"<p><a href=\"{reset_link}\">Click here to set a new password</a> "
        f"(link expires in 30 minutes).</p>"
        f"<p>If you didn't request this, ignore this email — your account is unchanged.</p>"
        f"<p style=\"color:#888;font-size:12px;\">— TrustNode Edge</p>"
    )
    try:
        from app.routers.notifications import _send_email, EmailRequest, SMTPConfig  # type: ignore
        req = EmailRequest(
            to=[email_to],
            cc=[],
            bcc=[],
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            smtp=SMTPConfig(**smtp),
            attachments=[],
        )
        result = _send_email(req)
        return bool(getattr(result, "ok", False)), str(getattr(result, "message", ""))
    except Exception as exc:
        return False, f"send_failed:{type(exc).__name__}:{exc}"


@router.post("/forgot-password")
def forgot_password(payload: _ForgotPasswordPayload, request: Request) -> Dict[str, Any]:
    """Email a password-reset link to the user. Always returns ok=True
    to avoid user-enumeration attacks."""
    client_host = str(getattr(request.client, "host", "") or "unknown")
    _check_rate_limit(client_host)
    identifier = str(payload.identifier or "").strip()
    if not identifier:
        return {"ok": True, "message": "If a matching account exists, a reset email has been sent."}
    try:
        u = auth_store.find_user_by_email_or_username(identifier)
    except Exception:
        u = None
    if not u:
        # User doesn't exist — return the same generic response. Audit it
        # so security folk can see suspicious patterns without revealing
        # to the caller.
        try:
            auth_store.record_login(identifier, ok=False, remote_ip=client_host, detail="forgot_password_unknown_user")
        except Exception:
            pass
        return {"ok": True, "message": "If a matching account exists, a reset email has been sent."}
    username = str(u.get("username") or "")
    email = str(u.get("email") or "")
    token = None
    try:
        token = auth_store.issue_reset_token(username, ttl_seconds=1800)
    except Exception:
        token = None
    if not token:
        try:
            auth_store.record_login(username, ok=False, remote_ip=client_host, detail="forgot_password_token_failed")
        except Exception:
            pass
        return {"ok": True, "message": "If a matching account exists, a reset email has been sent."}
    sent_ok, reason = _send_password_reset_email(email, username, token)
    try:
        auth_store.record_login(
            username, ok=bool(sent_ok), remote_ip=client_host,
            detail=f"forgot_password:{'sent' if sent_ok else reason}",
        )
    except Exception:
        pass
    return {"ok": True, "message": "If a matching account exists, a reset email has been sent."}


@router.post("/reset-password")
def reset_password(payload: _ResetPasswordPayload, request: Request) -> Dict[str, Any]:
    """Consume a reset token and set a new password."""
    client_host = str(getattr(request.client, "host", "") or "unknown")
    _check_rate_limit(client_host)
    token = str(payload.token or "").strip()
    new_password = str(payload.new_password or "")
    if not token or len(new_password) < 8:
        raise HTTPException(status_code=400, detail="Token and a password (min 8 chars) are required")
    try:
        username = auth_store.consume_reset_token(token)
    except Exception:
        username = None
    if not username:
        try:
            auth_store.record_login("", ok=False, remote_ip=client_host, detail="reset_password_invalid_token")
        except Exception:
            pass
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    try:
        _role_reset = str((auth_store.get_user(username) or {}).get("role") or "")
    except Exception:
        _role_reset = ""
    _policy_err = _password_policy_error(new_password, _role_reset)
    if _policy_err:
        raise HTTPException(status_code=400, detail=_policy_err)
    # Hash with the same scheme app_store uses.
    try:
        new_hash = app_store._hash_password_if_needed(new_password)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Password hashing failed: {exc}")
    ok = False
    try:
        ok = auth_store.set_user_password(username, new_hash)
    except Exception:
        ok = False
    try:
        auth_store.record_login(
            username, ok=bool(ok), remote_ip=client_host,
            detail="reset_password:set" if ok else "reset_password:db_failed",
        )
    except Exception:
        pass
    if not ok:
        raise HTTPException(status_code=500, detail="Could not update password")
    return {"ok": True, "message": "Password updated. You can now sign in."}


# ---------------------------------------------------------------------------
# Operator 2026-08-21 — Remote Access session endpoints
# ---------------------------------------------------------------------------
@router.post("/session-cookie")
def session_cookie(request: Request, response: Response) -> Dict[str, Any]:
    """Set the HttpOnly session cookie for an already-issued Bearer token
    (the LAN login page calls this so browser-native loads of the surface
    bundles pass the static guard)."""
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip() if auth.startswith("Bearer ") else ""
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    try:
        body = decode_access_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc
    import time as _t
    ttl = max(60, int(body.get("exp", 0)) - int(_t.time()))
    response.set_cookie(_access.SESSION_COOKIE, token, **_access.cookie_kwargs(request, ttl))
    return {"ok": True}


@router.post("/logout")
def logout(request: Request, response: Response) -> Dict[str, Any]:
    """Clear the session cookie and free the live-session slot. Works with an
    expired token too (public path)."""
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip() if auth.startswith("Bearer ") else ""
    if not token:
        token = str(request.cookies.get(_access.SESSION_COOKIE) or "")
    username = ""
    if token:
        try:
            username = str(decode_access_token(token).get("sub") or "")
        except Exception:
            username = ""
    try:
        from app.services import view_sessions
        if username:
            view_sessions.forget(username)
    except Exception:
        pass
    response.delete_cookie(_access.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.post("/unlock")
def unlock_account(request: Request, body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """Admin: clear a lockout for a user."""
    payload = getattr(request.state, "user_payload", None) or {}
    if str(payload.get("role") or "").lower() not in ("admin", "super"):
        raise HTTPException(status_code=403, detail="Admin role required")
    username = str(body.get("username") or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username required")
    auth_store.clear_failed_attempts(username)
    _access.audit("account.unlock", outcome="ok", request=request, payload=payload, details={"unlocked_user": username})
    return {"ok": True, "username": username}
