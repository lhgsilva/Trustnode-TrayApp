"""Live LAN socket (operator 2026-06-17).

Lets the operator toggle LAN sharing ON/OFF at runtime — no backend
restart, no app relaunch, no Customer-DB requirement.

How it works
------------
Uvicorn binds one socket per server instance. To turn LAN sharing on
we don't reconfigure the existing 127.0.0.1 server — we **start a
second uvicorn server** in a background thread that serves the SAME
FastAPI app on 0.0.0.0:<port>. Both servers share the running app's
state (DB pool, app_store, power_manager). Turning sharing off
gracefully shuts that second server down. The local-only server keeps
serving the desktop UI throughout, so the operator never sees a blip.

Why a second server instead of reconfiguring the first
------------------------------------------------------
Uvicorn doesn't expose a "rebind" API. Killing + recreating the
main server requires tearing down the desktop's HTTP session and
re-issuing the auth token. Running a second instance is ~30 LOC and
costs one extra socket + one extra event-loop thread.

Public API:

  * start() — idempotent. Starts the LAN server if not already running.
  * stop()  — idempotent. Shuts the LAN server down.
  * is_running() — bool.
  * sync_with_settings() — read app_settings.lan_sharing_enabled and
    flip start/stop accordingly. Called by the LAN-sharing router so
    the toggle takes effect immediately.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_lan_lock = threading.Lock()
_lan_server = None        # uvicorn.Server
_lan_thread: Optional[threading.Thread] = None
_lan_loop: Optional[asyncio.AbstractEventLoop] = None
_lan_port: int = 0
_lan_last_error: str = ""


# Candidate ports for the LAN socket. The primary backend owns
# 127.0.0.1:8000 already; binding 0.0.0.0:8000 would overlap and the
# OS rejects it. Start at 8088 and walk the next handful so port
# collisions with a randomly chosen co-tenant resolve themselves.
LAN_PORT_CANDIDATES = (8088, 8089, 8090, 8091, 8092)


def is_running() -> bool:
    with _lan_lock:
        return _lan_server is not None and _lan_thread is not None and _lan_thread.is_alive()


def last_error() -> str:
    return _lan_last_error


def _is_port_free(port: int) -> bool:
    """Probe 0.0.0.0:<port> without actually keeping the socket open.
    Used to pick the first available port from LAN_PORT_CANDIDATES so
    a collision with another process doesn't trigger a hard fail.
    """
    import socket
    sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sk.bind(("0.0.0.0", int(port)))
        return True
    except OSError:
        return False
    finally:
        try: sk.close()
        except Exception: pass


def _serve_in_thread(port: int) -> None:
    """Run a uvicorn.Server on 0.0.0.0:<port> on its own event loop
    until ``server.should_exit`` flips to True.
    """
    global _lan_server, _lan_loop, _lan_last_error
    try:
        import uvicorn  # late import: only loaded if LAN ever turns on.
        from app.main import app as fastapi_app
    except Exception as exc:
        _lan_last_error = f"import failed: {type(exc).__name__}: {exc}"
        logger.warning("LAN server %s", _lan_last_error)
        return
    config = uvicorn.Config(
        app=fastapi_app,
        host="0.0.0.0",
        port=int(port),
        log_level="warning",
        access_log=False,
        # 2026-07-25: CRITICAL — this second server shares the main server's
        # FastAPI app. With lifespan on (default), starting it re-fired EVERY
        # @app.on_event("startup") handler: double auto-resume (gateway
        # start-stop-start at boot), double watchdog/scheduler arming, doubled
        # boot-time DB contention — and toggling LAN OFF would run the
        # SHUTDOWN handlers while the app was still serving. The main server
        # owns the lifespan; this one must never touch it.
        lifespan="off",
    )
    server = uvicorn.Server(config)
    with _lan_lock:
        _lan_server = server
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    with _lan_lock:
        _lan_loop = loop
    try:
        loop.run_until_complete(server.serve())
    except OSError as exc:
        # Most common case: port already in use, or no permission to
        # bind on Windows. Record so the route layer can surface it.
        _lan_last_error = f"bind failed on port {port}: {exc.errno} {exc.strerror or exc}"
        logger.warning("LAN server %s", _lan_last_error)
    except Exception as exc:
        _lan_last_error = f"crashed: {type(exc).__name__}: {exc}"
        logger.warning("LAN server %s", _lan_last_error)
    finally:
        try:
            loop.close()
        except Exception:
            pass
        with _lan_lock:
            _lan_loop = None
            _lan_server = None


def start(primary_port: int) -> dict:
    """Start the LAN server if not already running.

    Picks the first free port from ``LAN_PORT_CANDIDATES``. The
    primary backend always owns 127.0.0.1:<primary_port> — binding
    0.0.0.0:<primary_port> would overlap and the OS rejects it. The
    fallback chain (8088 → 8089 → …) sidesteps that AND survives
    co-tenants like a developer running another HTTP server.

    Returns:
        {ok, running, port, candidates_tried, note}
    """
    global _lan_thread, _lan_port, _lan_last_error
    with _lan_lock:
        if _lan_server is not None and _lan_thread is not None and _lan_thread.is_alive():
            return {"ok": True, "running": True, "port": _lan_port, "note": "already running"}
    _lan_last_error = ""
    # Pre-flight: pick the first free port. If nothing in our candidate
    # list is free, surface that error verbatim rather than letting
    # uvicorn crash silently.
    tried = []
    chosen = 0
    for cand in LAN_PORT_CANDIDATES:
        if int(cand) == int(primary_port):
            tried.append(f"{cand}(skip: primary)")
            continue
        tried.append(str(cand))
        if _is_port_free(cand):
            chosen = int(cand)
            break
    if chosen == 0:
        _lan_last_error = f"no free port in {LAN_PORT_CANDIDATES}"
        return {
            "ok": False,
            "running": False,
            "port": 0,
            "candidates_tried": tried,
            "note": _lan_last_error,
        }
    # Operator 2026-06-18: self-heal the Windows Firewall rule BEFORE we
    # bind 0.0.0.0. If the installer's rule is missing (portable EXE,
    # broken old-installer netsh, manual delete), this adds it silently
    # so the user never sees the Defender prompt. Skipped on non-Windows
    # and cached per-process so repeated toggles don't re-probe netsh.
    try:
        from app.services import windows_firewall as _fw
        _fw.ensure_backend_rule()
    except Exception:
        pass
    with _lan_lock:
        _lan_port = chosen
        _lan_thread = threading.Thread(
            target=_serve_in_thread,
            args=(chosen,),
            daemon=True,
            name="tn-lan-server",
        )
        _lan_thread.start()
    # Give the server a moment to bind.
    import time as _t
    for _ in range(30):
        _t.sleep(0.05)
        if is_running():
            return {
                "ok": True,
                "running": True,
                "port": chosen,
                "candidates_tried": tried,
            }
    return {
        "ok": False,
        "running": False,
        "port": chosen,
        "candidates_tried": tried,
        "note": _lan_last_error or "failed to bind",
    }


def stop() -> dict:
    """Stop the LAN server gracefully. Idempotent."""
    global _lan_thread, _lan_server, _lan_loop
    with _lan_lock:
        if _lan_server is None or _lan_thread is None:
            return {"ok": True, "running": False, "note": "already stopped"}
        server = _lan_server
        loop = _lan_loop
        thread = _lan_thread
    # Tell uvicorn to exit. server.should_exit is read on the next
    # tick of its main loop, which lets in-flight requests finish.
    if server is not None:
        server.should_exit = True
    if loop is not None:
        try:
            loop.call_soon_threadsafe(lambda: None)  # nudge the loop
        except Exception:
            pass
    if thread is not None:
        thread.join(timeout=3.0)
    with _lan_lock:
        _lan_thread = None
        _lan_server = None
        _lan_loop = None
    return {"ok": True, "running": False}


def sync_with_settings(enabled: bool, primary_port: int) -> dict:
    """Read the operator's desired state and start/stop accordingly.

    `primary_port` is the port the primary 127.0.0.1 server owns;
    start() avoids it when picking the LAN-side port.
    """
    if bool(enabled):
        return start(int(primary_port))
    return stop()


def current_port() -> int:
    """The LAN-server port if running, else 0."""
    with _lan_lock:
        return _lan_port if (_lan_server is not None and _lan_thread is not None and _lan_thread.is_alive()) else 0
