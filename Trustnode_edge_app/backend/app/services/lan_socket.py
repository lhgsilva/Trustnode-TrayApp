"""Live LAN sockets (operator 2026-06-17; dual-stack HTTP + HTTPS 2026-08-21).

Lets the operator toggle remote access ON/OFF at runtime — no backend
restart, no app relaunch, no Customer-DB requirement.

How it works
------------
Uvicorn binds one socket per server instance. To turn remote access on we
don't reconfigure the existing 127.0.0.1 server — we start ADDITIONAL
uvicorn servers in background threads that serve the SAME FastAPI app:

  * HTTP  on <bind_host>:8088 (fallback 8089…8092) — works on any network
    with zero friction (owner decision 2026-08-21: HTTPS offered, never
    forced).
  * HTTPS on <bind_host>:8443 (fallback 8444…8447) — per-install self-signed
    certificate from `lan_tls` (or an enterprise cert/key dropped in
    <data>/lan_tls/custom.crt|key). Recommended; "HTTPS only" per site.

Both share the running app's state. Turning remote access off shuts both
down. The local-only server keeps serving the desktop UI throughout.

CRITICAL (2026-07-25): these secondary servers share the main server's
FastAPI app. With lifespan on (default) starting one re-fired EVERY
@app.on_event("startup") handler. The main server owns the lifespan; these
must never touch it — `lifespan="off"`.

Settings (app_settings): lan_sharing_enabled, lan_http_enabled (default True),
lan_https_only (default False), lan_bind_host (default "0.0.0.0").

Public API: start(primary_port, settings) / stop() / is_running() /
current_port() / current_https_port() / bind_host() / sync_with_settings().
"""
from __future__ import annotations

import asyncio
import logging
import socket
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_lan_lock = threading.Lock()
# scheme -> {"server": uvicorn.Server, "thread": Thread, "loop": loop, "port": int}
_listeners: Dict[str, Dict[str, Any]] = {}
_lan_last_error: str = ""
_bind_host: str = "0.0.0.0"

# The primary backend owns 127.0.0.1:8000 already; binding 0.0.0.0:8000 would
# overlap and the OS rejects it. Start at 8088 and walk the next handful so
# collisions with a co-tenant resolve themselves.
LAN_PORT_CANDIDATES = (8088, 8089, 8090, 8091, 8092)
LAN_HTTPS_PORT_CANDIDATES = (8443, 8444, 8445, 8446, 8447)


def _alive(entry: Optional[Dict[str, Any]]) -> bool:
    return bool(entry and entry.get("server") is not None and entry.get("thread") is not None and entry["thread"].is_alive())


def is_running() -> bool:
    with _lan_lock:
        return any(_alive(e) for e in _listeners.values())


def last_error() -> str:
    return _lan_last_error


def current_port() -> int:
    """The HTTP LAN port if running, else 0 (legacy name kept for callers)."""
    with _lan_lock:
        e = _listeners.get("http")
        return int(e["port"]) if _alive(e) else 0


def current_https_port() -> int:
    with _lan_lock:
        e = _listeners.get("https")
        return int(e["port"]) if _alive(e) else 0


def bind_host() -> str:
    return _bind_host


def _is_port_free(port: int, host: str = "0.0.0.0") -> bool:
    """Probe <host>:<port> without keeping the socket open."""
    import socket
    sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sk.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        try:
            sk.close()
        except Exception:
            pass


