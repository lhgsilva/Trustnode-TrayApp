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
#: Result of the last firewall self-heal, for /lan-sharing/status.
_firewall_state: Dict[str, Any] = {}
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
    async def _serve() -> None:
        # 2026-10-27: raise THIS loop's thread limiter before serving.
        #
        # main.py raises the anyio default limiter from 40 to 200 in
        # startup_event - but these servers run with lifespan="off" in their
        # OWN event loops, and current_default_thread_limiter() is a RunVar:
        # per loop, not per process. So every LAN listener was serving with
        # 40 tokens while the UI it serves runs ~22 pollers, and FastAPI puts
        # every sync route on that same pool.
        #
        # Reported from a LAN browser, and this is the whole of it:
        #   "Database connections could not be saved: Timed out after 20s:
        #    https://<host>:8443/api/app-store/domain"
        # The write never got a thread. The identical call on loopback, where
        # the limiter IS 200, returns immediately.
        # Raised to 64, NOT to the main loop's 200.
        #
        # 2026-10-27, measured: setting these loops to 200 cost ~3 points of
        # WebSocket delivery (97.0% -> 93.5%/94.5%, below the 95% gate) while
        # the database still committed 100.3% of stamps. Collection and the
        # historian were fine; the LIVE stream was losing events, because two
        # extra loops each allowed 200 concurrent threads to compete with the
        # collector for the same store.
        #
        # The main loop needs 200 because collection runs on it. These serve
        # HTTP only: they need enough that a config save is never starved -
        # the 40-token default is what produced the 20s timeout on
        # /api/app-store/domain - and no more than that.
        try:
            import anyio
            limiter = anyio.to_thread.current_default_thread_limiter()
            if limiter.total_tokens < 64:
                limiter.total_tokens = 64
                logger.info("LAN %s: thread limiter raised to %d",
                            scheme, limiter.total_tokens)
        except Exception as exc:                     # noqa: BLE001
            logger.warning("LAN %s: could not raise thread limiter: %r",
                           scheme, exc)
        await server.serve(sockets=[sock])

    try:
        loop.run_until_complete(_serve())
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


#: Remote access is OFF until the operator turns it on.
#:
#: 2026-10-27 this was flipped to True, asked for as "ideally the app should
#: have 1 lan access as default for setup of the software remotely using the
#: admin users login with full access". Every call site read
#: bool(s.get("lan_sharing_enabled")), so an absent key had meant OFF.
#:
#: 2026-10-29 it is back to OFF, from a customer machine. `lan_sharing_enabled`
#: is absent on every install that predates the flip - measured here, not one
#: of the 12 settings documents in the live store carries it - so the change
#: did not only affect new edges. It switched a dormant subsystem ON for every
#: existing customer: two listeners, a certificate, and a firewall rule the
#: machine cannot create without administrator rights ("firewall rule not in
#: place: The requested operation requires elevation"). None of it was asked
#: for by the site, and a customer must not be handed that on an upgrade.
#:
#: The capability is unchanged. An operator who switches remote access on gets
#: exactly what the 10-27 request described; /enable and /disable write an
#: explicit True or False and are respected either way. Only the meaning of
#: the ABSENT key is restored to what it was when the app worked.
LAN_SHARING_DEFAULT = False


def sharing_enabled(settings: Optional[Dict[str, Any]]) -> bool:
    """One reading of the setting, so the four call sites cannot disagree."""
    s = settings or {}
    value = s.get("lan_sharing_enabled", None)
    if value is None:
        return LAN_SHARING_DEFAULT
    return bool(value)


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
    # 2026-10-27: the result used to be discarded. On the portable build
    # this fails EVERY time with "The requested operation requires elevation
    # (Run as administrator)", so the app listened on 0.0.0.0 while Windows
    # dropped every packet from every other computer - and nothing recorded
    # why. Keep it for the status to report, and for the Remote Access page
    # to offer the one-click elevated fix.
    global _firewall_state
    try:
        from app.services import windows_firewall as _fw
        _firewall_state = _fw.ensure_backend_rule()
        if not _firewall_state.get("ok"):
            logger.warning("LAN: firewall rule not in place: %s",
                           _firewall_state.get("reason"))
    except Exception as exc:                        # noqa: BLE001
        _firewall_state = {"ok": False, "reason": type(exc).__name__}

    # The certificate must name the addresses this machine answers on TODAY.
    # Written once at generation, it goes stale the moment a laptop joins
    # WiFi - and a browser on that network then refuses the connection,
    # which looks exactly like the port being blocked.
    try:
        from app.services import lan_tls as _tls
        _cert_state = _tls.ensure_certificate_covers_lan()
        if _cert_state.get("regenerated"):
            logger.info("LAN TLS: certificate refreshed (%s)",
                        _cert_state.get("reason"))
    except Exception as exc:                        # noqa: BLE001
        logger.warning("LAN TLS: certificate check failed: %r", exc)

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
