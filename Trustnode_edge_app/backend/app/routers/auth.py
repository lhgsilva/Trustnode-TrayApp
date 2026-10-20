import logging
import time
from collections import defaultdict
from typing import Any, Dict, Optional

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


def _refresh_grace_seconds() -> int:
    """How long after expiry a session may still be renewed.

    Covers a laptop that slept or a panel that lost the network for a while.
    Long enough to be useful, short enough that a token recovered from an old
    log or backup is already dead. Tunable for sites that want it stricter.
    """
    import os as _os
    try:
        return max(0, int(_os.environ.get("TRUSTNODE_SESSION_REFRESH_GRACE_S", "")
                          or 30 * 60))
    except ValueError:
        return 30 * 60


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
    #
    # 2026-08-22: a licensed seat is handed to a person by e-mail, so the
    # address has to work as a login. resolve_login_identity() maps it to the
    # internal username (username is tried first, so no existing account can be
    # shadowed, and an ambiguous address never resolves). Everything downstream
    # — the JWT `sub`, the audit trail, token revocation, view_sessions — keeps
    # keying on the username exactly as before.
    try:
        from app.services import seats as _seats
        resolved = _seats.resolve_login_identity(username)
        if resolved and resolved != username:
            logger.info("login: e-mail %s resolved to user %s", username, resolved)
            username = resolved
    except Exception:
        pass
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


