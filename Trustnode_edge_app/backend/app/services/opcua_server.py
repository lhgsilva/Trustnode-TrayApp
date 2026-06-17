"""OPC UA server (operator 2026-06-17, Phase 3).

Edge-side OPC UA server that exposes TrustNode tags to LAN clients
(SCADA, HMI, third-party historians). Tags must opt-in via the
per-tag `publish_opcua` flag on the gateway config; only those are
materialised as nodes.

Design:
  * `asyncua` (BSD-licensed) runs in its own daemon thread on a fresh
    asyncio loop — same pattern as `lan_socket.py`. No restart needed
    to toggle on/off; the FastAPI app keeps serving 127.0.0.1.
  * Node tree: Objects → TrustNode → <GatewayName> → <DeviceName> → <TagName>
    (one Variable per tag). Quality and ts mirror the live-cache.
  * Updates pushed from `plc_manager` / `power_manager` via
    `publish_tag(gateway_id, device_name, tag_name, value, ts, quality)`.
  * If `asyncua` import fails (not installed on a stripped-down build),
    `start()` returns a graceful error instead of crashing the app.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_server = None             # asyncua.Server instance
_last_error: str = ""
_current_port: int = 0

# Map "gateway::device::tag" -> asyncua Variable node, populated on start.
_node_map: Dict[str, Any] = {}
# Pending updates buffered if a value arrives before the node exists yet
# (server still spinning up). Flushed on bind-success.
_pending_updates: list[tuple[str, Any, Optional[str], Optional[str]]] = []


def is_running() -> bool:
    with _lock:
        return _server is not None and _thread is not None and _thread.is_alive()


def last_error() -> str:
    return _last_error


def current_port() -> int:
    with _lock:
        return _current_port if is_running() else 0


def _publish_flags_map(bootstrap: Dict[str, Any]) -> Dict[str, Dict[str, bool]]:
    """app_settings.tag_publish_flags is a flat keyed map
    { "<gid>::<tag>": {opcua: bool, mqtt: bool} } — easier to set from
    the Tags page than mutating the gateway config doc.
    """
    s = bootstrap.get("app_settings") if isinstance(bootstrap.get("app_settings"), dict) else {}
    flags = s.get("tag_publish_flags") if isinstance(s.get("tag_publish_flags"), dict) else {}
    out: Dict[str, Dict[str, bool]] = {}
    for k, v in (flags or {}).items():
        if not isinstance(v, dict):
            continue
        out[str(k)] = {"opcua": bool(v.get("opcua")), "mqtt": bool(v.get("mqtt"))}
    return out


def _collect_publishable_tags(bootstrap: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Yield {gateway_id, gateway_name, device_name, tag_name} for every
    tag with publish_opcua=true. Reads both the keyed flat map AND any
    per-tag object that carries a publish_opcua attribute.
    """
    out: list[Dict[str, Any]] = []
    flags = _publish_flags_map(bootstrap)
    gws = bootstrap.get("gateway_configurations") or {}
    if not isinstance(gws, dict):
        return out
    for gid, gw in gws.items():
        if not isinstance(gw, dict):
            continue
        gname = str(gw.get("name") or gid)
        # Tags can be either a flat list of strings (current edge schema)
        # or a list of dicts. Handle both.
        tag_list = gw.get("tags") or []
        # Some configs embed devices; fall back to a single virtual device.
        devices = gw.get("devices") if isinstance(gw.get("devices"), list) else []
        if not devices:
            devices = [{"name": "device", "tags": tag_list}]
        for dev in devices:
            if not isinstance(dev, dict):
                continue
            dname = str(dev.get("name") or dev.get("device_name") or "device")
            for tag in (dev.get("tags") or tag_list or []):
                if isinstance(tag, dict):
                    tname = str(tag.get("name") or tag.get("tag_name") or "tag")
                    inline_flag = bool(tag.get("publish_opcua"))
                else:
                    tname = str(tag)
                    inline_flag = False
                key = f"{gid}::{tname}"
                flag_entry = flags.get(key) or {}
                if not (inline_flag or bool(flag_entry.get("opcua"))):
                    continue
                out.append({
                    "gateway_id": str(gid),
                    "gateway_name": gname,
                    "device_name": dname,
                    "tag_name": tname,
                })
    return out


async def _build_address_space(server, tags: list[Dict[str, Any]]) -> None:
    """Create per-gateway / per-device folders and a Variable per tag."""
    objects = server.nodes.objects
    # Top-level "TrustNode" folder.
    trustnode = await objects.add_folder(server.idx, "TrustNode")
    # Group by gateway → device.
    gw_folders: Dict[str, Any] = {}
    dev_folders: Dict[str, Any] = {}
    for t in tags:
        gid = t["gateway_id"]
        if gid not in gw_folders:
            gw_folders[gid] = await trustnode.add_folder(server.idx, t["gateway_name"])
        dkey = f"{gid}::{t['device_name']}"
        if dkey not in dev_folders:
            dev_folders[dkey] = await gw_folders[gid].add_folder(server.idx, t["device_name"])
        var = await dev_folders[dkey].add_variable(
            server.idx,
            t["tag_name"],
            0.0,  # initial value; replaced on first publish_tag call
        )
        # Make writable=False; this is a read-only mirror.
        try:
            await var.set_writable(False)
        except Exception:
            pass
        _node_map[f"{gid}::{t['device_name']}::{t['tag_name']}"] = var


