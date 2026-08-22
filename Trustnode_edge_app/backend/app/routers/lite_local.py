"""Local Lite API (operator 2026-06-17, M7).

Endpoints under /api/lite-local/* serve the LAN-shared, view-only
Lite UI. Everything here is PUBLIC by token — the auth middleware
allow-lists this prefix. Each call carries either:

  * ``Authorization: Bearer <session_jwt>`` — issued by
    POST /api/lite-local/validate after the operator-distributed
    view-link token has been resolved, OR
  * ``?token=<view_link_token>`` query string — single-call form, used
    by the initial bootstrap on page load.

Endpoints:

  * POST /validate              — exchange a view-link token for a
                                  session JWT (12 h TTL).
  * GET  /bootstrap?token=...   — dashboards + configs + edge identity.
  * GET  /live?token=...        — latest live_latest rows for the
                                  edge's tenant.
  * GET  /historian?token=...   — historian range query.

In ``customer_sql`` mode every read prefers the customer DB. In
``local_sqlite`` mode they all fall back to the edge's existing
SQLite. This means the LAN Lite works regardless of database mode.
"""
from __future__ import annotations

import hmac
import hashlib
import json
import logging
import secrets
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Depends
from pydantic import BaseModel

from app.state import app_store
from app.services import customer_sql, sinks_sql
# control_plane_store here is the INSTANCE (built in app.state via
# _build_control_plane_store), not the module. Earlier import pulled
# the module which has no methods — leading to "no attribute
# get_edge_view_link_by_token" at /validate time.
from app.state import control_plane_store

logger = logging.getLogger(__name__)
from app.services import access_policy as _access

# Operator 2026-08-21 (plan §3.3): the whole Local View API is licence-gated on
# `local_web_app` (404 when absent for remote clients; log-only on loopback).
router = APIRouter(prefix="/api/lite-local", tags=["lite-local"],
                   dependencies=[Depends(_access.require_module("local_web_app"))])


# ----------------------------------------------------------------------
# Session JWT (lightweight, edge-signed HS256-like).
# ----------------------------------------------------------------------
# We avoid pulling in `pyjwt` (already-shipping psycopg should not be
# our excuse for a new dep) — a 60-LOC HMAC-SHA256 compact JSON token
# is plenty for a LAN scope. Format:
#     base64(json_header).base64(json_body).base64(hmac(header.body))

_SESSION_TTL_S = 12 * 60 * 60