@router.post("/refresh")
def refresh(request: Request, response: Response) -> Dict[str, Any]:
    """Renew a session that is already authenticated.

    Called by the frontend well before expiry, and again on the first 401 it
    sees. Without it a token simply runs out under a screen that is still
    being watched.
    """
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip() if auth.startswith("Bearer ") else ""
    if not token:
        token = str(request.cookies.get(_access.SESSION_COOKIE) or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")

    # Signature first, and always. verify_exp=False only defers the expiry
    # decision to the grace check below - it does not weaken verification.
    try:
        payload = decode_access_token(token, verify_exp=False)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc

    now = int(time.time())
    exp = int(payload.get("exp") or 0)
    grace = _refresh_grace_seconds()
    if exp and exp + grace < now:
        # Long dead. Renewing this would turn any old token into a permanent
        # key, so the operator signs in again.
        raise HTTPException(status_code=401,
                            detail="Session expired. Please sign in again.")

    username = str(payload.get("sub") or "")
    if not username:
        raise HTTPException(status_code=401, detail="Invalid token: no subject")

    # Revocation must survive a refresh, or "Revoke" would only last until the
    # revoked screen renewed itself.
    try:
        from app.state import auth_store as _as_tv
        current_tv = int(_as_tv.get_token_version(username))
        if int(payload.get("tv") or 0) != current_tv:
            raise HTTPException(status_code=401,
                                detail="Session was revoked. Please sign in again.")
    except HTTPException:
        raise
    except Exception:
        # The token-version store is not reachable; refusing every refresh
        # would strand every open screen, so the signature and grace checks
        # above carry this one.
        pass

    user_public = {
        "username": username,
        "role": payload.get("role") or "viewer",
        "permissions": payload.get("permissions") or {},
        "modules": payload.get("modules") or [],
        "tenant_id": payload.get("tenant_id") or get_current_tenant(),
    }
    client_host = str(getattr(request.client, "host", "") or "unknown")
    _ttl = 4 * 3600 if _client_is_remote(client_host) else 12 * 3600
    fresh = create_access_token(user_public, expires_seconds=_ttl)
    try:
        response.set_cookie(_access.SESSION_COOKIE, fresh,
                            **_access.cookie_kwargs(request, _ttl))
    except Exception:
        pass
    return {"ok": True, "token": fresh, "expires_in": _ttl, "user": user_public}


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
    #: "code" asks for a 6-digit code by email (the default from 2026-10-20);
    #: "link" keeps the original SMTP reset link for an edge with no
    #: internet, which Supabase cannot reach.
    delivery: str = "code"


class _CodeRequestPayload(BaseModel):
    """Ask for a sign-in code. No password involved."""
    identifier: str = ""


class _CodeVerifyPayload(BaseModel):
    identifier: str = ""
    code: str = ""


class _CodeResetPayload(BaseModel):
    identifier: str = ""
    code: str = ""
    new_password: str = ""


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
    # 2026-10-20: was u["email"], which skipped the company fallback - so a
    # user with no address of their own got neither a code nor a link.
    email = _recovery_address(u)
    # 2026-10-20: a code by default, the link only when asked for. The link
    # still exists because an edge with no internet cannot be reached by
    # Supabase, and that is exactly the machine whose operator is locked out.
    if str(payload.delivery or "code").strip().lower() == "code":
        from app.services import recovery_codes
        if recovery_codes.is_deliverable(email):
            sent, reason = recovery_codes.send_code(
                email, otp_type=recovery_codes.OTP_RECOVERY)
            try:
                auth_store.record_login(
                    username, ok=bool(sent), remote_ip=client_host,
                    detail=f"forgot_password_code:{reason}")
            except Exception:
                pass
            if sent:
                return {"ok": True, "message": "If a matching account exists, a reset email has been sent."}
        # Supabase unreachable or no usable address: fall through to the link,
        # rather than leaving the operator with nothing.
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


# ---------------------------------------------------------------- codes
#
# 2026-10-20, requested: "instead of forget password, the users can request a
# code ... I want to use supabase features for that", and sign-in by code
# alone ("email should be enough").
#
# Supabase owns generation, expiry and delivery for all three endpoints
# below, so there is no second code store here to keep, expire or leak. The
# edge's existing SMTP link flow and its offline file-based recovery are left
# exactly as they are: a machine on a factory LAN with no internet cannot be
# rescued by an email service, and removing that path would lock people out.
#
# Every one of these answers the SAME way whether or not the account exists.


_GENERIC_CODE_REPLY = (
    "If that account exists and has a registered email, a code has been sent to it."
)


def _lookup_user_email(identifier: str) -> tuple[Optional[Dict[str, Any]], str]:
    """(user_row, where a code for them should go) or (None, "").

    The user's own address first; failing that, the COMPANY address on this
    edge's customer record.

    2026-10-20, decided: "every user locally or in the cloud ... need a email
    for future recovery ... when users doesn't have email registered the
    recovery should go to the company email registered." Without this an
    edge user created without an address simply cannot be recovered, which
    on the day this was written was almost all of them.
    """
    try:
        u = auth_store.find_user_by_email_or_username(identifier)
    except Exception:
        u = None
    if not u:
        return None, ""
    return u, _recovery_address(u)


def _recovery_address(u: Dict[str, Any]) -> str:
    """The address a code for this user should go to, or "".

    Split out of _lookup_user_email so /forgot-password shares it. That
    endpoint read u["email"] directly, so a user with no address of their
    own silently got no code and no link - the exact case this fallback
    exists for. One rule, one place.
    """
    from app.services import recovery_codes
    own = str(u.get("email") or "").strip()
    if recovery_codes.is_deliverable(own):
        return own
    # Fall back to the company this edge belongs to. The tenant and customer
    # come from app_store.site_scope_key() - `tenant|customer|edge`, with a
    # missing customer written as the literal "-". That is THE implementation
    # of this rule (CLAUDE.md 1b); re-deriving it here is how an earlier
    # change read another scope's document.
    try:
        from app.state import app_store as _store, control_plane_store as _cp
        parts = str(_store.site_scope_key() or "").split("|")
        if len(parts) >= 3:
            tenant = parts[0].strip()
            customer = parts[1].strip()
            if customer == "-":
                customer = ""
            for row in _cp.list_customers(tenant_id=tenant) or []:
                if customer and str(row.get("customer_id") or "").strip().lower() != customer:
                    continue
                company = str(row.get("contact_email") or "").strip()
                if recovery_codes.is_deliverable(company):
                    return company
    except Exception:
        pass
    return own


@router.post("/login-code/request")
def login_code_request(payload: _CodeRequestPayload, request: Request) -> Dict[str, Any]:
    """Email a sign-in code. Nobody types a password in this flow."""
    client_host = str(getattr(request.client, "host", "") or "unknown")
    _check_rate_limit(client_host)
    identifier = str(payload.identifier or "").strip()
    if not identifier:
        return {"ok": True, "message": _GENERIC_CODE_REPLY}
    from app.services import recovery_codes
    user, email = _lookup_user_email(identifier)
    sent, reason = (False, "unknown_user")
    if user and recovery_codes.is_deliverable(email):
        sent, reason = recovery_codes.send_code(
            email, otp_type=recovery_codes.OTP_MAGIC_LINK)
    try:
        auth_store.record_login(
            str((user or {}).get("username") or identifier), ok=bool(sent),
            remote_ip=client_host, detail=f"login_code_request:{reason}")
    except Exception:
        pass
    return {"ok": True, "message": _GENERIC_CODE_REPLY}


@router.post("/login-code/verify")
def login_code_verify(payload: _CodeVerifyPayload, request: Request,
                      response: Response) -> Dict[str, Any]:
    """Verify an emailed code and sign the user in.

    The code IS the credential here - that is the operator's decision
    ("email should be enough"), and it is what Slack, Notion and Linear do.
    It also means an attacker with mailbox access is an attacker with account
    access, so this is deliberately rate limited and fully audited.
    """
    client_host = str(getattr(request.client, "host", "") or "unknown")
    _check_rate_limit(client_host)
    identifier = str(payload.identifier or "").strip()
    code = str(payload.code or "").strip()
    if not identifier or not code:
        raise HTTPException(status_code=400, detail="Identifier and code are required")
    from app.services import recovery_codes
    user, email = _lookup_user_email(identifier)
    ok = False
    if user and recovery_codes.is_deliverable(email):
        ok, _tok, _why = recovery_codes.verify_code(
            email, code, otp_type=recovery_codes.OTP_MAGIC_LINK)
    if not ok or not user:
        try:
            auth_store.record_login(identifier, ok=False, remote_ip=client_host,
                                    detail="login_code_verify_failed")
        except Exception:
            pass
        # Same answer as a wrong password, for the same reason.
        raise HTTPException(status_code=401, detail="That code is not valid")
    user_public = _public_user(user)
    _ttl = 4 * 3600
    token = create_access_token(user_public, expires_seconds=_ttl)
    try:
        auth_store.clear_failed_attempts(user_public["username"])
        auth_store.record_login(user_public["username"], ok=True,
                                remote_ip=client_host, detail="login_code")
    except Exception:
        pass
    return {"ok": True, "token": token, "user": user_public,
            "expires_in": _ttl}


@router.post("/reset-password/code")
def reset_password_with_code(payload: _CodeResetPayload,
                             request: Request) -> Dict[str, Any]:
    """Set a new password using the emailed recovery code."""
    client_host = str(getattr(request.client, "host", "") or "unknown")
    _check_rate_limit(client_host)
    identifier = str(payload.identifier or "").strip()
    code = str(payload.code or "").strip()
    new_password = str(payload.new_password or "")
    if not identifier or not code:
        raise HTTPException(status_code=400, detail="Identifier and code are required")
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    from app.services import recovery_codes
    user, email = _lookup_user_email(identifier)
    ok = False
    if user and recovery_codes.is_deliverable(email):
        ok, _tok, _why = recovery_codes.verify_code(
            email, code, otp_type=recovery_codes.OTP_RECOVERY)
    if not ok or not user:
        try:
            auth_store.record_login(identifier, ok=False, remote_ip=client_host,
                                    detail="reset_code_failed")
        except Exception:
            pass
        raise HTTPException(status_code=400, detail="That code is not valid")
    username = str(user.get("username") or "")
    # Same policy and the same hashing scheme as /reset-password - a second
    # way in must not be a weaker way in.
    try:
        _role = str((auth_store.get_user(username) or {}).get("role") or "")
    except Exception:
        _role = ""
    _policy_err = _password_policy_error(new_password, _role)
    if _policy_err:
        raise HTTPException(status_code=400, detail=_policy_err)
    try:
        new_hash = app_store._hash_password_if_needed(new_password)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Password hashing failed: {exc}") from exc
    if not auth_store.set_user_password(username, new_hash):
        raise HTTPException(status_code=400, detail="Could not set the password")
    try:
        auth_store.clear_failed_attempts(username)
        auth_store.record_login(username, ok=True, remote_ip=client_host,
                                detail="reset_password_code")
    except Exception:
        pass
    return {"ok": True, "message": "Password updated. You can sign in now."}


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


# ---------------------------------------------------------------------------
# Local account recovery (2026-08-25)
#
# An operator activated a brand-new edge, the flow reported success, and the
# admin it promised did not exist. There was then NO way back in: forgot-password
# needs SMTP and an account that already carries an e-mail, and nothing could
# create the first admin. The machine was simply unusable.
#
# Recovery is gated on PROOF OF LOCAL PRESENCE, never on a password: the caller
# must read a one-time code we write into the data directory. Anyone who can
# read that file already owns the machine, so this grants an attacker nothing
# new - while a remote attacker on the LAN cannot use it at all. That is why
# these routes are safe to expose unauthenticated, and why a plain "create the
# first admin when none exists" route would NOT be.
# ---------------------------------------------------------------------------
_RECOVERY_FILE = "trustnode-recovery-code.txt"
_RECOVERY_TTL_S = 900  # 15 minutes

# Recovery gets its OWN budget. Sharing the login limiter meant an operator who
# had just failed a few sign-ins - the exact person who needs recovery - was
# rate-limited out of the only way back in. Brute force is not the threat here
# (the code is 48 bits and single-use); a wedged operator is.
_recovery_attempts: dict = defaultdict(list)
_RECOVERY_MAX = 15
_RECOVERY_WINDOW = 300  # seconds


def _check_recovery_rate_limit(ip: str) -> None:
    now = time.time()
    _recovery_attempts[ip] = [t for t in _recovery_attempts[ip]
                              if now - t < _RECOVERY_WINDOW]
    if len(_recovery_attempts[ip]) >= _RECOVERY_MAX:
        raise HTTPException(
            status_code=429,
            detail="Too many recovery attempts. Wait 5 minutes and try again.")
    _recovery_attempts[ip].append(now)


def _recovery_dir() -> str:
    import os
    data_dir = str(os.environ.get("TRUSTNODE_DATA_DIR", "") or "").strip()
    if not data_dir:
        store = str(os.environ.get("TRUSTNODE_APP_STORE_PATH", "") or "").strip()
        data_dir = os.path.dirname(store) if store else ""
    if not data_dir:
        data_dir = os.path.join(os.path.expanduser("~"), ".trustnode_edge", "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.abspath(data_dir)


def _recovery_path() -> str:
    import os
    return os.path.join(_recovery_dir(), _RECOVERY_FILE)


def _all_local_users() -> list:
    """Every account login() could accept, from every store it consults.

    There is more than one: AuthStore (step 2 of login) and the control-plane
    SQLite mirror (step 3), and activation writes the admin into the LATTER.
    Counting only AuthStore made this endpoint report "no admin" on an edge
    that had a perfectly good one - the opposite of reassuring when you are
    already locked out.
    """
    seen = {}
    for loader in (
        lambda: auth_store.list_users() or [],
        # all_tenants: the edge binds to tenant-<customer>, so a default-scoped
        # listing silently misses the very admin activation created. Only
        # counts and an existence check leave this module - never the rows.
        lambda: control_plane_store.list_users(all_tenants=True) or [],
    ):
        try:
            rows = loader()
        except Exception:
            rows = []
        for u in rows:
            if not isinstance(u, dict):
                continue
            name = str(u.get("username") or "").strip()
            if name and name not in seen:
                seen[name] = u
    return list(seen.values())


def _admin_accounts() -> list:
    out = []
    for u in _all_local_users():
        if str(u.get("role") or "").lower() not in ("admin", "super"):
            continue
        if str(u.get("status") or "active").lower() == "disabled":
            continue
        out.append(u)
    return out


@router.get("/recovery-status")
def recovery_status(request: Request) -> Dict[str, Any]:
    """Can anyone actually get into this edge? Public and deliberately dull:
    counts and a file path, never a code, a name, or a hash."""
    admins = _admin_accounts()
    total = len(_all_local_users())
    linked = False
    try:
        settings = ((app_store.get_bootstrap(prefer_cloud_reads=False) or {})
                    .get("app_settings") or {})
        linked = bool(settings.get("edge_linked"))
    except Exception:
        linked = False
    return {
        "ok": True,
        "has_admin": bool(admins),
        "admin_count": len(admins),
        "user_count": int(total),
        "edge_linked": linked,
        "recovery_file": _recovery_path(),
        # The break-glass admin works even with an empty store, but ONLY from
        # the edge desktop itself while it still has its default password.
        # An operator who is locked out has no way to know that exists.
        "master_account_hint": _master_admin_credentials()[0],
        "master_default_password": not bool(
            str(__import__("os").environ.get("TRUSTNODE_MASTER_ADMIN_PASSWORD", "") or "").strip()),
    }


@router.post("/local-recovery/request")
def local_recovery_request(request: Request) -> Dict[str, Any]:
    """Write a fresh single-use code to disk. Returns WHERE it was written,
    never what it says - reading it is the proof of physical access."""
    import os
    import secrets
    client_host = str(getattr(request.client, "host", "") or "unknown")
    if _client_is_remote(client_host):
        raise HTTPException(
            status_code=403,
            detail="Account recovery can only be run from the edge computer itself.")
    _check_recovery_rate_limit(client_host)
    code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
    path = _recovery_path()
    expires = time.time() + _RECOVERY_TTL_S
    minutes = _RECOVERY_TTL_S // 60
    body = (
        "TrustNode Edge - local account recovery\n"
        "=======================================\n\n"
        "Recovery code: " + code + "\n\n"
        "Valid for " + str(minutes) + " minutes from the moment it was issued.\n"
        "Type this code into the Recover local access screen in the app to\n"
        "create or reset the administrator account on THIS computer.\n\n"
        "If you did not request this, delete this file. Anyone who can read it\n"
        "can take over the local administrator account.\n"
    )
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        try:
            os.chmod(path, 0o600)  # best effort; a no-op on some Windows volumes
        except Exception:
            pass
    except Exception as exc:
        raise HTTPException(status_code=500,
                            detail="Could not write the recovery file: " + str(exc))
    # held in memory too, so a stale file alone can never authorise anything
    request.app.state.tn_recovery = {"code": code, "expires": expires}
    try:
        auth_store.record_login("(recovery)", ok=False, remote_ip=client_host,
                                detail="local_recovery_requested")
    except Exception:
        pass
    return {"ok": True, "recovery_file": path, "expires_in_s": _RECOVERY_TTL_S}


class _LocalRecoveryPayload(BaseModel):
    code: str = ""
    username: str = ""
    password: str = ""


@router.post("/local-recovery/complete")
def local_recovery_complete(payload: _LocalRecoveryPayload,
                            request: Request) -> Dict[str, Any]:
    """Consume the code and create (or reset) a local administrator."""
    import hmac
    import os
    from app.auth import hash_password
    client_host = str(getattr(request.client, "host", "") or "unknown")
    if _client_is_remote(client_host):
        raise HTTPException(
            status_code=403,
            detail="Account recovery can only be run from the edge computer itself.")
    _check_recovery_rate_limit(client_host)

    issued = getattr(request.app.state, "tn_recovery", None) or {}
    want = str(issued.get("code") or "")
    if not want:
        raise HTTPException(status_code=400,
                            detail="No recovery is in progress. Choose Start recovery first.")
    if time.time() > float(issued.get("expires") or 0):
        request.app.state.tn_recovery = None
        raise HTTPException(status_code=400,
                            detail="That recovery code has expired. Start recovery again.")

    got = str(payload.code or "").strip().upper().replace(" ", "")
    if not hmac.compare_digest(got, want):
        try:
            auth_store.record_login("(recovery)", ok=False, remote_ip=client_host,
                                    detail="local_recovery_bad_code")
        except Exception:
            pass
        raise HTTPException(status_code=403, detail="That recovery code is not correct.")

    username = str(payload.username or "").strip()
    password = str(payload.password or "")
    if not username:
        raise HTTPException(status_code=400, detail="Choose an administrator name.")
    policy = _password_policy_error(password, "admin")
    if policy:
        raise HTTPException(status_code=400, detail=policy)

    # Keep the new admin in whatever tenant this edge is already bound to,
    # otherwise the account exists but every scoped read comes back empty.
    tenant_id = "default"
    try:
        settings = ((app_store.get_bootstrap(prefer_cloud_reads=False) or {})
                    .get("app_settings") or {})
        tenant_id = normalize_tenant_id(
            str(settings.get("tenant_login_realm")
                or settings.get("tenant_id") or "default"))
    except Exception:
        tenant_id = "default"

    try:
        existing = auth_store.get_user(username)
    except Exception:
        existing = None
    if not existing:
        # it may only exist in the control-plane mirror, which is where
        # activation puts it - that is still a reset, not a creation
        existing = next((u for u in _all_local_users()
                         if str(u.get("username") or "") == username), None)
    perms = (existing or {}).get("permissions")
    mods = (existing or {}).get("modules")
    auth_store.upsert_user(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permissions=perms if isinstance(perms, dict) else None,
        modules=mods if isinstance(mods, list) else None,
        tenant_id=tenant_id,
        status="active",
        must_change_password=False,
    )
    try:
        auth_store.clear_failed_attempts(username)
    except Exception:
        pass

    # A reset that leaves the OLD password working is not a reset. login()
    # accepts an account from AuthStore *or* from the control-plane mirror, and
    # activation writes the admin into the mirror - so updating only AuthStore
    # left the previous credentials valid on step 3. Push the same new password
    # into every tenant row that carries this name.
    mirrored = 0
    try:
        tenants = list(control_plane_store.list_user_tenants(username=username) or [])
    except Exception:
        tenants = []
    for tid in set(tenants + [tenant_id]):
        try:
            if control_plane_store.set_user_password(
                    tenant_id=tid, username=username, password=password,
                    must_change=False):
                mirrored += 1
        except Exception:
            pass

    # single use: burn the code and remove the file
    request.app.state.tn_recovery = None
    try:
        os.remove(_recovery_path())
    except Exception:
        pass
    try:
        auth_store.record_login(username, ok=True, remote_ip=client_host,
                                detail="local_recovery_completed")
    except Exception:
        pass
    try:
        _access.audit("account.local_recovery", outcome="ok", request=request,
                      payload={"username": username},
                      details={"recovered_user": username, "tenant_id": tenant_id,
                               "was_reset": bool(existing)})
    except Exception:
        pass
    return {"ok": True, "username": username, "tenant_id": tenant_id,
            "reset": bool(existing), "mirrored_rows": mirrored}