def _serve_in_thread(scheme: str, host: str, port: int, ssl_cert: str = "", ssl_key: str = "") -> None:
    """Run one uvicorn.Server on its own event loop until should_exit flips."""
    global _lan_last_error
    try:
        import uvicorn  # late import: only loaded if remote access ever turns on.
        from app.main import app as fastapi_app
    except Exception as exc:
        _lan_last_error = f"import failed: {type(exc).__name__}: {exc}"
        logger.warning("LAN server %s", _lan_last_error)
        return
    kwargs: Dict[str, Any] = dict(
        app=fastapi_app, host=host, port=int(port), log_level="warning", access_log=False,
        lifespan="off",  # the primary server owns the lifespan — never re-fire startup here
    )
    if scheme == "https":
        kwargs.update(ssl_certfile=ssl_cert, ssl_keyfile=ssl_key)
    config = uvicorn.Config(**kwargs)
    server = uvicorn.Server(config)
    # Bind the socket OURSELVES and hand it to uvicorn (2026-08-21). uvicorn's
    # graceful shutdown waits for open connections, so a plain should_exit could
    # leave the port bound long after stop() returned — the next start() then
    # picked the following candidate port and both listeners answered. Owning
    # the socket lets stop() close it and free the port deterministically.
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((host, int(port)))
        sock.listen(2048)
        sock.setblocking(False)
    except OSError as exc:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        _lan_last_error = f"{scheme} bind failed on port {port}: {exc.errno} {exc.strerror or exc}"
        logger.warning("LAN server %s", _lan_last_error)
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    with _lan_lock:
        entry = _listeners.setdefault(scheme, {})
        entry.update(server=server, loop=loop, port=int(port), socket=sock)
    try:
        loop.run_until_complete(server.serve(sockets=[sock]))
    except OSError as exc:
        _lan_last_error = f"{scheme} bind failed on port {port}: {exc.errno} {exc.strerror or exc}"
        logger.warning("LAN server %s", _lan_last_error)
    except Exception as exc:
        _lan_last_error = f"{scheme} crashed: {type(exc).__name__}: {exc}"
        logger.warning("LAN server %s", _lan_last_error)
    finally:
        try:
            loop.close()
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
        with _lan_lock:
            entry = _listeners.get(scheme) or {}
            entry.update(server=None, loop=None, socket=None)


def _pick_port(candidates, primary_port: int, host: str) -> tuple[int, list]:
    tried = []
    for cand in candidates:
        if int(cand) == int(primary_port):
            tried.append(f"{cand}(skip: primary)")
            continue
        tried.append(str(cand))
        if _is_port_free(cand, host):
            return int(cand), tried
    return 0, tried


def _start_listener(scheme: str, host: str, port: int, ssl_cert: str = "", ssl_key: str = "") -> bool:
    import time as _t
    with _lan_lock:
        if _alive(_listeners.get(scheme)):
            return True
        th = threading.Thread(target=_serve_in_thread, args=(scheme, host, port, ssl_cert, ssl_key),
                              daemon=True, name=f"tn-lan-{scheme}")
        _listeners[scheme] = {"server": None, "thread": th, "loop": None,
                              "port": int(port), "socket": None}
        th.start()
    for _ in range(40):
        _t.sleep(0.05)
        with _lan_lock:
            if _alive(_listeners.get(scheme)):
                return True
    return False


