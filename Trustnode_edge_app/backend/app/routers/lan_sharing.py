"""Remote Access (formerly "LAN sharing") — operator 2026-06-17 (M6), rebuilt
2026-08-21 for docs/edge-runtime-lan-access-and-view-licensing-plan.

Endpoints under /api/lan-sharing/*:

  * GET  /status        — on/off, listeners (HTTP 8088+, HTTPS 8443+), every
                          LAN IP + hostname, one URL per surface
                          (Edge full / Local View / legacy Lite), licence state
                          of each surface, active remote sessions.
  * POST /enable|disable— flip app_settings.lan_sharing_enabled (admin;
                          `lan_access` licence).
  * PUT  /config        — {https_only, http_enabled, bind_host} (admin).
  * GET  /certificate   — the LAN certificate (PEM) for the trust guide (public).
  * GET  /sessions      — active sessions (admin).
  * POST /sessions/revoke {username} — invalidate every token of a user (admin).

Authorization: loopback callers without a JWT (the Electron tray) are allowed
as before; any other caller must carry an admin/super JWT (the auth middleware
has already verified it). Licence gate `lan_access` applies to enable.
"""
from __future__ import annotations

import socket
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Request, Response

from app.config import settings
from app.state import app_store
from app.services import lan_socket, access_policy as _access

router = APIRouter(prefix="/api/lan-sharing", tags=["lan-sharing"])

# Firewall probe cache: (taken_at, payload). See _firewall_allows_backend.
import time as _time_fw
_FW_CACHE: tuple[float, dict] | None = None
_FW_CACHE_TTL_S = 15.0


# The four keys lan_socket actually reads. Nothing else in app_settings has
# any bearing on the listeners, so nothing else travels with them.
_LAN_KEYS = ("lan_sharing_enabled", "lan_http_enabled", "lan_https_only",
             "lan_bind_host")


def _load_app_settings() -> Dict[str, Any]:
    """This edge's LAN settings.

    2026-10-30. These used to be read from, and written to, `config_documents`
    - the legacy UNSCOPED table. One SQLite file routinely holds documents for
    several edges; the operator's own store holds three (edge-01,
    edge-74d903ffcd, edge-7e86b73f66). So "the LAN settings" were whichever
    edge wrote them last, and a re-activated install inherited the previous
    one's listener configuration. CLAUDE.md rule 1b: when the edge has an
    identity it must never be served the unscoped document.

    They now live in their own site-level domain, keyed `tenant|customer|edge`,
    because the listeners are a property of the MACHINE. app_settings is a
    per-user document (theme, last profile), which is the wrong home for a
    setting that decides whether other computers can reach this edge at all.

    An edge with no identity yet still reads and writes unscoped, because that
    is where its saves go too - get_domain_scoped_fast() already draws that
    line, so the rule lives in one place.

    First run on an existing install seeds from the old unscoped app_settings,
    so nobody has to turn Remote Access on again after upgrading.
    """
    try:
        doc = app_store.get_domain_scoped_fast("lan_sharing")
    except Exception:
        doc = None
    if isinstance(doc, dict) and doc:
        return dict(doc)

    # Nothing stored yet: carry the old values across, once.
    seeded: Dict[str, Any] = {}
    try:
        bootstrap = app_store.get_bootstrap() or {}
        legacy = bootstrap.get("app_settings")
        if isinstance(legacy, dict):
            seeded = {k: legacy[k] for k in _LAN_KEYS if k in legacy}
    except Exception:
        seeded = {}
    if seeded:
        try:
            _save_app_settings(seeded)
            print("[trustnode][lan] migrated %d LAN setting(s) out of the "
                  "unscoped document into this edge's own scope"
                  % len(seeded), flush=True)
        except Exception as exc:                            # noqa: BLE001
            # A failed WRITE must not turn into a failed READ - returning {}
            # here would read as "sharing off" and is how a config wipe
            # starts (see config_wipe_on_failed_read).
            print("[trustnode][lan] could not migrate LAN settings: %r" % exc,
                  flush=True)
    return seeded


