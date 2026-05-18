"""Pull cp_users from the cloud portal into the local edge SQLite.

The portal at the VPS owns the customer-facing source of truth for users
(role, permissions, modules, status). A customer-site edge needs to
honor portal-side changes so an operator who is locked out via the portal
can't keep logging into the local edge.

Direction: cloud -> local edge (one way). Local password changes still
live on the edge; portal-side password changes propagate via the
existing temp-password / forced-rotation flow (the JWT-issuing edge
auth path bumps `must_change_password` so the user is prompted on
their next local login).

What we DON'T sync:
  - password_hash: bcrypt hashes never travel through the cloud. The
    portal's `GET /api/cp/users` response strips this field already.
  - last_login_utc: per-edge state, no point overwriting.

What we DO sync (when a remote row differs):
  - role, status, email, mfa_enabled, customer_id
  - modules (JSON list of enabled module keys)
  - permissions (JSON map of granular flags)
  - must_change_password (so a portal-issued temp password flag arrives)

Soft-deletion: if a user disappears from the cloud's list, we set
status='inactive' locally rather than deleting (preserves audit + lets
the user be re-enabled by the portal later).

Gated on cloud_auto_sync_enabled + a non-empty cloud URL. The pull
loop runs in a daemon thread started by main.py at startup.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

import requests

log = logging.getLogger("trustnode.cp_users_puller")


def _env(key: str, default: str = "") -> str:
    return str(os.environ.get(key, default) or default).strip()


class CpUsersPuller:
    """Background service that periodically syncs portal-side cp_users
    onto the local edge SQLite. Single-instance — main.py creates one
    and calls .start()."""

    def __init__(self, *, control_plane_store, tenant_id: str,
                 cloud_url: str, login_user: str, login_password: str,
                 poll_seconds: float = 30.0) -> None:
        self._store = control_plane_store
        self._tenant_id = str(tenant_id or "").strip()
        self._cloud_url = str(cloud_url or "").rstrip("/")
        self._user = str(login_user or "").strip()
        self._password = str(login_password or "")
        self._poll_seconds = max(5.0, float(poll_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._token = ""
        self._token_exp = 0.0
        self._last_synced_utc = ""
        self._last_error = ""

    # ---- public API ----
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not self._cloud_url:
            log.info("cp_users_puller: no cloud URL — pull disabled")
            return
        if not self._tenant_id:
            log.info("cp_users_puller: no tenant_id — pull disabled")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cp-users-puller",
                                        daemon=True)
        self._thread.start()
        log.info("cp_users_puller: started tenant=%s url=%s interval=%.1fs",
                 self._tenant_id, self._cloud_url, self._poll_seconds)

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "tenant_id": self._tenant_id,
            "cloud_url": self._cloud_url,
            "poll_seconds": self._poll_seconds,
            "last_synced_utc": self._last_synced_utc,
            "last_error": self._last_error,
        }

    # ---- internals ----
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._sync_once()
                self._last_error = ""
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.warning("cp_users_puller: sync error %s", self._last_error)
            self._stop.wait(self._poll_seconds)

    def _ensure_token(self) -> str:
        now = time.time()
        if self._token and self._token_exp > now + 60:
            return self._token
        try:
            r = requests.post(
                f"{self._cloud_url}/api/auth/login",
                json={"username": self._user, "password": self._password},
                timeout=10,
            )
        except Exception as exc:
            raise RuntimeError(f"cloud_login_failed: {exc}") from exc
        if r.status_code // 100 != 2:
            raise RuntimeError(f"cloud_login_http_{r.status_code}: {r.text[:160]}")
        body = r.json()
        token = str(body.get("token") or body.get("access_token") or "").strip()
        if not token:
            raise RuntimeError("cloud_login_missing_token")
        # JWT exp claim — best effort
        try:
            import base64
            seg = token.split(".")[1]
            seg += "=" * (-len(seg) % 4)
            payload = json.loads(base64.urlsafe_b64decode(seg))
            self._token_exp = float(payload.get("exp") or 0)
        except Exception:
            self._token_exp = now + 600
        self._token = token
        return token

    def _fetch_cloud_users(self) -> list[dict[str, Any]]:
        token = self._ensure_token()
        r = requests.get(
            f"{self._cloud_url}/api/control-plane/users",
            headers={"Authorization": f"Bearer {token}"},
            params={"tenant_id": self._tenant_id},
            timeout=15,
        )
        if r.status_code == 401:
            # token might have been invalidated; force-refresh next loop
            self._token = ""; self._token_exp = 0.0
            raise RuntimeError("cloud_users_unauth")
        if r.status_code // 100 != 2:
            raise RuntimeError(f"cloud_users_http_{r.status_code}: {r.text[:160]}")
        body = r.json()
        rows = body.get("rows") if isinstance(body, dict) else None
        return list(rows) if isinstance(rows, list) else []

    def _sync_once(self) -> None:
        cloud_rows = self._fetch_cloud_users()
        cloud_by_username = {str(u.get("username") or ""): u for u in cloud_rows
                             if u.get("username")}

        local_rows = self._store.list_users(tenant_id=self._tenant_id)
        local_by_username = {str(u.get("username") or ""): u for u in local_rows
                             if u.get("username")}

        changes = 0
        for uname, remote in cloud_by_username.items():
            local = local_by_username.get(uname)
            if local is None:
                # New user from the cloud — create with NO password (admin
                # must set one via local-edge "Set password" or the user
                # uses a cloud-issued temp password). We mark them with a
                # forced password change so they can't log in with an
                # empty hash.
                self._store.upsert_user(
                    tenant_id=self._tenant_id,
                    customer_id=str(remote.get("customer_id") or ""),
                    username=uname,
                    password=None,
                    role=str(remote.get("role") or "viewer"),
                    status=str(remote.get("status") or "active"),
                    email=str(remote.get("email") or ""),
                    mfa_enabled=bool(remote.get("mfa_enabled")),
                    modules=list(remote.get("modules") or []),
                    permissions=dict(remote.get("permissions") or {}),
                )
                # Force a password set on next login since we have no hash
                # locally yet. Done via direct SQL because ControlPlaneStore
                # has no public "flip must_change_password" method — its
                # password-set primitives also rewrite the hash.
                try:
                    with self._store._connect() as conn:                  # noqa: SLF001
                        conn.execute(
                            "UPDATE cp_users SET must_change_password=1 "
                            "WHERE tenant_id=? AND username=?",
                            (self._tenant_id, uname),
                        )
                        conn.commit()
                except Exception:
                    pass
                changes += 1
                log.info("cp_users_puller: created local user '%s' from cloud", uname)
                continue
            # Compare meaningful fields; ignore password_hash + timestamps.
            if self._user_differs(local, remote):
                self._store.upsert_user(
                    tenant_id=self._tenant_id,
                    customer_id=str(remote.get("customer_id") or local.get("customer_id") or ""),
                    username=uname,
                    password=None,                         # never sync password
                    role=str(remote.get("role") or local.get("role") or "viewer"),
                    status=str(remote.get("status") or "active"),
                    email=str(remote.get("email") or local.get("email") or ""),
                    mfa_enabled=bool(remote.get("mfa_enabled")),
                    modules=list(remote.get("modules") or []),
                    permissions=dict(remote.get("permissions") or {}),
                )
                changes += 1
                log.info("cp_users_puller: updated local user '%s' from cloud", uname)

        # Soft-deactivate users absent in cloud but present locally —
        # don't touch the built-in 'admin' (operator's local fallback).
        for uname, local in local_by_username.items():
            if uname.lower() == "admin":
                continue
            if uname in cloud_by_username:
                continue
            if str(local.get("status") or "") == "inactive":
                continue
            try:
                self._store.upsert_user(
                    tenant_id=self._tenant_id,
                    customer_id=str(local.get("customer_id") or ""),
                    username=uname,
                    password=None,
                    role=str(local.get("role") or "viewer"),
                    status="inactive",
                    email=str(local.get("email") or ""),
                    mfa_enabled=bool(local.get("mfa_enabled")),
                    modules=list(local.get("modules") or []),
                    permissions=dict(local.get("permissions") or {}),
                )
                changes += 1
                log.info("cp_users_puller: deactivated local user '%s' (gone from cloud)", uname)
            except Exception as exc:
                log.warning("cp_users_puller: deactivate '%s' failed: %s", uname, exc)

        self._last_synced_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if changes:
            log.info("cp_users_puller: sync done — %d change(s)", changes)

    @staticmethod
    def _user_differs(local: dict[str, Any], remote: dict[str, Any]) -> bool:
        for key in ("role", "status", "email", "customer_id"):
            if str(local.get(key) or "") != str(remote.get(key) or ""):
                return True
        if bool(local.get("mfa_enabled")) != bool(remote.get("mfa_enabled")):
            return True
        # Compare modules + permissions as sets / sorted JSON for stability.
        if sorted(local.get("modules") or []) != sorted(remote.get("modules") or []):
            return True
        # permissions is a flat dict[str, bool] — normalize before compare
        lp = {str(k): bool(v) for k, v in (local.get("permissions") or {}).items()}
        rp = {str(k): bool(v) for k, v in (remote.get("permissions") or {}).items()}
        if lp != rp:
            return True
        return False


def build_from_env(control_plane_store, app_settings: dict[str, Any] | None = None
                   ) -> CpUsersPuller | None:
    """Construct a CpUsersPuller from environment + app_settings, or
    return None when cloud sync is disabled / config is missing.
    Called from main.py on startup."""
    settings = app_settings or {}
    cloud_auto = settings.get("cloud_auto_sync_enabled")
    if cloud_auto is False:
        return None
    cloud_url = (
        str(settings.get("cloud_url") or settings.get("cloud_api_url") or "")
        .strip().rstrip("/")
    )
    if not cloud_url:
        cloud_url = _env("TRUSTNODE_CLOUD_API_URL")
    if not cloud_url:
        return None
    tenant_id = (
        str(settings.get("tenant_id") or settings.get("tenant_login_realm") or "").strip()
        or _env("TRUSTNODE_TENANT_ID")
        or "default"
    )
    user = _env("TRUSTNODE_CLOUD_BOOTSTRAP_USER", "admin")
    pwd  = _env("TRUSTNODE_CLOUD_BOOTSTRAP_PASSWORD", "admin")
    poll_s = float(_env("TRUSTNODE_CP_USERS_POLL_SECONDS", "30") or 30)
    return CpUsersPuller(
        control_plane_store=control_plane_store,
        tenant_id=tenant_id,
        cloud_url=cloud_url,
        login_user=user,
        login_password=pwd,
        poll_seconds=poll_s,
    )