def start(primary_port: int, app_settings: Optional[Dict[str, Any]] = None) -> dict:
    """Start the LAN listeners according to settings. Idempotent.
    Returns {ok, running, port, https_port, candidates_tried, note}."""
    global _lan_last_error, _bind_host
    s = dict(app_settings or {})
    http_enabled = bool(s.get("lan_http_enabled", True))
    https_only = bool(s.get("lan_https_only", False))
    host = str(s.get("lan_bind_host") or "0.0.0.0").strip() or "0.0.0.0"
    _bind_host = host
    _lan_last_error = ""
    tried_all: list = []
    notes: list = []

    # Operator 2026-06-18: self-heal the Windows Firewall rule BEFORE binding a
    # non-loopback address (program-scoped rule, private/domain profiles).
    try:
        from app.services import windows_firewall as _fw
        _fw.ensure_backend_rule()
    except Exception:
        pass

    http_port = 0
    if http_enabled and not https_only:
        http_port, tried = _pick_port(LAN_PORT_CANDIDATES, primary_port, host)
        tried_all += tried
        if http_port and not _start_listener("http", host, http_port):
            notes.append(_lan_last_error or f"http failed to bind {http_port}")
            http_port = 0
        elif not http_port:
            notes.append(f"no free HTTP port in {LAN_PORT_CANDIDATES}")

    https_port = 0
    try:
        from app.services import lan_tls
        tls = lan_tls.ensure_certificate()
    except Exception:
        tls = None
    if tls and tls.get("cert") and tls.get("key"):
        https_port, tried = _pick_port(LAN_HTTPS_PORT_CANDIDATES, primary_port, host)
        tried_all += [f"https:{t}" for t in tried]
        if https_port and not _start_listener("https", host, https_port, str(tls["cert"]), str(tls["key"])):
            notes.append(_lan_last_error or f"https failed to bind {https_port}")
            https_port = 0
        elif not https_port:
            notes.append(f"no free HTTPS port in {LAN_HTTPS_PORT_CANDIDATES}")
    else:
        notes.append("https unavailable (no certificate)")

    running = bool(http_port or https_port)
    if not running and not _lan_last_error:
        _lan_last_error = "; ".join(notes) or "failed to bind"
    return {
        "ok": running,
        "running": running,
        "port": http_port,
        "https_port": https_port,
        "candidates_tried": tried_all,
        "note": "; ".join(notes),
    }


def stop() -> dict:
    """Stop every LAN listener gracefully. Idempotent."""
    with _lan_lock:
        entries = {k: dict(v) for k, v in _listeners.items()}
    if not any(_alive(e) for e in entries.values()):
        with _lan_lock:
            _listeners.clear()
        return {"ok": True, "running": False, "note": "already stopped"}
    for scheme, e in entries.items():
        server, loop, thread = e.get("server"), e.get("loop"), e.get("thread")
        sock, port = e.get("socket"), int(e.get("port") or 0)
        if server is not None:
            server.should_exit = True
            # Do not wait for in-flight connections: a browser holding a
            # keep-alive socket must never keep the old port bound.
            server.force_exit = True
        if loop is not None:
            try:
                loop.call_soon_threadsafe(lambda: None)  # nudge the loop
            except Exception:
                pass
        if thread is not None:
            thread.join(timeout=5.0)
        if thread is not None and thread.is_alive():
            # Last resort: release the port ourselves so the next start() binds
            # the SAME port instead of climbing the candidate ladder.
            try:
                if sock is not None:
                    sock.close()
                logger.warning(
                    "LAN %s listener did not exit in 5s — closed its socket to "
                    "release port %d", scheme, port,
                )
            except Exception as exc:
                logger.warning("LAN %s listener socket close failed on port %d: %s",
                               scheme, port, exc)
    with _lan_lock:
        _listeners.clear()
    return {"ok": True, "running": False}


def sync_with_settings(enabled: bool, primary_port: int, app_settings: Optional[Dict[str, Any]] = None) -> dict:
    """Start/stop according to the operator's desired state. When `enabled`
    and already running with different HTTP/HTTPS options, restart."""
    if not bool(enabled):
        return stop()
    s = dict(app_settings or {})
    want_http = bool(s.get("lan_http_enabled", True)) and not bool(s.get("lan_https_only", False))
    if is_running():
        have_http = current_port() > 0
        have_https = current_https_port() > 0
        host_changed = (str(s.get("lan_bind_host") or "0.0.0.0") != _bind_host)
        if have_http != want_http or host_changed or (not have_https and _https_possible()):
            stop()
        else:
            return {"ok": True, "running": True, "port": current_port(), "https_port": current_https_port(), "note": "already running"}
    return start(int(primary_port), s)


def _https_possible() -> bool:
    try:
        from app.services import lan_tls
        d = lan_tls.describe()
        return bool(d and d.get("cert"))
    except Exception:
        return False