def _save_app_settings(s: Dict[str, Any]) -> None:
    """Write the LAN settings to this edge's scope, never the shared table.

    Merged onto what is stored, not substituted for it. The callers do
    `s = _load(); s["one_key"] = x; _save(s)`, so if that load had come back
    short - a transient read failure, a store still opening - a replacing
    write would quietly drop the other three settings. That is the partial
    blank write that has cost this app a day more than once.
    """
    incoming = {k: v for k, v in (s or {}).items() if k in _LAN_KEYS}
    payload = {}
    try:
        current = app_store.get_domain_scoped_fast("lan_sharing")
        if isinstance(current, dict):
            payload.update({k: v for k, v in current.items() if k in _LAN_KEYS})
    except Exception:
        pass
    payload.update(incoming)
    try:
        scope = app_store.site_scope_key()
    except Exception:
        scope = ""
    if scope:
        app_store.upsert_domain_scoped(scope, "lan_sharing", payload,
                                       actor="lan_sharing_router")
    else:
        # No identity: unscoped is where this edge's saves belong.
        app_store.upsert_domain("lan_sharing", payload, actor="lan_sharing_router")


def _require_admin(request: Request) -> Optional[Dict[str, Any]]:
    """Loopback without a token = the tray (allowed). Otherwise admin/super."""
    payload = getattr(request.state, "user_payload", None)
    if not payload:
        if not _access.request_is_remote(request):
            return None
        raise HTTPException(status_code=401, detail="Authentication required")
    role = str(payload.get("role") or "").strip().lower()
    if role not in _access.ADMIN_ROLES:
        _access.audit("remote_access", outcome="denied", request=request, payload=payload,
                      details={"reason": "admin role required"})
        raise HTTPException(status_code=403, detail="Admin role required")
    return payload


def _local_ip_addresses() -> List[str]:
    """Enumerate the host's non-loopback IPv4 addresses (best-effort)."""
    ips: List[str] = []
    try:
        for entry in socket.getaddrinfo(socket.gethostname(), None):
            family, _, _, _, sockaddr = entry
            if family == socket.AF_INET:
                ip = str(sockaddr[0])
                if ip and not ip.startswith("127.") and ip not in ips:
                    ips.append(ip)
    except Exception:
        pass
    if not ips:
        try:
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sk.settimeout(0.5)
            sk.connect(("8.8.8.8", 80))
            ips.append(sk.getsockname()[0])
            sk.close()
        except Exception:
            pass
    return ips


_SURFACE_PATHS = {"full": "/trustnode/full/", "view": "/trustnode/client/", "lite": "/trustnode/lite/"}


def _urls(scheme: str, hosts: List[str], port: int) -> Dict[str, List[str]]:
    if not port:
        return {k: [] for k in _SURFACE_PATHS}
    return {k: [f"{scheme}://{h}:{port}{p}" for h in hosts] for k, p in _SURFACE_PATHS.items()}


def _firewall_allows_backend() -> dict:
    """Can the LAN actually reach this process, and if not, why?

    2026-10-27, reported: "remote lan connections still not working ...
    blocked on the edge for both http and https". Measured: HTTPS answered
    200 on the host's own LAN address while Windows had no inbound rule for
    the running executable at all, so every OTHER computer was dropped.

    `lan_socket.start()` already calls windows_firewall.ensure_backend_rule()
    - inside `except Exception: pass`, so when it fails nothing is recorded
    and nothing is shown. On the portable build it fails every time:

        "The requested operation requires elevation (Run as administrator)."

    The installer runs elevated and gets its rule; the portable never can.
    Reporting it is what lets the Remote Access page offer the one-click
    fix below instead of leaving the operator with a dead page.
    """
    # 2026-10-30, measured against the live edge: /api/lan-sharing/status
    # took 85-150 ms while every other endpoint answered in 5-7 ms, and the
    # Remote Access page polls it. The cost is this netsh call (53 ms on its
    # own). A firewall rule does not change between two polls, so the answer
    # is cached briefly - the page stays truthful and stops paying for the
    # same shell-out several times a second.
    global _FW_CACHE
    _now = _time_fw.time()
    if _FW_CACHE and (_now - _FW_CACHE[0]) < _FW_CACHE_TTL_S:
        return dict(_FW_CACHE[1])
    try:
        from app.services import windows_firewall as _fw
        if not _fw._is_windows():                    # noqa: SLF001
            return {"checked": False, "present": None, "reason": "not windows"}
        exe = _fw._current_exe_path()                # noqa: SLF001
        if not exe:
            return {"checked": False, "present": None,
                    "reason": "exe path not resolvable"}
        present = bool(_fw._rule_exists_for_path(exe))   # noqa: SLF001
        out = {"checked": True, "present": present, "program": exe,
               "reason": "" if present else
                         "no inbound rule allows this program - other "
                         "computers cannot reach it"}
        _FW_CACHE = (_now, dict(out))
        return dict(out)
    except Exception as exc:                         # noqa: BLE001
        return {"checked": False, "present": None, "reason": type(exc).__name__}

