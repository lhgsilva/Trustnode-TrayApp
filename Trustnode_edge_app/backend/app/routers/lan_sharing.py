"""LAN sharing (operator 2026-06-17, M6).

Endpoints under /api/lan-sharing/* let the Settings UI:

  * GET  /status          — see whether sharing is on, which IPs the
                            backend is currently bound to, the LAN
                            URL clients can open, and whether a restart
                            is needed for a recent flag change.
  * POST /enable           — flip app_settings.lan_sharing_enabled = True.
                             Returns a "restart_required" hint when the
                             current bind host is 127.0.0.1.
  * POST /disable          — flip the flag off + restart hint.

Actually flipping the bind host requires the operator to relaunch the
edge (Electron tray "Restart" / OS service restart). We surface the
state so the UI can guide them; we do not kill our own backend.
"""
from __future__ import annotations

import socket
from typing import Any, Dict, List

from fastapi import APIRouter

from app.config import settings
from app.state import app_store
from app.services import lan_socket

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


def _local_ip_addresses() -> List[str]:
    """Enumerate the host's non-loopback IPv4 addresses.

    Used to print "Lite is reachable at http://<ip>:8000" in the UI.
    Best-effort — falls back to a single UDP-trick lookup if the
    hostname route fails (the trick doesn't actually send any packets
    but tells the OS to pick the outbound interface).
    """
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


@router.get("/status")
def get_status() -> dict:
    s = _load_app_settings()
    enabled = bool(s.get("lan_sharing_enabled"))
    lan_live = lan_socket.is_running()
    # If the flag is on but the socket isn't (just-after-boot), try to
    # bring it up now. Cheap idempotent call.
    if enabled and not lan_live:
        try:
            lan_socket.sync_with_settings(True, int(settings.trustnode_port))
            lan_live = lan_socket.is_running()
        except Exception:
            pass
    lan_port = lan_socket.current_port()
    ips = _local_ip_addresses() if lan_live else []
    primary_port = int(settings.trustnode_port)
    # Lite URLs use the LAN-side port (not the primary 127.0.0.1 port)
    # so LAN clients hit the 0.0.0.0 socket.
    lite_urls = [f"http://{ip}:{lan_port}/lite/" for ip in ips] if lan_port else []
    return {
        "ok": True,
        "enabled": enabled,
        "running": lan_live,
        "bind_host": "0.0.0.0" if lan_live else "127.0.0.1",
        # `port` stays for backwards compatibility with the tray menu;
        # `primary_port` and `lan_port` are explicit so consumers can
        # pick the right one for the URL they need to print.
        "port": lan_port or primary_port,
        "primary_port": primary_port,
        "lan_port": lan_port,
        "ips": ips,
        "lite_urls": lite_urls,
        "restart_required": False,
        "last_error": lan_socket.last_error(),
    }


@router.post("/enable")
def post_enable() -> dict:
    s = _load_app_settings()
    s["lan_sharing_enabled"] = True
    _save_app_settings(s)
    res = lan_socket.sync_with_settings(True, int(settings.trustnode_port))
    # Pull last_error AFTER sync so it reflects the failed bind, not a
    # stale prior failure. The tray dialog renders this verbatim.
    return {
        "ok": bool(res.get("ok")),
        "enabled": True,
        "running": lan_socket.is_running(),
        "port": res.get("port"),
        "candidates_tried": res.get("candidates_tried") or [],
        "note": res.get("note") or "",
        "last_error": lan_socket.last_error() or "",
        "restart_required": False,
    }


@router.post("/disable")
def post_disable() -> dict:
    s = _load_app_settings()
    s["lan_sharing_enabled"] = False
    _save_app_settings(s)
    res = lan_socket.sync_with_settings(False, int(settings.trustnode_port))
    return {
        "ok": bool(res.get("ok")),
        "enabled": False,
        "running": lan_socket.is_running(),
        "restart_required": False,
        "note": res.get("note") or "",
    }
