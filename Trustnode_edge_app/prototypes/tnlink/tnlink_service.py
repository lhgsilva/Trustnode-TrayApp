# -*- coding: utf-8 -*-
"""TN-Link as a service: the tray drives the real app, not a copy of it.

2026-10-28. `tnlink_tray.py` draws the menus and knows nothing about the
protocol; `tnlink.py` owns connecting, sharing and tearing down. This file
is the wiring between them, and it exists so there is exactly ONE
implementation of every action: the tray calls the same `cmd_*` the console
calls, so a fix to either is a fix to both.

Run it the way the operator will:

    TrustNode-Link.exe --tray          (role asked once, then remembered)
    TrustNode-Link.exe --tray --role server

What the tray can do, and what it deliberately cannot:

  * Everything repeated daily is on the menu - the code, the relay, start
    and stop sharing, reach a device, restart, quit.
  * Anything that needs a conversation (which device, which subnet, a
    confirmation) opens the console window instead of inventing a second
    UI in the notification area. A tray menu is a bad place to ask a
    question, and two UIs for one action is how they drift apart.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Dict, Optional


def _clip(text: str) -> bool:
    """Put text on the clipboard. Best effort - never raises at the user."""
    try:
        import subprocess
        p = subprocess.Popen(["clip"], stdin=subprocess.PIPE,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        p.communicate(str(text or "").encode("utf-16le"))
        return p.returncode == 0
    except Exception:
        return False


def _saved_connections() -> list:
    """The address book, ready for the screen.

    2026-10-31. Each row carries the label the UI should print, so the
    name -> host -> id fallback lives in ONE place. Two copies of that rule
    is how a list ends up showing an id in the tray and a name in the window.
    """
    try:
        import tnlink as _t
        out = []
        for r in _t.connections():
            out.append({
                "unit": str(r.get("unit") or ""),
                "label": _t.connection_label(r),
                "name": str(r.get("name") or ""),
                "host": str(r.get("host") or ""),
                "address": str(r.get("address") or ""),
                "favorite": bool(r.get("favorite")),
                # Can we reconnect without asking the operator for a code?
                "can_connect": bool(r.get("next_code")),
                "uses": int(r.get("uses") or 0),
                "last_utc": float(r.get("last_utc") or 0),
            })
        return out
    except Exception:
        return []


class LinkService:
    """Holds the running Server or Client and exposes it to the tray."""

    def __init__(self, role: str, args: Any):
        self.role = role
        self.args = args
        self.app: Any = None               # tnlink.Server or tnlink.Client
        self.error: str = ""
        self.started_at: float = 0.0
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stopping = False
        self._paired = False
        self._tray = None

    # -- state the tray renders -----------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        app = self.app
        # Server has no `alive` flag - only Client does - so asking for one
        # reported every PLANT as "stopped" while it was serving happily.
        # The service knows whether its own thread is running; that is the
        # honest answer for both roles.
        running = bool(app) and bool(self._thread and self._thread.is_alive())
        peers = 0
        peer_list = []
        try:
            # The relay pairs one engineer with one plant; `sec` is set once
            # the handshake completes, so it IS the peer count.
            if running and getattr(app, "sec", None) is not None:
                peers = 1
                peer_list = [{
                    "id": "peer",
                    "name": "engineer" if self.role == "server" else "plant",
                    "since": time.strftime("%H:%M", time.localtime(self.started_at or time.time())),
                }]
        except Exception:
            pass
        unit = ""
        expires = ""
        shared = []
        try:
            import tnlink as _tn
            unit = _tn.unit_id()
            dl = float(getattr(app, "deadline", 0) or 0)
            if dl:
                left = int(max(0, dl - time.time()) // 60)
                expires = "%dh %02dm left" % (left // 60, left % 60)
            tun = getattr(app, "tunnel_info", None) or {}
            shared = [str(c) for c in (tun.get("subnets") or [])]
            for sid, info in (getattr(app, "streams", {}) or {}).items():
                shared.append(str(info))
        except Exception:
            pass
        devices = []
        try:
            for d in (getattr(app, "devices", None) or []):
                devices.append({
                    "ip": d.get("ip"),
                    "ports": d.get("ports") or [],
                    # The protocol, which is what the ports prove.
                    "kind": app.describe_device(d) if hasattr(app, "describe_device") else "device",
                    # What the device SAID it is, when it answered.
                    "what": d.get("what") or "",
                    "vendor": d.get("vendor") or "",
                    "product": d.get("product") or "",
                    "part": d.get("part") or "",
                    "revision": d.get("revision") or "",
                    "type": d.get("type") or "",
                })
        except Exception:
            devices = []
        # What the engineer can actually reach RIGHT NOW, and at which
        # address. 2026-10-28, requested: "after a device is selected it
        # should show the exact devices connected". Pressing Connect used
        # to print the answer into a console nobody sees, so the operator
        # had no way to know which address to type into RSLinx.
        reachable = []
        try:
            # Three ways in, and the Connected list was only showing two.
            # Option 1 routes the device at its REAL address over the
            # tunnel - no claim, no port forward - so after it had plainly
            # worked the list still said "nothing connected yet".
            for ip in (getattr(app, "routed", None) or []):
                reachable.append({"device": str(ip), "at": str(ip),
                                  "how": "its own address (routed)"})
            for ip in (getattr(app, "claimed", None) or []):
                reachable.append({"device": str(ip), "at": str(ip),
                                  "how": "its own address"})
            for m in (getattr(app, "maps", None) or []):
                remote = str(m.get("remote") or "")
                reachable.append({"device": remote.rsplit(":", 1)[0] or remote,
                                  "at": str(m.get("local") or ""),
                                  "how": "port %s" % (remote.rsplit(":", 1)[-1]
                                                      if ":" in remote else "?")})
        except Exception:
            reachable = []
        # 2026-10-29, reported: the engineer's window showed a
        # "Connection code" identical to the code typed into "Connect to a
        # plant", so the same string appeared twice as if it were both
        # ends. A code belongs to the PLANT. What an engineer needs to see
        # is its OWN unit id and WHICH plant it reached.
        peer = ""
        try:
            if self.role != "server":
                peer = str((getattr(app, "info", None) or {}).get("host") or "")
        except Exception:
            peer = ""
        return {
            "role": self.role,
            "devices": devices,
            "reachable": reachable,
            "peer": peer,
            # One exchange with the plant, measured. A PLC download is
            # tens of thousands of them, and this number multiplied by
            # that count IS the download time - so it belongs on screen
            # before someone starts one.
            "link_ms": round(float(getattr(app, "link_ms", 0) or 0), 1),
            "same_lan": list(getattr(app, "same_lan", None) or []),
            "unit_id": unit,
            # 2026-10-31, requested: "shows the recent connections and also
            # save them a favorite and rename the connection if needed".
            # Favourites first, then most recent; each row already carries the
            # label to show, so the UI does not re-derive that rule and drift.
            "connections": _saved_connections(),
            "expires": expires,
            "shared": shared,
            "running": running,
            "peers": peers,
            "peer_list": peer_list,
            "error": self.error,
            "relay": getattr(self.args, "relay", "") or "",
            "code": (getattr(app, "code", "")
                     or (getattr(self.args, "code", "") or ""))
                    if self.role == "server" else "",
            "recent": self._recent(),
        }

    # -- remembered plants ----------------------------------------------
    def _recent_path(self) -> str:
        import tnlink
        return os.path.join(os.path.dirname(tnlink._settings_path()), "recent.txt")

    def _recent(self) -> list:
        try:
            with open(self._recent_path(), "r", encoding="utf-8") as fh:
                return [l.strip() for l in fh if l.strip()][:8]
        except Exception:
            return []

    def remember(self, code: str) -> None:
        code = str(code or "").strip()
        if not code:
            return
        seen = [c for c in self._recent() if c != code]
        try:
            with open(self._recent_path(), "w", encoding="utf-8") as fh:
                fh.write("\n".join([code] + seen[:7]))
        except Exception:
            pass

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        """Run the app in its own thread so the tray stays responsive."""
        import tnlink
        with self._lock:
            if self._thread and self._thread.is_alive():
                return

            def _run():
                self._stopping = False
                self.error = ""
                self.started_at = time.time()
                a = self.args
                try:
                    # 2026-10-28, second correction. This passed the parsed
                    # argument namespace straight in - but the real
                    # signatures are
                    #     Server(code, allow, port, minutes, bind,
                    #            firewall=, relay=)
                    #     Client(host, port, code)
                    # so the tray raised TypeError the moment it started and
                    # the whole app looked dead. Built the way main() builds
                    # them, which is the only version that is known to work.
                    relay = "" if getattr(a, "direct", False) else                         str(getattr(a, "relay", "") or "").strip()
                    if self.role == "server":
                        code = str(getattr(a, "code", "") or "").strip()                             or tnlink.new_code()
                        a.code = code
                        self.app = tnlink.Server(
                            code, getattr(a, "allow", []) or [],
                            int(getattr(a, "port", 0) or tnlink.DEFAULT_PORT),
                            int(getattr(a, "minutes", 60) or 60),
                            str(getattr(a, "bind", "0.0.0.0") or "0.0.0.0"),
                            firewall=not getattr(a, "no_firewall", False),
                            relay=relay)
                        self.app.serve()
                    else:
                        code = str(getattr(a, "code", "") or "").strip()
                        if not code:
                            self.error = "no connection code yet"
                            self._notify("TN-Link",
                                         "Enter the plant's connection code first.")
                            return
                        # 2026-10-28: this passed the RELAY as `host` and
                        # called run(). Client dials the relay only when
                        # __init_relay__ has been called - otherwise
                        # connect() opens a raw TCP socket to that name on
                        # the LAN port, which no relay answers. So it never
                        # paired, and the interface sat on "waiting to
                        # pair" with nothing to show.
                        host = str(getattr(a, "server", "") or "").strip()
                        self.app = tnlink.Client(
                            host, int(getattr(a, "port", 0) or tnlink.DEFAULT_PORT),
                            code)
                        if relay and not host:
                            self.app.__init_relay__(relay)
                        import tnlink_api
                        tnlink_api.log("connecting to the plant ...")
                        self.app.connect()
                        tnlink_api.log("paired with the plant", "ok")
                        self._paired = True
                        # connect() returns once the session is up; the
                        # thread then just holds it open. Commands come
                        # from the tray and the interface, not from here.
                        while getattr(self.app, "alive", False) and not self._stopping:
                            time.sleep(1.0)
                except Exception as exc:                 # noqa: BLE001
                    self.error = "%s: %s" % (type(exc).__name__, exc)
                    self._notify("TN-Link stopped", self.error)

            self._thread = threading.Thread(target=_run, name="tnlink-service",
                                            daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stopping = True
        self._paired = False
        app = self.app
        if not app:
            return
        for name in ("cmd_tunnel_down", "cmd_bridge_down"):
            fn = getattr(app, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
        try:
            app.alive = False
            if getattr(app, "sec", None):
                app.sec.close()
        except Exception:
            pass

    def restart(self) -> None:
        self.stop()
        time.sleep(1.0)
        self.app = None
        self.start()
        self._notify("TN-Link", "Service restarted.")

    def _notify(self, title: str, msg: str) -> None:
        if self._tray is not None:
            self._tray.notify(title, msg)

    # -- the address book -------------------------------------------------
    # Keyed by the other end's permanent unit id, so an entry survives the
    # pairing code rotating (codes are one-shot by design) and the far end
    # being renamed. See tnlink.remember_connection.

    # -- actions the menu calls ------------------------------------------
    def actions(self) -> Dict[str, Any]:
        import tnlink
        import tnlink_tray

        def rename_connection(unit="", name=""):
            """Give a machine the name the operator calls it by."""
            ok = bool(tnlink.rename_connection(str(unit or ""), str(name or "")))
            self._notify("TN-Link",
                         ("Renamed to %s." % name) if ok and name
                         else ("Name cleared." if ok else "No such connection."))
            return {"ok": ok}

        def favorite_connection(unit="", on=True):
            ok = bool(tnlink.favorite_connection(str(unit or ""), bool(on)))
            return {"ok": ok}

        def forget_connection(unit=""):
            ok = bool(tnlink.forget_connection(str(unit or "")))
            return {"ok": ok}

        def copy_code():
            code = self.snapshot().get("code") or ""
            if code and _clip(code):
                self._notify("TN-Link", "Connection code copied.")
            elif not code:
                self._notify("TN-Link", "No connection code yet.")

        def new_code():
            # Regenerating invalidates the old code on purpose: that is how
            # an operator revokes access they handed out by phone.
            try:
                self.args.code = tnlink.new_code()
                self.restart()
                self._notify("TN-Link", "New code: %s" % self.args.code)
            except Exception as exc:                     # noqa: BLE001
                self._notify("TN-Link", "Could not make a new code: %s" % exc)

        def show_qr():
            # A phone reads the code off the screen; the console already
            # knows how to draw it, so it is not drawn twice.
            open_interface()

        def change_relay():
            open_interface()

        def enter_code():
            open_interface()

        def connect_to(code):
            self.args.code = code
            self.remember(code)
            self.restart()

        def connect_saved(unit=""):
            """Reconnect to a machine we have paired with before.

            2026-10-31, requested: "a way to reconnect to the same computer
            after disconnect without re-entering the connection id code".
            The code that machine expects from us next was handed over at the
            previous pairing, over the encrypted channel, and filed against
            its permanent unit id - so there is nothing for the operator to
            type and nothing anyone else could have overheard.
            """
            code = ""
            try:
                code = tnlink.next_code_for(str(unit or ""))
            except Exception:
                code = ""
            if not code:
                self._notify("TN-Link",
                             "No stored code for that machine yet - pair once "
                             "with a code it gives you, and reconnecting is "
                             "automatic after that.")
                return {"ok": False, "reason": "no stored code"}
            connect_to(code)
            return {"ok": True}

        def open_interface():
            # The window is the default; the browser and the console are
            # the fallbacks inside this one call, so every caller gets the
            # best interface the machine can give.
            tnlink_tray.open_console_window()

        def open_in_browser():
            if not tnlink_tray.open_in_browser():
                self._notify("TN-Link", "Could not open the interface.")

        def stop_sharing():
            self.stop()
            self._notify("TN-Link", "Sharing stopped.")

        def disconnect_peer(_pid=None):
            self.stop()

        def _set_role(role):
            """Chosen from the tray, remembered, and started straight away."""
            import tnlink as _tn
            self.stop()
            self.app = None
            self.role = role
            _tn.remember_role(role)
            if self._tray is not None:
                self._tray.role = "plant" if role == "server" else "engineer"
                self._tray.actions = self.actions()
                self._tray._icon.menu = self._tray._build_menu() if self._tray._icon else None
            self.start()
            self._notify("TN-Link", "This computer is now the %s."
                         % ("plant" if role == "server" else "engineer"))

        def become_plant():
            _set_role("server")

        def become_engineer():
            # 2026-10-29, reported: "when I click it open a new window it
            # should not happen". The operator is ALREADY looking at the
            # interface - that is where they clicked. The card for typing
            # a code appears in the window they are in.
            _set_role("client")

        def switch_role():
            import tnlink as _tn
            self.stop()
            self.app = None
            self.role = ""
            _tn.remember_role("")
            if self._tray is not None:
                self._tray.role = "idle"
                self._tray.actions = self.actions()
                if self._tray._icon:
                    self._tray._icon.menu = self._tray._build_menu()
            self._notify("TN-Link", "Choose what this computer is.")

        def scan_devices():
            """Find what is on the plant network, for the window to list."""
            app = self.app
            if app is None or not hasattr(app, "find_plcs"):
                raise RuntimeError("not connected to a plant yet")
            import tnlink_api
            tnlink_api.log("scanning the plant network ...")
            found = app.find_plcs() or []
            tnlink_api.log("found %d device(s)" % len(found),
                           "ok" if found else "warn")
            return len(found)

        def connect_device(ip=""):
            """Reach one device - real address when possible."""
            app = self.app
            if app is None or not hasattr(app, "cmd_plc"):
                raise RuntimeError("not connected to a plant yet")
            ip = str(ip or "").strip()
            if not ip:
                raise ValueError("no address given")
            import tnlink_api
            tnlink_api.log("connecting to %s ..." % ip)
            app.cmd_plc(ip, "44818")
            tnlink_api.log("connected to %s" % ip, "ok")
            return ip

        # -- the console menu, on the tray ------------------------------
        # 2026-10-28, requested: "add the 7-8 options that we had before in
        # the tray app including remote desktop connection". These are the
        # SAME `cmd_*` the console menu calls - not a second
        # implementation - so a fix to either is a fix to both. What they
        # print goes into the interface log, because under the tray the
        # console is hidden and an action whose only output is invisible
        # reads as an action that did nothing.
        def _console(label, fn, *a):
            import contextlib
            import io as _io
            import tnlink_api
            buf = _io.StringIO()
            tnlink_api.log(label)
            try:
                # redirect_stdout is process-wide, so a line printed by the
                # session thread during these few seconds lands in the log
                # instead of the console. The console is hidden under the
                # tray anyway, and the log is where the operator is
                # looking, so nothing is lost either way.
                with contextlib.redirect_stdout(buf):
                    fn(*a)
            finally:
                for line in buf.getvalue().splitlines():
                    if line.strip():
                        tnlink_api.log(line.rstrip())
            return True

        def _engineer():
            app = self.app
            if app is None or self.role == "server":
                raise RuntimeError("connect to a plant first")
            return app

        def connect_plc(ip=""):
            """Menu 1. The device keeps its REAL address, for RSLinx."""
            app = _engineer()
            ip = str(ip or "").strip()
            if not ip:
                raise ValueError("pick a device first")
            return _console("connecting to %s ..." % ip, app.cmd_plc, ip, "44818")

        def find_devices():
            """Menu 2. Everything that answers, not just Allen-Bradley."""
            app = _engineer()
            return _console("scanning the plant network ...", app.cmd_devices)

        def remote_desktop():
            """Menu 3, and then actually open it.

            cmd_rdp forwards 3389 to a free local port and PRINTS where.
            Under the tray nobody reads that, so the forward is set up and
            mstsc is launched at the port it chose - which is the thing the
            operator wanted when they pressed the menu entry.
            """
            app = _engineer()
            before = len(getattr(app, "maps", []) or [])
            _console("forwarding Remote Desktop ...", app.cmd_rdp)
            maps = getattr(app, "maps", []) or []
            if len(maps) <= before:
                raise RuntimeError("the Remote Desktop forward was not set up")
            local = str(maps[-1].get("local") or "")
            import subprocess
            import tnlink_api
            try:
                subprocess.Popen(["mstsc", "/v:%s" % local])
                tnlink_api.log("Remote Desktop opening on %s" % local, "ok")
            except Exception as exc:                     # noqa: BLE001
                tnlink_api.log("open Remote Desktop yourself: %s (%s)"
                               % (local, exc), "warn")
            return local

        def reach_device(ip=""):
            """Menu 4. Every port the device answers on, not just one."""
            app = _engineer()
            ip = str(ip or "").strip()
            if not ip:
                raise ValueError("pick a device first")
            return _console("reaching %s ..." % ip, app.cmd_device, ip)

        def what_is_set_up():
            """Menu 5."""
            app = self.app
            if app is None or not hasattr(app, "cmd_status"):
                raise RuntimeError("nothing is running yet")
            return _console("what is set up right now:", app.cmd_status)

        def share_network():
            """Menu 6. Reach ANY device at its own address."""
            app = _engineer()
            return _console("sharing the whole plant network ...",
                            app.menu_share_network)

        def share_bridge():
            """Menu 8. Layer 2, for broadcast browsing only."""
            app = _engineer()
            return _console("sharing at Layer 2 ...", app.cmd_bridge, "")

        def stop_everything():
            """Menu 7. Everything this PC put up, taken back down."""
            app = self.app
            if app is None:
                return True
            import tnlink_api
            for name in ("cmd_tunnel_down", "cmd_bridge_down"):
                fn = getattr(app, name, None)
                if callable(fn):
                    try:
                        _console("stopping ...", fn)
                    except Exception:
                        pass
            try:
                app.maps = []
            except Exception:
                pass
            tnlink_api.log("everything stopped", "ok")
            return True

        def copy_unit_id():
            uid = self.snapshot().get("unit_id") or ""
            if uid and _clip(uid):
                self._notify("TN-Link", "Unit ID copied: %s" % uid)

        def open_logs():
            # Requested: the logs are a menu entry that opens the
            # interface, rather than a second window that has to be kept
            # in step with it.
            open_interface()

        def quit_app():
            """Quit means the process is GONE.

            2026-10-28, reported: "the process need to be killed when we
            close the app to avoid multiples istances". Stopping the
            service was never enough - the icon kept the loop running, and
            the exit pause then held the process on a hidden console. The
            icon is stopped so the tray loop returns and run_tray exits
            cleanly; the watchdog is the backstop for anything that blocks
            on the way out.
            """
            self.stop()
            try:
                if self._tray is not None and self._tray._icon is not None:
                    self._tray._stop.set()
                    self._tray._icon.stop()
            except Exception:
                pass

            def _backstop():
                time.sleep(4.0)
                os._exit(0)

            threading.Thread(target=_backstop, name="tnlink-quit",
                             daemon=True).start()

        return {
            "connect_saved": connect_saved,
            "rename_connection": rename_connection,
            "favorite_connection": favorite_connection,
            "forget_connection": forget_connection,
            "copy_code": copy_code,
            "new_code": new_code,
            "show_qr": show_qr,
            "change_relay": change_relay,
            "enter_code": enter_code,
            "connect_to": connect_to,
            "open_interface": open_interface,
            "open_in_browser": open_in_browser,
            "stop_sharing": stop_sharing,
            "connect_plc": connect_plc,
            "find_devices": find_devices,
            "remote_desktop": remote_desktop,
            "reach_device": reach_device,
            "what_is_set_up": what_is_set_up,
            "share_network": share_network,
            "share_bridge": share_bridge,
            "stop_everything": stop_everything,
            "disconnect_peer": disconnect_peer,
            "become_plant": become_plant,
            "become_engineer": become_engineer,
            "switch_role": switch_role,
            "scan_devices": scan_devices,
            "connect_device": connect_device,
            "copy_unit_id": copy_unit_id,
            "open_logs": open_logs,
            "restart": self.restart,
            "quit": quit_app,
        }


def run_tray(role: str, args: Any) -> int:
    """Start the service and show the tray. Blocks until the operator quits."""
    import tnlink_tray
    svc = LinkService(role, args)
    # One API, consumed by the tray AND the window, so the two front ends
    # cannot tell the operator different stories about the same session.
    try:
        import tnlink_api
        svc.api = tnlink_api.LocalApi(svc.snapshot, svc.actions())
        svc.api.start()
    except Exception as exc:                            # noqa: BLE001
        svc.api = None
        print("  interface API not started: %s" % exc)
    # The console belongs to this process and is the interface; hide it
    # while the tray is in charge so the operator gets a tray app, not a
    # window they must not close.
    tnlink_tray.show_console(False)
    tray = tnlink_tray.TrayApp(role, svc.actions(), svc.snapshot)
    svc._tray = tray
    # With no role chosen there is nothing to start - the tray opens on the
    # "what is this computer" menu and starts whatever the operator picks.
    if role:
        svc.start()
    rc = tray.run()
    # Quit means GONE. The tray has stopped, the service has stopped, and
    # what is left is a relay dial or a socket accept inside a daemon
    # thread that may sit in a blocking call for another minute. Leaving
    # the process alive is what produced several instances on one machine,
    # so the exit is made unconditional here rather than hoped for.
    try:
        if svc.api is not None:
            svc.api.stop()
    except Exception:
        pass
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(int(rc or 0))