@router.get("/status")
def get_status(request: Request) -> dict:
    _require_admin(request)
    s = _load_app_settings()
    enabled = lan_socket.sharing_enabled(s)
    lan_live = lan_socket.is_running()
    # If the flag is on but the sockets are down (just-after-boot), bring them up.
    if enabled and not lan_live:
        try:
            lan_socket.sync_with_settings(True, int(settings.trustnode_port), s)
            lan_live = lan_socket.is_running()
        except Exception:
            pass
    http_port = lan_socket.current_port()
    https_port = lan_socket.current_https_port()
    ips = _local_ip_addresses() if lan_live else []
    hostname = socket.gethostname() or ""
    primary_port = int(settings.trustnode_port)
    http_urls = _urls("http", ips, http_port)
    https_urls = _urls("https", ips, https_port)
    host_urls_http = _urls("http", [hostname], http_port) if hostname else {k: [] for k in _SURFACE_PATHS}
    host_urls_https = _urls("https", [hostname], https_port) if hostname else {k: [] for k in _SURFACE_PATHS}
    tls_info: Dict[str, Any] = {}
    try:
        from app.services import lan_tls
        tls_info = lan_tls.describe() or {}
    except Exception:
        tls_info = {}
    sessions: List[Dict[str, Any]] = []
    try:
        from app.services import view_sessions
        sessions = view_sessions.list_active(include_local=False)
    except Exception:
        sessions = []
    import os as _os
    master_default = not bool(str(_os.environ.get("TRUSTNODE_MASTER_ADMIN_PASSWORD", "") or "").strip())
    return {
        "ok": True,
        "enabled": enabled,
        "running": lan_live,
        "bind_host": lan_socket.bind_host() if lan_live else "127.0.0.1",
        # 2026-10-27: this fell back to primary_port when HTTP was not
        # LAN-exposed, so the status reported 8000 - a LOOPBACK-only
        # listener - as though it were the LAN port, and the operator was
        # handed an http:// address that refuses every connection.
        # Measured: http://<lan-ip>:8000 refused while https://<lan-ip>:8443
        # answered 200. 0 now means "not reachable over the LAN".
        "port": http_port,                        # legacy field (tray)
        "http_lan_exposed": bool(http_port),
        "https_lan_exposed": bool(https_port),
        "firewall": _firewall_allows_backend(),
        "primary_port": primary_port,
        "lan_port": http_port,
        "https_port": https_port,
        "http_enabled": bool(s.get("lan_http_enabled", True)),
        "https_only": bool(s.get("lan_https_only", False)),
        "ips": ips,
        "hostname": hostname,
        # legacy URL lists (tray + old UI) — keep pointing at the same surfaces
        "lite_urls": http_urls["lite"],
        "full_urls": http_urls["full"],
        "view_urls": http_urls["view"],
        "hostname_urls": {"full": host_urls_http["full"], "view": host_urls_http["view"], "lite": host_urls_http["lite"]},
        "https": {
            "available": bool(https_port),
            "port": https_port,
            "https_only": bool(s.get("lan_https_only", False)),
            "urls": https_urls,
            "hostname_urls": {"full": host_urls_https["full"], "view": host_urls_https["view"], "lite": host_urls_https["lite"]},
            "cert_fingerprint_sha256": str(tls_info.get("fingerprint_sha256") or ""),
            "cert_kind": str(tls_info.get("kind") or ""),
            "cert_not_after_utc": str(tls_info.get("not_after_utc") or ""),
            "cert_url": "/api/lan-sharing/certificate",
        },
        "licensed": _access.license_status(),
        "rbac_mode": _access.rbac_mode(),
        "license_gate_mode": _access.license_gate_mode(),
        "master_admin_default_password": master_default,
        "sessions": sessions,
        "restart_required": False,
        "last_error": lan_socket.last_error(),
    }