def _b64(b: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    import base64
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _session_secret() -> bytes:
    """Per-edge session secret. Persisted in app_settings so a restart
    doesn't invalidate every live Lite session. Generated lazily.
    """
    try:
        bootstrap = app_store.get_bootstrap() or {}
    except Exception:
        bootstrap = {}
    s = bootstrap.get("app_settings") if isinstance(bootstrap.get("app_settings"), dict) else {}
    raw = str(s.get("lite_session_secret") or "")
    if raw:
        try:
            return _b64d(raw)
        except Exception:
            pass
    secret = secrets.token_bytes(32)
    try:
        s_new = dict(s)
        s_new["lite_session_secret"] = _b64(secret)
        app_store.upsert_domain("app_settings", s_new, actor="lite_local_router")
    except Exception:
        pass
    return secret


def _issue_session_jwt(token_row: Dict[str, Any]) -> str:
    secret = _session_secret()
    header = {"alg": "HS256", "typ": "JWT"}
    body = {
        "iat": int(time.time()),
        "exp": int(time.time()) + _SESSION_TTL_S,
        "tenant_id": str(token_row.get("tenant_id") or "default"),
        "edge_id": str(token_row.get("edge_id") or ""),
        "customer_id": str(token_row.get("customer_id") or ""),
        "user_id": str(token_row.get("user_id") or ""),
        "scope_key": str(token_row.get("scope_key") or ""),
        "token_id": str(token_row.get("token") or "")[:16],
    }
    h_b = _b64(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
    p_b = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    sig = hmac.new(secret, f"{h_b}.{p_b}".encode(), hashlib.sha256).digest()
    return f"{h_b}.{p_b}.{_b64(sig)}"


def _verify_session_jwt(jwt: str) -> Optional[Dict[str, Any]]:
    try:
        h_b, p_b, s_b = jwt.split(".")
        secret = _session_secret()
        expected = hmac.new(secret, f"{h_b}.{p_b}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64d(s_b)):
            return None
        body = json.loads(_b64d(p_b))
        if int(body.get("exp") or 0) < int(time.time()):
            return None
        return body
    except Exception:
        return None


def _extract_session(request: Request, token_qs: Optional[str]) -> Dict[str, Any]:
    """Resolve a Lite session from either a Bearer JWT or a raw
    view-link token (single-call form). Returns the session body or
    raises 401.
    """
    bearer = ""
    auth = str(request.headers.get("authorization") or "")
    if auth.lower().startswith("bearer "):
        bearer = auth[7:].strip()
    if bearer:
        # 1. Try a Lite-local session JWT (issued by /validate).
        body = _verify_session_jwt(bearer)
        if body:
            return body
        # 2. Operator 2026-06-18: accept the MAIN auth JWT too (issued
        # by /api/auth/login). A logged-in LAN viewer doesn't have a
        # view-link token, but they can still hit lite-local endpoints
        # to load the dashboard. Scope comes from app_settings since
        # the JWT is unscoped (tray-style login).
        try:
            from app.auth import decode_access_token
            jwt_body = decode_access_token(bearer)
            if jwt_body:
                # Pull tenant/customer/edge from app_settings so the
                # response carries a sensible scope for the shim's
                # dashboard_configurations gate.
                try:
                    bootstrap = app_store.get_bootstrap() or {}
                except Exception:
                    bootstrap = {}
                s = bootstrap.get("app_settings") or {}
                if not isinstance(s, dict):
                    s = {}
                return {
                    "tenant_id": str(jwt_body.get("tenant_id") or s.get("tenant_id") or "default"),
                    "edge_id": str(s.get("edge_id") or ""),
                    "customer_id": str(jwt_body.get("customer_id") or s.get("customer_id") or ""),
                    "user_id": str(jwt_body.get("sub") or jwt_body.get("username") or ""),
                    # 2026-08-22: identity travels with the session so the Lite
                    # capability flags can be decided per person, not per install.
                    "username": str(jwt_body.get("sub") or jwt_body.get("username") or ""),
                    "role": str(jwt_body.get("role") or ""),
                    "permissions": jwt_body.get("permissions") if isinstance(jwt_body.get("permissions"), dict) else {},
                    "scope_key": "",
                    "token_id": "auth-jwt",
                    "auth_source": "jwt",
                }
        except Exception:
            pass
    if token_qs and _access.has_module("view_share_links"):
        row = control_plane_store.get_edge_view_link_by_token(token=token_qs)
        if row and str(row.get("status") or "").lower() == "active":
            # Synthesize a session body so downstream reads can scope.
            return {
                "tenant_id": str(row.get("tenant_id") or "default"),
                "edge_id": str(row.get("edge_id") or ""),
                "customer_id": str(row.get("customer_id") or ""),
                "user_id": str(row.get("user_id") or ""),
                "scope_key": str(row.get("scope_key") or ""),
                "token_id": str(row.get("token") or "")[:16],
            }
    raise HTTPException(status_code=401, detail="invalid lite session")


# ----------------------------------------------------------------------
# Helpers to choose the read source
# ----------------------------------------------------------------------

def _customer_target_or_none() -> Optional[Dict[str, Any]]:
    try:
        bootstrap = app_store.get_bootstrap() or {}
    except Exception:
        return None
    s = bootstrap.get("app_settings") if isinstance(bootstrap.get("app_settings"), dict) else {}
    if str(s.get("database_mode") or "local_sqlite").lower() != "customer_sql":
        return None
    target = s.get("customer_sql_target")
    if isinstance(target, dict) and target.get("host"):
        return target
    return None


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------

class AccessCheckRequest(BaseModel):
    variant: str  # "full" | "lite" | "client"


_VARIANT_FLAG = {
    "full": "access_full",
    "lite": "access_lite",
    "client": "access_client",
}


@router.post("/check-access")
def post_check_access(payload: AccessCheckRequest, request: Request) -> dict:
    """Operator 2026-06-18 — LAN variant access gate.

    Reads the Bearer JWT (set by /api/auth/login), looks up the user's
    permissions, and returns 200 if access_<variant> is set, 403 otherwise.

    The check intentionally ALSO succeeds if the user has role=admin or
    role=super so the operator can always rescue themselves into any view.
    """
    variant = str(payload.variant or "lite").strip().lower()
    flag = _VARIANT_FLAG.get(variant)
    if not flag:
        raise HTTPException(status_code=400, detail=f"unknown variant: {variant}")
    auth_h = request.headers.get("authorization") or ""
    token = ""
    if auth_h.lower().startswith("bearer "):
        token = auth_h[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        from app.auth import decode_access_token
        body = decode_access_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"invalid token: {exc}") from exc
    username = str(body.get("sub") or body.get("username") or "").strip()
    role = str(body.get("role") or "").strip().lower()
    # 2026-08-22: delegate to access_policy.surface_access — the same decision
    # the static surface guard makes when the bundle itself is fetched. Before
    # this, the login gate read ONLY the users_access document, so it could wave
    # a user through to a surface the guard then refused (or, with named licence
    # seats, refuse one the guard would allow). One source of truth.
    from app.services import access_policy as _access

    remote = _access.request_is_remote(request)
    allowed, reason = _access.surface_access(variant, body, remote)
    # Which surfaces this session could open — lets the login page send someone
    # straight to the one their seat entitles them to.
    entitled = [
        v for v in ("full", "client", "lite")
        if _access.surface_access(v, body, remote)[0]
    ]
    if not allowed:
        detail = f"user '{username}' has no access to {variant}"
        if str(reason or "").startswith("seat:"):
            detail = (f"user '{username}' does not hold a licence seat for this view"
                      if reason != "seat:view_ui" else
                      f"user '{username}' is licensed for the other view interface")
        elif str(reason or "").startswith("licence:"):
            detail = f"this licence does not include {variant} access"
        raise HTTPException(status_code=403, detail=detail)
    return {"ok": True, "username": username, "variant": variant,
            "reason": reason or "permission", "entitled": entitled}


class ValidateRequest(BaseModel):
    token: str


@router.post("/validate")
def post_validate(payload: ValidateRequest, request: Request) -> dict:
    # Owner decision 2026-08-21: Local View requires a login; no-login share
    # links exist only with the `view_share_links` licence option.
    _access.require_module("view_share_links")(request)
    try:
        row = control_plane_store.get_edge_view_link_by_token(token=payload.token)
    except Exception as exc:
        logger.exception("lite-local validate: store lookup raised")
        raise HTTPException(status_code=500, detail=f"token lookup failed: {type(exc).__name__}: {exc}") from exc
    if not row:
        raise HTTPException(status_code=404, detail="view link not found")
    if str(row.get("status") or "").lower() != "active":
        raise HTTPException(status_code=403, detail="view link revoked")
    try:
        jwt = _issue_session_jwt(row)
    except Exception as exc:
        logger.exception("lite-local validate: jwt issue raised")
        raise HTTPException(status_code=500, detail=f"jwt issue failed: {type(exc).__name__}: {exc}") from exc
    # Operator 2026-06-17: also mint a regular auth JWT for the user
    # the view-link belongs to (when user_id is set). The local React
    # Lite uses this so the LAN browser can call /api/* exactly like a
    # logged-in user — same pages, same data, no separate API surface.
    auth_jwt = ""
    user_public: dict = {}
    user_id = str(row.get("user_id") or "").strip()
    if user_id:
        try:
            from app.auth import create_access_token
            # Pull the matching user row from the local app store so we
            # set permissions exactly as the operator configured.
            bootstrap = app_store.get_bootstrap() or {}
            users_access = bootstrap.get("users_access") if isinstance(bootstrap.get("users_access"), dict) else {}
            for u in (users_access.get("users") or []) if isinstance(users_access, dict) else []:
                if str(u.get("username") or "") == user_id:
                    user_public = {
                        "username": user_id,
                        "role": str(u.get("role") or "viewer"),
                        "permissions": u.get("permissions") or {},
                        "modules": u.get("modules") or {},
                        "tenant_id": str(row.get("tenant_id") or "default"),
                        "customer_id": str(row.get("customer_id") or ""),
                    }
                    break
            if not user_public:
                # Fallback: a viewer-class user — enough to render Lite.
                user_public = {
                    "username": user_id,
                    "role": "viewer",
                    "permissions": {},
                    "modules": {},
                    "tenant_id": str(row.get("tenant_id") or "default"),
                    "customer_id": str(row.get("customer_id") or ""),
                }
            auth_jwt = create_access_token(user_public)
        except Exception as exc:
            logger.exception("lite-local validate: auth-jwt mint failed (continuing)")
    return {
        "ok": True,
        "jwt": jwt,
        "auth_token": auth_jwt,
        "user": user_public,
        "scope": {
            "tenant_id": row.get("tenant_id"),
            "edge_id": row.get("edge_id"),
            "customer_id": row.get("customer_id"),
            "scope_key": row.get("scope_key"),
            "user_id": user_id,
        },
        "ttl_s": _SESSION_TTL_S,
    }


def _lite_capabilities(session: Dict[str, Any]) -> Dict[str, Any]:
    """What THIS person may see in Lite.

    Lite used to build its tabs from static config flags (`show_power_tab` and
    friends), so it showed the same menu to everyone regardless of the licence or
    the user's permissions — and it had no way to offer Batch or the assistant at
    all. It now receives the same truth the React surfaces use: the licence
    modules and the user's resolved permissions, one flag per feature.
    """
    from app.services import permission_catalog as _pc
    from app.services import access_policy as _access

    username = str(session.get("username") or "")
    role = str(session.get("role") or "").strip().lower()
    perms: Dict[str, Any] = {}
    if isinstance(session.get("permissions"), dict):
        perms = dict(session["permissions"])
    if not perms and username:
        try:
            from app.state import auth_store as _as
            rec = _as.get_user(username) or {}
            if isinstance(rec.get("permissions"), dict):
                perms = dict(rec["permissions"])
        except Exception:
            perms = {}

    is_admin = role in ("admin", "super")
    # A no-login share link has no user behind it. It gets the surface's
    # baseline — dashboards and historian — and never the admin bypass.
    anonymous_link = not username and str(session.get("auth_source") or "") != "jwt"

    def _allowed(key: str, module: str = "") -> bool:
        if module and not _access.has_module(module):
            return False          # the site does not have the feature at all
        if is_admin:
            return True
        if anonymous_link:
            return key in ("dashboard", "historian")
        return _pc.resolve(perms, key)

    return {
        "role": role,
        "is_admin": is_admin,
        # one flag per Lite tab; the client renders only what is true
        "dashboard": _allowed("dashboard", "dashboard"),
        "power": _allowed("power_overview", "power_overview"),
        "historian": _allowed("historian", "historian"),
        "reporting": _allowed("reporting", "reporting"),
        "alarms": _allowed("alarms", "alarms"),
        "tags": _allowed("tags", "tags"),
        "batch": _allowed("batch_management", "batch_management"),
        "intelligence": _allowed("trustnode_intelligence", "trustnode_intelligence"),
        # Lite is a read-only surface: never offer an action it cannot perform.
        "read_only": True,
    }


@router.get("/capabilities")
def get_capabilities(request: Request, token: str = "") -> dict:
    """Feature flags for the Lite surface, derived from the licence and the
    signed-in user's permissions."""
    session = _extract_session(request, token)
    return {"ok": True, "capabilities": _lite_capabilities(session), "session": session}


@router.get("/bootstrap")
def get_bootstrap(request: Request, token: str = "") -> dict:
    session = _extract_session(request, token)
    target = _customer_target_or_none()
    if target is not None:
        engine, err = customer_sql.get_engine(target)
        if engine is not None:
            try:
                from sqlalchemy import text
                schema = str(target.get("schema") or "public") or "public"
                with engine.connect() as conn:
                    rows = conn.execute(
                        text(
                            f'SELECT domain, payload_json FROM "{schema}"."config_documents" '
                            f'WHERE tenant_id = :tenant'
                        ),
                        {"tenant": session.get("tenant_id") or "default"},
                    ).fetchall()
                bootstrap: Dict[str, Any] = {}
                for r in rows:
                    try:
                        bootstrap[str(r[0])] = json.loads(r[1]) if isinstance(r[1], (str, bytes)) else r[1]
                    except Exception:
                        bootstrap[str(r[0])] = None
                return {"ok": True, "source": "customer_sql", "data": bootstrap, "session": session,
                        "capabilities": _lite_capabilities(session)}
            except Exception as exc:
                logger.warning("lite-local bootstrap from customer DB failed: %s", exc)
    # Fallback: the local SQLite bootstrap (no customer DB or read failed).
    try:
        bootstrap = app_store.get_bootstrap() or {}
    except Exception:
        bootstrap = {}
    return {"ok": True, "source": "local_sqlite", "data": bootstrap, "session": session,
            "capabilities": _lite_capabilities(session)}


@router.get("/live")
def get_live(
    request: Request,
    token: str = "",
    limit: int = Query(2000, ge=1, le=10000),
) -> dict:
    session = _extract_session(request, token)
    target = _customer_target_or_none()
    if target is not None:
        engine, _ = customer_sql.get_engine(target)
        if engine is not None:
            try:
                from sqlalchemy import text
                schema = str(target.get("schema") or "public") or "public"
                with engine.connect() as conn:
                    rows = conn.execute(
                        text(
                            f'SELECT tenant_id, gateway_id, gateway_name, device_name, '
                            f'plc_ip, database_name, tag_name, ts_utc, value, value_text, '
                            f'quality, quality_label, source '
                            f'FROM "{schema}"."live_latest" '
                            f'WHERE tenant_id = :tenant '
                            f'ORDER BY ts_utc DESC LIMIT :lim'
                        ),
                        {"tenant": session.get("tenant_id") or "default", "lim": int(limit)},
                    ).mappings().all()
                return {"ok": True, "source": "customer_sql", "rows": [dict(r) for r in rows]}
            except Exception as exc:
                logger.warning("lite-local live from customer DB failed: %s", exc)
    # Fallback to local SQLite.
    try:
        rows = app_store.get_live_rows(limit=int(limit))
    except Exception:
        rows = []
    return {"ok": True, "source": "local_sqlite", "rows": rows}


@router.get("/historian")
def get_historian(
    request: Request,
    token: str = "",
    from_utc: str = "",
    to_utc: str = "",
    gateway: str = "",
    tag: str = "",
    limit: int = Query(5000, ge=1, le=50000),
) -> dict:
    session = _extract_session(request, token)
    target = _customer_target_or_none()
    if target is not None:
        engine, _ = customer_sql.get_engine(target)
        if engine is not None:
            try:
                from sqlalchemy import text
                schema = str(target.get("schema") or "public") or "public"
                where = "WHERE tenant_id = :tenant"
                params: Dict[str, Any] = {
                    "tenant": session.get("tenant_id") or "default",
                    "lim": int(limit),
                }
                if from_utc:
                    where += " AND ts_utc >= :from_utc"
                    params["from_utc"] = from_utc
                if to_utc:
                    where += " AND ts_utc <= :to_utc"
                    params["to_utc"] = to_utc
                if gateway:
                    where += " AND gateway_id = :gateway"
                    params["gateway"] = gateway
                if tag:
                    where += " AND LOWER(tag_name) LIKE LOWER(:tag)"
                    params["tag"] = f"%{tag}%"
                with engine.connect() as conn:
                    rows = conn.execute(
                        text(
                            f'SELECT ts_utc, gateway_id, gateway_name, device_name, '
                            f'plc_ip, database_name, tag_name, value, value_text, '
                            f'quality, quality_label, source '
                            f'FROM "{schema}"."historian_readings" '
                            f'{where} ORDER BY ts_utc DESC LIMIT :lim'
                        ),
                        params,
                    ).mappings().all()
                return {"ok": True, "source": "customer_sql", "rows": [dict(r) for r in rows]}
            except Exception as exc:
                logger.warning("lite-local historian from customer DB failed: %s", exc)
    # Fallback to local SQLite historian.
    try:
        rows = app_store.get_historian_rows_range(
            from_utc=from_utc,
            to_utc=to_utc,
            gateway=gateway,
            tag=tag,
            limit=int(limit),
        )
    except Exception:
        rows = []
    return {"ok": True, "source": "local_sqlite", "rows": rows}


# ---------------------------------------------------------------------------
# Operator 2026-08-21 (plan Phase 4.3): reports for Local View. The LAN Lite
# shim returned [] for generated_reports ("reports queue is cloud-only"); the
# edge's own reports_store has them. Same session rules as the other
# lite-local reads; file download accepts the session via header or ?token=.
# ---------------------------------------------------------------------------
@router.get("/reports")
def get_lite_reports(request: Request, limit: int = 100, token: str = Query(default="")) -> dict:
    sess = _extract_session(request, token)
    tenant_id = str(sess.get("tenant_id") or "default")
    try:
        from app.state import reports_store
        rows = reports_store.list_generated(tenant_id=tenant_id, limit=max(1, min(int(limit or 100), 500)))
    except TypeError:
        from app.state import reports_store
        rows = reports_store.list_generated()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"reports unavailable: {type(exc).__name__}: {exc}") from exc
    out = []
    for r in rows or []:
        out.append({
            "id": r.get("id") or r.get("generated_id"),
            "template_id": r.get("template_id"),
            "template_name": r.get("template_name") or r.get("name"),
            "created_utc": r.get("created_utc"),
            "file_name": r.get("file_name"),
            "file_bytes": r.get("file_bytes"),
            "status": r.get("status") or "done",
            "triggered_by": r.get("triggered_by"),
        })
    return {"ok": True, "rows": out, "source": "local_reports_store"}


@router.get("/reports/{generated_id}/file")
def get_lite_report_file(generated_id: str, request: Request, inline: bool = True, token: str = Query(default="")):
    _extract_session(request, token)
    from fastapi.responses import FileResponse
    from pathlib import Path as _P
    from app.state import reports_store
    record = reports_store.get_generated(generated_id)
    if not record:
        raise HTTPException(status_code=404, detail="Report not found")
    path = _P(str(record.get("file_path") or ""))
    if not path.exists():
        raise HTTPException(status_code=410, detail="Report file is no longer on disk")
    disposition = "inline" if inline else "attachment"
    name = record.get("file_name") or path.name
    return FileResponse(str(path), media_type="application/pdf", filename=name,
                        headers={"Content-Disposition": f'{disposition}; filename="{name}"'})
