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


def _load_app_settings() -> Dict[str, Any]:
    try:
        bootstrap = app_store.get_bootstrap() or {}
    except Exception:
        bootstrap = {}
    s = bootstrap.get("app_settings") if isinstance(bootstrap.get("app_settings"), dict) else {}
    return dict(s)


def _save_app_settings(s: Dict[str, Any]) -> None:
    app_store.upsert_domain("app_settings", s, actor="lan_sharing_router")


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


@router.get("/status")
def get_status(request: Request) -> dict:
    _require_admin(request)
    s = _load_app_settings()
    enabled = bool(s.get("lan_sharing_enabled"))
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
        "port": http_port or primary_port,        # legacy field (tray)
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
    if bool(s.get("lan_sharing_enabled")):
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