@router.post("/enable")
def post_enable(request: Request) -> dict:
    payload = _require_admin(request)
    # licence: lan_access (log-only on loopback, enforced for remote callers)
    _access.require_module("lan_access")(request)
    s = _load_app_settings()
    s["lan_sharing_enabled"] = True
    _save_app_settings(s)
    res = lan_socket.sync_with_settings(True, int(settings.trustnode_port), s)
    _access.audit("remote_access.enable", outcome="ok" if res.get("ok") else "failed", request=request, payload=payload,
                  details={"port": res.get("port"), "https_port": res.get("https_port"), "note": res.get("note") or ""})
    return {
        "ok": bool(res.get("ok")),
        "enabled": True,
        "running": lan_socket.is_running(),
        "port": res.get("port"),
        "https_port": res.get("https_port"),
        "candidates_tried": res.get("candidates_tried") or [],
        "note": res.get("note") or "",
        "last_error": lan_socket.last_error() or "",
        "restart_required": False,
    }


@router.post("/disable")
def post_disable(request: Request) -> dict:
    payload = _require_admin(request)
    s = _load_app_settings()
    s["lan_sharing_enabled"] = False
    _save_app_settings(s)
    res = lan_socket.sync_with_settings(False, int(settings.trustnode_port), s)
    _access.audit("remote_access.disable", outcome="ok", request=request, payload=payload, details={})
    return {
        "ok": bool(res.get("ok")),
        "enabled": False,
        "running": lan_socket.is_running(),
        "restart_required": False,
        "note": res.get("note") or "",
    }


@router.put("/config")
def put_config(request: Request, body: Dict[str, Any] = Body(default={})) -> dict:
    payload = _require_admin(request)
    s = _load_app_settings()
    changed: Dict[str, Any] = {}
    if "https_only" in body:
        s["lan_https_only"] = bool(body.get("https_only"))
        changed["https_only"] = s["lan_https_only"]
        if s["lan_https_only"]:
            s["lan_http_enabled"] = False
    if "http_enabled" in body:
        s["lan_http_enabled"] = bool(body.get("http_enabled"))
        changed["http_enabled"] = s["lan_http_enabled"]
        if s["lan_http_enabled"]:
            s["lan_https_only"] = False
    if "bind_host" in body:
        host = str(body.get("bind_host") or "0.0.0.0").strip() or "0.0.0.0"
        s["lan_bind_host"] = host
        changed["bind_host"] = host
    _save_app_settings(s)
    res: Dict[str, Any] = {}
    if lan_socket.sharing_enabled(s):
        res = lan_socket.sync_with_settings(True, int(settings.trustnode_port), s)
    _access.audit("remote_access.config", outcome="ok", request=request, payload=payload, details=changed)
    return {"ok": True, "changed": changed, "running": lan_socket.is_running(),
            "port": lan_socket.current_port(), "https_port": lan_socket.current_https_port(),
            "note": res.get("note") or ""}


@router.get("/certificate")
def get_certificate() -> Response:
    """Public: the LAN certificate for the trust guide (a public key, not a secret)."""
    try:
        from app.services import lan_tls
        pem = lan_tls.certificate_pem()
    except Exception:
        pem = None
    if not pem:
        raise HTTPException(status_code=404, detail="No LAN certificate yet — turn Remote Access on first")
    return Response(content=pem, media_type="application/x-pem-file",
                    headers={"Content-Disposition": 'attachment; filename="trustnode-edge-lan.crt"'})


@router.get("/sessions")
def get_sessions(request: Request) -> dict:
    _require_admin(request)
    from app.services import view_sessions
    return {"ok": True, "rows": view_sessions.list_active(include_local=True)}


@router.post("/sessions/revoke")
def post_revoke(request: Request, body: Dict[str, Any] = Body(default={})) -> dict:
    payload = _require_admin(request)
    username = str(body.get("username") or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username required")
    from app.state import auth_store
    from app.services import view_sessions
    version = auth_store.bump_token_version(username)
    _access.invalidate_token_cache(username)
    view_sessions.forget(username)
    _access.audit("remote_access.revoke", outcome="ok", request=request, payload=payload,
                  details={"revoked_user": username, "token_version": version})
    return {"ok": True, "username": username, "token_version": version}

@router.post("/firewall-rule")
def post_firewall_rule(request: Request) -> dict:
    """Create the inbound rule for this executable, with one UAC prompt.

    Offered by the Remote Access page when the status reports the rule is
    missing. Admin-only: it changes the host firewall.
    """
    _require_admin(request)
    from app.services import windows_firewall as _fw
    out = _fw.add_rule_elevated()
    return {"ok": bool(out.get("ok")), "added": bool(out.get("added")),
            "reason": str(out.get("reason") or ""),
            "program": str(out.get("exe_path") or "")}
