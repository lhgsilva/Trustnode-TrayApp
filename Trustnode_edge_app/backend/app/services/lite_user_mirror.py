"""Mirror edge users to Supabase Auth + lite_profiles.

When an operator creates or updates a user in the desktop "Users & Access"
view, this module pushes the same user into Supabase so the same
credentials log into the cloud Lite app. The Lite app reads tenant_id
from the lite_profiles row to filter every dashboard / alarm / report
query via RLS.

Everything here is best-effort: a Supabase outage must NEVER block the
local user save. Failures are logged and swallowed.

Required env vars:
  TRUSTNODE_SUPABASE_URL          — https://<project>.supabase.co
  TRUSTNODE_SUPABASE_SERVICE_KEY  — service-role JWT (never bundled
                                    into the Lite browser code)

Optional:
  TRUSTNODE_SUPABASE_USER_DOMAIN  — fallback email domain for users that
                                    only carry a username (no email),
                                    default 'trustnode.local'
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

import requests

log = logging.getLogger(__name__)

_AUTH_TIMEOUT_S = 12.0
_DEFAULT_DOMAIN = "trustnode.local"


def _env(name: str) -> str:
    return str(os.environ.get(name, "") or "").strip()


def _supabase_cfg() -> tuple[str, str] | None:
    url = _env("TRUSTNODE_SUPABASE_URL").rstrip("/")
    key = _env("TRUSTNODE_SUPABASE_SERVICE_KEY")
    if not (url and key):
        return None
    return url, key


def _email_for(username: str, explicit_email: str | None) -> str:
    """Pick a stable email. Supabase requires one (it's the login handle).
    Prefer the explicit email if the operator typed one; otherwise build
    `<username>@<TRUSTNODE_SUPABASE_USER_DOMAIN>` so we have something
    consistent across boots."""
    candidate = (explicit_email or "").strip()
    if candidate and "@" in candidate:
        return candidate
    domain = _env("TRUSTNODE_SUPABASE_USER_DOMAIN") or _DEFAULT_DOMAIN
    safe = "".join(c for c in (username or "user").lower() if c.isalnum() or c in "._-")
    return f"{safe or 'user'}@{domain}"


def _auth_headers(key: str) -> dict[str, str]:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _find_user_by_email(url: str, key: str, email: str) -> dict[str, Any] | None:
    """GET /auth/v1/admin/users?email=... returns either the user or
    {users: [], aud: ...}. Supabase doesn't have an idempotent upsert for
    auth users — we have to check existence then create-or-update."""
    try:
        r = requests.get(
            f"{url}/auth/v1/admin/users",
            headers=_auth_headers(key),
            params={"email": email},
            timeout=_AUTH_TIMEOUT_S,
        )
        if r.status_code != 200:
            return None
        body = r.json()
        users = body.get("users") if isinstance(body, dict) else None
        if isinstance(users, list):
            for u in users:
                if str(u.get("email") or "").lower() == email.lower():
                    return u
        return None
    except Exception as exc:
        log.debug("lite-user-mirror: user lookup failed: %s", exc)
        return None


def _create_user(url: str, key: str, email: str, password: str, *,
                 username: str, role: str, tenant_id: str) -> dict[str, Any] | None:
    body = {
        "email": email,
        "password": password,
        "email_confirm": True,  # don't make operators click a magic link
        "user_metadata": {
            "username": username,
            "role": role,
            "tenant_id": tenant_id,
            "source": "trustnode_edge",
        },
    }
    try:
        r = requests.post(
            f"{url}/auth/v1/admin/users",
            headers=_auth_headers(key),
            json=body,
            timeout=_AUTH_TIMEOUT_S,
        )
        if r.status_code in (200, 201):
            return r.json()
        log.debug("lite-user-mirror: create_user HTTP %s: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.debug("lite-user-mirror: create_user error: %s", exc)
    return None


def _update_user(url: str, key: str, user_id: str, *, password: str | None,
                 username: str, role: str, tenant_id: str) -> bool:
    body: dict[str, Any] = {
        "email_confirm": True,
        "user_metadata": {
            "username": username,
            "role": role,
            "tenant_id": tenant_id,
            "source": "trustnode_edge",
        },
    }
    if password:
        body["password"] = password
    try:
        r = requests.put(
            f"{url}/auth/v1/admin/users/{user_id}",
            headers=_auth_headers(key),
            json=body,
            timeout=_AUTH_TIMEOUT_S,
        )
        if r.status_code in (200, 201):
            return True
        log.debug("lite-user-mirror: update_user HTTP %s: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.debug("lite-user-mirror: update_user error: %s", exc)
    return False


def _delete_user(url: str, key: str, user_id: str) -> bool:
    try:
        r = requests.delete(
            f"{url}/auth/v1/admin/users/{user_id}",
            headers=_auth_headers(key),
            timeout=_AUTH_TIMEOUT_S,
        )
        return r.status_code in (200, 204)
    except Exception as exc:
        log.debug("lite-user-mirror: delete_user error: %s", exc)
        return False


def _upsert_lite_profile(*, user_id: str, tenant_id: str, username: str,
                         email: str, role: str) -> bool:
    """Upsert the `public.lite_profiles` row that the Lite RLS function
    consults to resolve `lite_current_tenant()`. Without this row the
    user can authenticate but sees nothing.

    Uses the SQLAlchemy engine the rest of the edge uses (shared pool)
    rather than opening a new connection.
    """
    try:
        from app.state import app_store
    except Exception:
        return False
    try:
        cloud = app_store._get_cloud_database_target()  # type: ignore[attr-defined]
    except Exception:
        cloud = None
    if not cloud:
        return False
    try:
        from sqlalchemy import text  # type: ignore
        schema = str(cloud.get("schema") or "public")
        engine, _ = app_store._get_or_create_cloud_engine(cloud, schema)  # type: ignore[attr-defined]
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"""
                    INSERT INTO "{schema}".lite_profiles (
                      user_id, tenant_id, username, email, role,
                      created_utc, updated_utc
                    ) VALUES (
                      :user_id, :tenant_id, :username, :email, :role,
                      now(), now()
                    )
                    ON CONFLICT (user_id) DO UPDATE SET
                      tenant_id   = EXCLUDED.tenant_id,
                      username    = EXCLUDED.username,
                      email       = EXCLUDED.email,
                      role        = EXCLUDED.role,
                      updated_utc = now()
                    """
                ),
                {
                    "user_id": user_id, "tenant_id": tenant_id,
                    "username": username, "email": email, "role": role,
                },
            )
        return True
    except Exception as exc:
        log.debug("lite-user-mirror: lite_profiles upsert failed: %s", exc)
        return False


def mirror_user_upsert(*, tenant_id: str, username: str, password: str | None,
                       role: str, email: str | None = None) -> None:
    """Best-effort, fire-and-forget mirror to Supabase Auth + lite_profiles.

    Runs in a background thread so the local HTTP request returns fast.
    Called from the edge user-CRUD endpoint right after the local SQLite
    save succeeds. Safe to invoke even when env vars are missing — it
    just no-ops.
    """
    cfg = _supabase_cfg()
    if not cfg:
        return  # nothing to do — no service key configured

    # Snapshot args (immutable strings) so the closure is hermetic.
    args = {
        "tenant_id": str(tenant_id or "default").strip() or "default",
        "username": str(username or "").strip(),
        "password": password,
        "role": str(role or "viewer").strip() or "viewer",
        "email": str(email or "").strip() or None,
    }
    if not args["username"]:
        return

    def _run() -> None:
        url, key = cfg
        email_addr = _email_for(args["username"], args["email"])
        existing = _find_user_by_email(url, key, email_addr)
        if existing is None:
            # Create — password is required by Supabase Auth on create.
            if not args["password"]:
                log.debug("lite-user-mirror: skip create for %s (no password)", args["username"])
                return
            created = _create_user(
                url, key, email_addr, args["password"],
                username=args["username"], role=args["role"], tenant_id=args["tenant_id"],
            )
            if not created:
                return
            user_id = str(created.get("id") or "")
        else:
            user_id = str(existing.get("id") or "")
            if not user_id:
                return
            _update_user(
                url, key, user_id,
                password=args["password"],
                username=args["username"], role=args["role"], tenant_id=args["tenant_id"],
            )
        # Always (re)write the lite_profiles row so a tenant change or
        # role rename takes effect immediately on the Lite side.
        _upsert_lite_profile(
            user_id=user_id, tenant_id=args["tenant_id"],
            username=args["username"], email=email_addr, role=args["role"],
        )

    threading.Thread(target=_run, name=f"lite-user-mirror-{args['username']}",
                     daemon=True).start()


def mirror_user_delete(*, tenant_id: str, username: str, email: str | None = None) -> None:
    """Remove the mirrored Supabase Auth account + lite_profiles row.
    Best-effort and threaded, same as upsert."""
    cfg = _supabase_cfg()
    if not cfg or not str(username or "").strip():
        return
    email_addr = _email_for(username, email)

    def _run() -> None:
        url, key = cfg
        existing = _find_user_by_email(url, key, email_addr)
        if not existing:
            return
        user_id = str(existing.get("id") or "")
        if user_id:
            _delete_user(url, key, user_id)

    threading.Thread(target=_run, name=f"lite-user-mirror-del-{username}",
                     daemon=True).start()
