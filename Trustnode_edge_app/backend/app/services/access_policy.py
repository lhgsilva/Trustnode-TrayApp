"""Central access policy for the edge API (operator 2026-08-21, Phase 1 of
docs/edge-runtime-lan-access-and-view-licensing-plan-2026-08-21.md).

Why this exists: server-side role enforcement used to live in a handful of
routers (`app_store`, `retention`) while `plc`, `reports`, `database`,
`connections`, `notifications`, `lan_sharing` had none, and
`license_inspect.has_module()` had ZERO callers — module licensing was UI-only.
That is fine while the UI is the local desktop; it is unacceptable once the
full runtime is reachable from other PCs on the LAN.

Three rules, evaluated in the auth middleware AFTER the JWT is verified:

1. ROLE by METHOD + PREFIX — `GET/HEAD/OPTIONS` are reads (any authenticated
   role). Mutations under configuration prefixes need `engineer`/`admin`/
   `super`; user / licence / LAN / connection management needs `admin`/`super`.
   A short OPERATOR allow-list covers operational actions (alarm ack, batch
   scan/start/stop, report run, export).
2. LICENCE GATES — `require_module(key)` dependencies (404, like the batch
   module) and the network rule below.
3. NETWORK ORIGIN never grants rights, it can only remove them: a mutating
   request from a NON-loopback client additionally needs the `remote_admin_lan`
   module (the "TrustNode Edge over LAN" permission). Loopback keeps today's
   behaviour exactly.

Modes (env, default chosen for safety):
  TRUSTNODE_RBAC_MODE      = enforce | log | off   (default: "lan" — see below)
  TRUSTNODE_LICENSE_GATES  = enforce | log | off   (default: "lan")
The special default "lan" means: ENFORCE for non-loopback clients (the new
exposure), LOG-ONLY for loopback (the desktop keeps working while the denial
log proves the policy). Every would-be denial is written to the customer log
and to cp_security_audit_log with user, role, method, path, ip.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import threading
import time
from typing import Any, Dict, Optional, Tuple

# Module-level so the string annotations (PEP 563) on the dependency
# closures resolve for FastAPI — a locally imported `Request` made FastAPI
# treat the `request` parameter as a QUERY field (422 "query.request missing").
from fastapi import HTTPException, Request

logger = logging.getLogger("trustnode.access")

ADMIN_ROLES = {"admin", "super"}
CONFIG_ROLES = {"admin", "super", "engineer"}
OPERATOR_ROLES = {"admin", "super", "engineer", "operator", "client_operator"}
READ_METHODS = {"GET", "HEAD", "OPTIONS"}

# Token-version cache shared with the auth middleware (username -> (mono, version)).
# Revocation pops the entry so a revoked token is refused on the very next request.
TV_CACHE: Dict[str, tuple] = {}


def invalidate_token_cache(username: str) -> None:
    try:
        TV_CACHE.pop(str(username or "").lower(), None)
    except Exception:
        pass

# Prefixes whose MUTATIONS are admin-only (users, licence, LAN exposure,
# integrations, data destinations, backups/retention, UI source, workspace).
ADMIN_ONLY_PREFIXES = (
    "/api/control-plane/",
    "/api/lan-sharing/",
    "/api/connections/",
    "/api/database/",
    "/api/customer-db/",
    "/api/app-store/retention",
    "/api/app-store/backup",
    "/api/ui-source/",
    "/api/workspace/",
    "/api/directories/",
)

# Mutations an OPERATOR may perform remotely (operational, not configuration).
# Reviewed with the owner 2026-08-21: alarm acknowledgement, batch scan /
# start / stop / hold / resume / comments, report run / generate / export,
# own password change, gateway start/stop (operators restart collection).
OPERATOR_ALLOW = (
    re.compile(r"^/api/auth/(change-password|logout|session-cookie)$"),
    re.compile(r"^/api/app-store/alarms/.*/(ack|acknowledge)"),
    re.compile(r"^/api/batch-management/v2/batches/scan$"),
    re.compile(r"^/api/batch-management/v2/batches/[^/]+/(start|stop|hold|resume|comments)$"),
    re.compile(r"^/api/batch-management/batches/(scan|[^/]+/(start|stop|next-child))$"),
    re.compile(r"^/api/reports/(render|export/(csv|txt)|templates/[^/]+/generate|schedules/[^/]+/run)$"),
    re.compile(r"^/api/historian/export-xlsx$"),
    re.compile(r"^/api/plc/(start|stop|gateways/start|gateways/stop)$"),
)

# Paths that are not "configuration" even though they are mutations — any
# authenticated role may call them (telemetry ingest has its own auth, the
# lite-local API has its own token checks, cloud-live is separate).
NEUTRAL_PREFIXES = (
    "/api/v1/",
    "/api/lite-local/",
    "/api/lite-view/",
    "/api/cloud-live/",
    "/api/auth/",
)

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


def _mode(env_name: str) -> str:
    v = str(os.environ.get(env_name, "lan") or "lan").strip().lower()
    return v if v in {"enforce", "log", "off", "lan"} else "lan"


def rbac_mode() -> str:
    return _mode("TRUSTNODE_RBAC_MODE")


def license_gate_mode() -> str:
    return _mode("TRUSTNODE_LICENSE_GATES")


def is_loopback_host(host: str) -> bool:
    h = str(host or "").strip().lower()
    if not h:
        return True  # no client info (tests / ASGI without client) — treat as local
    if h in _LOOPBACK:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def client_host(request: Any) -> str:
    try:
        return str((request.client.host if request.client else "") or "")
    except Exception:
        return ""


def request_is_remote(request: Any) -> bool:
    return not is_loopback_host(client_host(request))


def _effective(mode: str, remote: bool) -> str:
    """Resolve the 'lan' default into enforce/log per origin."""
    if mode == "lan":
        return "enforce" if remote else "log"
    return mode


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------
_audit_lock = threading.Lock()
_audit_last: Dict[str, float] = {}


def audit(action: str, *, outcome: str, request: Any = None, payload: Optional[Dict[str, Any]] = None,
          details: Optional[Dict[str, Any]] = None, rate_key: str = "") -> None:
    """Write to the customer log + cp_security_audit_log. Never raises.
    `rate_key` collapses repeated identical denials to one row per minute."""
    try:
        now = time.monotonic()
        if rate_key:
            with _audit_lock:
                last = _audit_last.get(rate_key, 0.0)
                if now - last < 60.0:
                    return
                _audit_last[rate_key] = now
                if len(_audit_last) > 2000:
                    _audit_last.clear()
        user = ""
        role = ""
        tenant = "default"
        if isinstance(payload, dict):
            user = str(payload.get("sub") or payload.get("username") or "")
            role = str(payload.get("role") or "")
            tenant = str(payload.get("tenant_id") or "default")
        det = dict(details or {})
        if request is not None:
            try:
                det.setdefault("method", str(request.method))
                det.setdefault("path", str(request.url.path))
                det.setdefault("ip", client_host(request))
            except Exception:
                pass
        det.setdefault("role", role)
        try:
            from app.state import control_plane_store
            control_plane_store.audit(
                actor_type="user" if user else "anonymous", actor_id=user or "-", tenant_id=tenant,
                action=action, outcome=outcome, correlation_id=f"acc-{int(time.time())}", details=det,
            )
        except Exception:
            pass
        try:
            from app.state import app_store
            level = "warning" if outcome in ("denied", "would_deny") else "info"
            # AppStore exposes append_log_rows(rows) — the singular append_log
            # this used to call does not exist, so every audit line was being
            # swallowed by the except below (found 2026-08-21).
            app_store.append_log_rows([{
                "level": level, "category": "access",
                "message": f"{action} {outcome}: user={user or '-'} role={role or '-'} "
                           f"{det.get('method', '')} {det.get('path', '')} ip={det.get('ip', '')}"
                           + (f" reason={det.get('reason')}" if det.get("reason") else ""),
            }])
        except Exception:
            logger.info("access %s %s user=%s role=%s %s", action, outcome, user, role, det)
    except Exception:
        pass


# --------------------------------------------------------------------------
# licence helpers
# --------------------------------------------------------------------------
# Keys introduced 2026-08-21. Licences issued by the portal BEFORE the tier
# editor (no package_key) cannot carry them yet; for those we derive:
#   remote_admin_lan  := lan_access AND local_web_app   (LAN web access was
#                        sold as "LAN Sharing & LAN Web Access")
#   view_share_links  := lan_access                     (existing share links
#                        keep working; new tier licences must be explicit)
# A licence WITH a package_key (new portal) is taken literally.
_LEGACY_DERIVED = {
    "remote_admin_lan": ("lan_access", "local_web_app"),
    "view_share_links": ("lan_access",),
}


def has_module(key: str) -> bool:
    try:
        from app.services import license_inspect
        if license_inspect.has_module(key):
            return True
        if key in _LEGACY_DERIVED and not license_inspect.raw_package_key():
            return all(license_inspect.has_module(k) for k in _LEGACY_DERIVED[key])
        return False
    except Exception:
        return True  # never brick the edge on an evaluation error


def license_status(keys: Tuple[str, ...] = ("lan_access", "local_web_app", "remote_admin_lan", "view_share_links")) -> Dict[str, bool]:
    return {k: has_module(k) for k in keys}


def require_module(key: str):
    """FastAPI dependency factory: 404 when the licence lacks `key` (same
    posture as the batch module — an unlicensed surface does not exist).
    Honours TRUSTNODE_LICENSE_GATES (lan default: enforce for remote clients,
    log for loopback)."""
    def _dep(request: Request) -> None:
        if has_module(key):
            return
        remote = request_is_remote(request)
        eff = _effective(license_gate_mode(), remote)
        if eff == "off":
            return
        payload = getattr(request.state, "user_payload", None)
        audit("license_gate", outcome="denied" if eff == "enforce" else "would_deny", request=request,
              payload=payload, details={"module": key}, rate_key=f"lic:{key}:{client_host(request)}")
        if eff == "enforce":
            raise HTTPException(status_code=404, detail="Not found")

    return _dep


# --------------------------------------------------------------------------
# role policy
# --------------------------------------------------------------------------
def _required_roles(method: str, path: str) -> Optional[set]:
    """Return the role set allowed for this mutation, or None for 'any role'."""
    if method in READ_METHODS:
        return None
    for p in NEUTRAL_PREFIXES:
        if path.startswith(p):
            return None
    for pat in OPERATOR_ALLOW:
        if pat.search(path):
            return set(OPERATOR_ROLES)  # operators and above — never plain viewers
    for p in ADMIN_ONLY_PREFIXES:
        if path.startswith(p):
            return set(ADMIN_ROLES)
    # everything else under /api/ that mutates = configuration → engineer+
    return set(CONFIG_ROLES)


def evaluate(request: Any, payload: Dict[str, Any]) -> Tuple[bool, str, str]:
    """(allowed, reason, effective_mode). Called by the auth middleware for
    every authenticated /api request. Never raises."""
    try:
        method = str(request.method or "GET").upper()
        path = str(request.url.path or "")
        role = str(payload.get("role") or "viewer").strip().lower()
        remote = request_is_remote(request)
        eff = _effective(rbac_mode(), remote)
        reason = ""
        required = _required_roles(method, path)
        if required is not None and role not in required:
            reason = f"role '{role}' may not {method} {path}"
        # network rule: remote mutations additionally need remote_admin_lan
        if not reason and remote and method not in READ_METHODS and not any(path.startswith(p) for p in NEUTRAL_PREFIXES):
            lic_eff = _effective(license_gate_mode(), remote)
            if lic_eff != "off" and not has_module("remote_admin_lan"):
                reason = "remote configuration requires the 'remote_admin_lan' licence module"
                if lic_eff == "log":
                    audit("rbac", outcome="would_deny", request=request, payload=payload,
                          details={"reason": reason}, rate_key=f"ral:{path}:{client_host(request)}")
                    return True, reason, "log"
        if not reason:
            return True, "", eff
        if eff == "off":
            return True, reason, eff
        audit("rbac", outcome="denied" if eff == "enforce" else "would_deny", request=request,
              payload=payload, details={"reason": reason},
              rate_key=f"rbac:{role}:{method}:{path}:{client_host(request)}")
        return (eff != "enforce"), reason, eff
    except Exception as exc:
        logger.debug("access evaluate failed open: %r", exc)
        return True, "", "off"


# --------------------------------------------------------------------------
# static-surface guard (/trustnode/{full|client|lite}/app/)
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Licence seats (2026-08-22)
# --------------------------------------------------------------------------
# Which seat product entitles a person to each browser surface. Cloud View is
# absent on purpose: it is served by the hosted Lite app, not by this edge.
SURFACE_SEAT = {"full": "studio", "client": "view_lan", "lite": "view_lan"}


def user_seats(payload: Dict[str, Any]) -> list:
    """Seats carried by a verified session.

    Token claim first, then the stored user record - the same precedence the
    access_* flags already use, so a freshly issued token wins and an older one
    still resolves correctly."""
    perms = payload.get("permissions") if isinstance(payload.get("permissions"), dict) else None
    if isinstance(perms, dict) and isinstance(perms.get("seats"), list):
        return [str(x).strip().lower() for x in perms.get("seats") or [] if str(x).strip()]
    try:
        from app.state import auth_store as _as
        rec = _as.get_user(str(payload.get("sub") or "")) or {}
        seats = rec.get("seats")
        if isinstance(seats, list) and seats:
            return [str(x).strip().lower() for x in seats if str(x).strip()]
        rec_perms = rec.get("permissions") if isinstance(rec.get("permissions"), dict) else {}
        if isinstance(rec_perms.get("seats"), list):
            return [str(x).strip().lower() for x in rec_perms.get("seats") or [] if str(x).strip()]
    except Exception:
        pass
    return []


def seat_required_for_surface(surface: str) -> str:
    return SURFACE_SEAT.get(str(surface or "").strip().lower(), "")


SURFACE_RE = re.compile(r"^/trustnode/(full|client|lite)/app/")
SURFACE_FLAG = {"full": "access_full", "client": "access_client", "lite": "access_lite"}
SURFACE_MODULE = {"full": "remote_admin_lan", "client": "local_web_app", "lite": "local_web_app"}
SESSION_COOKIE = "tn_session"


def surface_of(path: str) -> str:
    m = SURFACE_RE.match(str(path or ""))
    return m.group(1) if m else ""


def token_from_request(request: Any) -> str:
    """Bearer header, else the HttpOnly session cookie set at login."""
    try:
        auth = request.headers.get("Authorization", "") or ""
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return str(request.cookies.get(SESSION_COOKIE) or "").strip()
    except Exception:
        return ""


def surface_access(surface: str, payload: Dict[str, Any], remote: bool) -> Tuple[bool, str]:
    """Decide whether a verified session may load a LAN surface bundle."""
    role = str(payload.get("role") or "viewer").strip().lower()
    username = str(payload.get("sub") or payload.get("username") or "")
    # licence (remote only for the full runtime; View surfaces always need local_web_app)
    mod = SURFACE_MODULE.get(surface, "")
    lic_eff = _effective(license_gate_mode(), remote)
    if lic_eff == "enforce" and mod:
        if surface == "full":
            if remote and not has_module(mod):
                return False, "licence:remote_admin_lan"
        elif not has_module(mod):
            return False, f"licence:{mod}"
    # full runtime from the LAN: admin or engineer only (owner decision 2026-08-21)
    if surface == "full" and remote and role not in CONFIG_ROLES:
        return False, "role"
    # Named licence seats (2026-08-22). Only enforced for REMOTE clients and
    # only when the portal issued a licence that actually carries seat counts —
    # a licence predating seats keeps the previous behaviour untouched, and the
    # desktop (loopback) is never gated on a seat.
    if remote and lic_eff == "enforce":
        try:
            from app.services import license_inspect as _li
            from app.services import seats as _seats
            need = seat_required_for_surface(surface)
            if need and _li.seats_are_explicit():
                held = user_seats(payload)
                if need not in held:
                    return False, f"seat:{need}"
                if surface in ("client", "lite"):
                    # A View LAN seat is served ONE ui; the other is not theirs.
                    want = "lite" if surface == "lite" else "app_readonly"
                    if _seats.view_ui_of_user({"permissions": payload.get("permissions") or {}}) != want:
                        return False, "seat:view_ui"
        except Exception:
            pass
    if role in ADMIN_ROLES:
        return True, "role"
    # per-user access flag: the token's permissions claim (issued at login from
    # the auth store) first, then the users_access config document.
    try:
        tok_perms = payload.get("permissions") if isinstance(payload.get("permissions"), dict) else {}
        if bool(tok_perms.get(SURFACE_FLAG.get(surface, ""))):
            return True, "permission"
    except Exception:
        pass
    try:
        from app.state import app_store
        bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
        users_access = bootstrap.get("users_access") if isinstance(bootstrap.get("users_access"), dict) else {}
        for u in (users_access.get("users") or []):
            if str(u.get("username") or "") == username:
                perms = u.get("permissions") or {}
                if bool(perms.get(SURFACE_FLAG.get(surface, ""))):
                    return True, "permission"
                break
    except Exception:
        pass
    # engineers get the full runtime by role (they may configure) even without the flag
    if surface == "full" and role == "engineer":
        return True, "role"
    return False, "permission"


def cookie_kwargs(request: Any, max_age: int) -> Dict[str, Any]:
    secure = False
    try:
        secure = str(request.url.scheme or "").lower() == "https"
    except Exception:
        pass
    return {"httponly": True, "samesite": "lax", "secure": secure, "path": "/", "max_age": int(max_age)}
