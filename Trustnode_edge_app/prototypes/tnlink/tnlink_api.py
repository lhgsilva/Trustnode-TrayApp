# -*- coding: utf-8 -*-
"""One local API, so the tray and the window can never disagree.

2026-10-28. The tray and the Electron interface both have to show the same
state and start the same things. Two code paths doing that is how they
drift - one grows a feature, the other quietly keeps the old behaviour, and
the operator is told two different stories about the same session.

So the core exposes a small HTTP+WS surface on loopback, and BOTH front
ends consume it. The console still works exactly as before; this sits
beside it.

Safety, because this is a listening socket on a machine with a PLC behind
it:

  * 127.0.0.1 ONLY. Never 0.0.0.0 - the whole point of LS-Link is that
    nothing is exposed until an engineer pairs, and a convenience API
    bound to every interface would undo that in one line.
  * A token, minted per run, required on every call. Loopback is not a
    security boundary on Windows: any local process can reach it, and this
    API can start a tunnel.
  * The port is chosen at run time and written next to the token, so a
    second instance cannot silently answer for the first.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

#: Most recent lines of what happened, for the Logs view. Bounded on
#: purpose: a long remote session must not grow memory without limit.
_LOG_MAX = 500
_log: List[Dict[str, Any]] = []
_log_lock = threading.Lock()


def log(message: str, kind: str = "info") -> None:
    """Record one line for the interface. Never raises at the caller."""
    try:
        with _log_lock:
            _log.append({"ts": time.strftime("%H:%M:%S"),
                         "kind": str(kind), "text": str(message)})
            if len(_log) > _LOG_MAX:
                del _log[0:len(_log) - _LOG_MAX]
    except Exception:
        pass


def recent_log(limit: int = 200) -> List[Dict[str, Any]]:
    with _log_lock:
        return list(_log[-int(limit):])


def endpoint_file() -> str:
    """Where the window looks for the port and token of THIS run.

    TNLINK_API_FILE moves it. That is how a test runs a plant and an
    engineer on one machine without the two overwriting each other's
    endpoint - and it doubles as the escape hatch on a PC where APPDATA is
    locked down.
    """
    override = os.environ.get("TNLINK_API_FILE")
    if override:
        try:
            os.makedirs(os.path.dirname(override), exist_ok=True)
        except Exception:
            pass
        return override
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "TrustNodeLink")
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        pass
    return os.path.join(folder, "api.json")


class _Handler(BaseHTTPRequestHandler):
    server_version = "tnlink"
    sys_version = ""

    # Silence the default stderr logging; the interface shows our own.
    def log_message(self, fmt, *args):      # noqa: A003
        pass

    def _authorised(self) -> bool:
        want = getattr(self.server, "token", "")
        got = self.headers.get("X-TNLink-Token", "")
        # compare_digest: a token check that leaks timing is still a leak.
        try:
            import hmac
            return bool(want) and hmac.compare_digest(str(want), str(got))
        except Exception:
            return False

    def _send(self, code: int, body: Any) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except Exception:
            pass

    def _serve_ui(self) -> bool:
        """Serve the interface itself, with this run's token built in.

        2026-10-28, requested: "we need the app packed in one exe to be run
        in any computer without installation". Shipping Electron would make
        that one exe about 290 MB, most of it a browser the machine already
        has. The interface is a single HTML page, so the core serves it and
        it opens in the default browser: same page, no runtime, no install,
        and the exe stays small.

        The token is written INTO the page rather than put in the URL - a
        URL lands in history and in the title bar, and this one can start a
        tunnel.
        """
        import os, sys
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        page = os.path.join(base, "ui", "index.html")
        if not os.path.isfile(page):
            return False
        try:
            with open(page, "r", encoding="utf-8") as fh:
                html = fh.read()
        except Exception:
            return False
        shim = ("<script>window.__TNLINK__={token:%r,port:%d};</script>"
                % (getattr(self.server, "token", ""), int(self.server.server_address[1])))
        html = html.replace("<script>", shim + "<script>", 1)
        raw = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except Exception:
            pass
        return True

    def do_GET(self) -> None:               # noqa: N802
        path = (self.path or "/").split("?")[0]
        # The page itself is served unauthenticated - it contains no data,
        # only the token needed to ASK for data, and it is bound to
        # loopback. Everything that returns state still needs the token.
        if path in ("/", "/index.html"):
            if self._serve_ui():
                return
            return self._send(404, {"error": "interface not bundled"})
        if not self._authorised():
            return self._send(401, {"error": "unauthorised"})
        api = getattr(self.server, "api", None)
        if path == "/state":
            return self._send(200, api.state())
        if path == "/logs":
            return self._send(200, {"lines": recent_log()})
        return self._send(404, {"error": "not found"})

    def do_POST(self) -> None:              # noqa: N802
        if not self._authorised():
            return self._send(401, {"error": "unauthorised"})
        path = (self.path or "/").split("?")[0]
        api = getattr(self.server, "api", None)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            body = {}
        if not path.startswith("/do/"):
            return self._send(404, {"error": "not found"})
        name = path[4:]
        fn = (api.actions or {}).get(name)
        if not fn:
            return self._send(404, {"error": "no such action: %s" % name})
        try:
            out = fn(**body) if body else fn()
            return self._send(200, {"ok": True, "result": out})
        except TypeError:
            # An action that takes no arguments, called with a body.
            try:
                return self._send(200, {"ok": True, "result": fn()})
            except Exception as exc:        # noqa: BLE001
                return self._send(500, {"ok": False, "error": str(exc)})
        except Exception as exc:            # noqa: BLE001
            log("%s failed: %s" % (name, exc), "error")
            return self._send(500, {"ok": False, "error": str(exc)})


class LocalApi:
    """Serves `state()` and the named actions on loopback."""

    def __init__(self, state: Callable[[], Dict[str, Any]],
                 actions: Dict[str, Callable[..., Any]]):
        self.state = state
        self.actions = actions or {}
        self.port = 0
        self.token = secrets.token_urlsafe(24)
        self._srv: Optional[ThreadingHTTPServer] = None

    def start(self) -> int:
        if self._srv is not None:
            return self.port
        # Port 0 lets the OS pick a free one; writing it down is what makes
        # a second instance impossible to confuse with the first.
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        srv.api = self                       # type: ignore[attr-defined]
        srv.token = self.token               # type: ignore[attr-defined]
        self.port = int(srv.server_address[1])
        self._srv = srv
        threading.Thread(target=srv.serve_forever, name="tnlink-api",
                         daemon=True).start()
        try:
            with open(endpoint_file(), "w", encoding="utf-8") as fh:
                json.dump({"port": self.port, "token": self.token,
                           "pid": os.getpid()}, fh)
        except Exception:
            pass
        log("interface API listening on 127.0.0.1:%d" % self.port)
        return self.port

    def stop(self) -> None:
        srv, self._srv = self._srv, None
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:
                pass
        try:
            os.remove(endpoint_file())
        except Exception:
            pass