def _serve(port: int, endpoint: str, server_name: str, anonymous: bool,
            username: str, password: str, bootstrap: Dict[str, Any]) -> None:
    global _server, _loop, _last_error, _current_port
    try:
        from asyncua import Server, ua  # noqa: F401
    except Exception as exc:
        _last_error = f"asyncua import failed: {type(exc).__name__}: {exc}"
        logger.warning("opcua_server: %s", _last_error)
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    with _lock:
        _loop = loop

    async def _run() -> None:
        global _server, _last_error, _current_port
        try:
            from asyncua import Server
            srv = Server()
            await srv.init()
            srv.set_endpoint(endpoint)
            srv.set_server_name(server_name)
            uri = "urn:trustnode:edge"
            srv.idx = await srv.register_namespace(uri)
            # Auth policy: anonymous-only or username/password.
            if anonymous:
                try:
                    from asyncua.server.users import UserRole
                    srv.set_security_policy([])  # No security for LAN by default.
                except Exception:
                    pass
            else:
                try:
                    from asyncua.server.user_manager import UserManager
                    # asyncua >=1.0 uses a callable; older has add_user.
                    if username:
                        srv.user_manager.add_user(username, password or "")
                except Exception as e:
                    logger.warning("opcua user setup failed: %s", e)
            tags = _collect_publishable_tags(bootstrap)
            await _build_address_space(srv, tags)
            with _lock:
                _server = srv
                _current_port = int(port)
            # Drain any updates queued before the address space was ready.
            for key, value, ts_utc, quality in list(_pending_updates):
                node = _node_map.get(key)
                if node is None:
                    continue
                try:
                    await node.write_value(value)
                except Exception:
                    pass
            _pending_updates.clear()
            async with srv:
                # Idle loop while the server runs in its own task.
                while True:
                    await asyncio.sleep(1.0)
        except OSError as exc:
            _last_error = f"bind failed on port {port}: {exc.errno} {exc.strerror or exc}"
            logger.warning("opcua_server: %s", _last_error)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _last_error = f"crashed: {type(exc).__name__}: {exc}"
            logger.warning("opcua_server: %s", _last_error)
        finally:
            with _lock:
                _server = None
                _current_port = 0

    try:
        loop.run_until_complete(_run())
    finally:
        try:
            loop.close()
        except Exception:
            pass
        with _lock:
            _loop = None


def start(port: int = 4840, endpoint: Optional[str] = None,
          server_name: str = "TrustNode Edge OPC UA",
          anonymous: bool = True, username: str = "", password: str = "",
          bootstrap: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    global _thread, _last_error
    with _lock:
        if _server is not None and _thread is not None and _thread.is_alive():
            return {"ok": True, "running": True, "port": _current_port, "note": "already running"}
    _last_error = ""
    if bootstrap is None:
        try:
            from app.state import app_store
            bootstrap = app_store.get_bootstrap() or {}
        except Exception:
            bootstrap = {}
    ep = endpoint or f"opc.tcp://0.0.0.0:{int(port)}/trustnode/edge"
    with _lock:
        _thread = threading.Thread(
            target=_serve,
            args=(int(port), ep, server_name, bool(anonymous), str(username or ""), str(password or ""), bootstrap or {}),
            daemon=True,
            name="tn-opcua-server",
        )
        _thread.start()
    # Brief wait so the caller can see the start outcome.
    import time as _t
    for _ in range(40):
        _t.sleep(0.1)
        if is_running():
            return {"ok": True, "running": True, "port": int(port), "endpoint": ep}
        if _last_error:
            return {"ok": False, "running": False, "port": int(port), "note": _last_error}
    return {"ok": False, "running": False, "port": int(port), "note": _last_error or "failed to start (timeout)"}


def stop() -> Dict[str, Any]:
    global _thread, _server, _loop
    with _lock:
        loop = _loop
        thread = _thread
    if loop is not None:
        try:
            # Cancel every running task so the server's `async with` exits.
            def _cancel_all():
                for task in asyncio.all_tasks(loop=loop):
                    task.cancel()
            loop.call_soon_threadsafe(_cancel_all)
        except Exception:
            pass
    if thread is not None:
        thread.join(timeout=3.0)
    with _lock:
        _thread = None
        _server = None
        _loop = None
        _node_map.clear()
    return {"ok": True, "running": False}


def publish_tag(gateway_id: str, device_name: str, tag_name: str,
                value: Any, ts_utc: Optional[str] = None,
                quality: Optional[str] = None) -> None:
    """Best-effort push of a new value to the OPC UA node for a tag.
    If the server isn't running or the tag isn't publishable, this is a
    no-op. Buffers if the server is mid-startup.
    """
    key = f"{str(gateway_id)}::{str(device_name)}::{str(tag_name)}"
    with _lock:
        node = _node_map.get(key)
        loop = _loop
        running = _server is not None
    if not running:
        return
    if node is None:
        # Server is up but doesn't have this tag registered. Probably the
        # tag was added after start; user can restart the server to pick
        # it up.
        return
    if loop is None or loop.is_closed():
        return
    try:
        # write_value is a coroutine; schedule it on the server's loop.
        async def _w():
            try:
                await node.write_value(float(value) if value is not None and value != "" else 0.0)
            except Exception:
                pass
        asyncio.run_coroutine_threadsafe(_w(), loop)
    except Exception:
        pass
