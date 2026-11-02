# -*- coding: utf-8 -*-
"""TN-Link in the notification area: start it once, leave it running.

2026-10-28, requested: "make the app a tray service app that can be kept on
on the tray with menu option actions straight from the menu, like restart,
quit, open interface, share id number, enter the remote server id".

Why a tray app at all. The console build has to stay open to stay
connected: close the window and the plant end disappears, which is the
opposite of what a machine beside a PLC needs. A tray icon is the smallest
thing that keeps it running, says whether it is connected, and puts the
four or five actions an operator actually performs one click away.

The menus differ by role because the jobs differ:

  PLANT (server) - the PC beside the PLC, left running
      Status: ...                     (disabled, reads the live state)
      Connection code: ABCD-...       -> copies it
      Copy connection code
      Show QR code                    (a phone can read it)
      New connection code             (invalidates the old one)
      ---
      Open interface                  (the console window)
      Relay: configured               (disabled; the host is not shown)
      Change relay address...
      ---
      Connected clients (n)           -> submenu, one entry each, Disconnect
      Share: PLC / network / nothing  -> what this end is currently offering
      ---
      Restart service
      Quit

  ENGINEER (client) - the laptop, connecting out
      Status: ...
      Enter connection code...        -> paste the plant's code
      Recent plants                   -> submenu, reconnect in one click
      ---
      Open interface
      Relay: ... / Change relay address...
      ---
      Reach a device...               (one PLC, its REAL address)
      Share the whole network
      Stop sharing
      ---
      Restart service
      Quit

Nothing here owns the protocol: the tray drives the same functions the
console does, so there is one implementation of connecting and sharing and
this file only decides what is on the menu.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import webbrowser
from typing import Any, Callable, Dict, List, Optional

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:                                        # pragma: no cover
    pystray = None
    Image = ImageDraw = None


# --------------------------------------------------------------------- icon
#: The tray icon is drawn rather than shipped: one less file to lose in a
#: build, and the colour carries the only state an operator needs at a
#: glance - connected, waiting, or stopped.
_COLOURS = {
    "connected": (20, 168, 154),     # teal
    "waiting": (217, 131, 36),       # amber: listening, nobody there yet
    "stopped": (120, 124, 130),      # grey
    "error": (200, 60, 60),
}


def _icon_image(state: str = "stopped"):
    if Image is None:
        return None
    rgb = _COLOURS.get(state, _COLOURS["stopped"])
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=rgb)
    # A link: two rings, so the icon reads as "connection" at 16px.
    d.ellipse((18, 22, 34, 42), outline=(255, 255, 255, 235), width=4)
    d.ellipse((30, 22, 46, 42), outline=(255, 255, 255, 235), width=4)
    return img


class TrayApp:
    """The notification-area front end.

    `actions` is supplied by the caller - the console app - so this file
    never reaches into the protocol. Every entry is optional; a missing one
    simply does not appear, which is how the two roles share one class.
    """

    def __init__(self, role: str, actions: Dict[str, Callable[..., Any]],
                 state: Callable[[], Dict[str, Any]]):
        r = str(role or "").lower()
        # 2026-10-28: "" is a legitimate state - the app has started and the
        # operator has not said what this computer is yet. It gets its own
        # menu offering both, rather than guessing one and being wrong.
        self.role = ("plant" if r.startswith(("p", "s"))
                     else ("engineer" if r.startswith(("e", "c")) else "idle"))
        self.actions = actions or {}
        self.state = state
        self._icon = None
        self._stop = threading.Event()

    # -- helpers ---------------------------------------------------------
    def _call(self, name: str, *a):
        fn = self.actions.get(name)
        if not fn:
            return
        try:
            fn(*a)
        except Exception as exc:                          # noqa: BLE001
            self.notify("TN-Link", "%s failed: %s" % (name, exc))

    def notify(self, title: str, message: str) -> None:
        try:
            if self._icon is not None:
                self._icon.notify(message, title)
        except Exception:
            pass

    def _snapshot(self) -> Dict[str, Any]:
        try:
            return self.state() or {}
        except Exception:
            return {}

    # -- menu ------------------------------------------------------------
    def _status_text(self, *_a) -> str:
        s = self._snapshot()
        if self.role == "idle":
            return "Status: not started - choose below"
        if not s.get("running"):
            return "Status: stopped"
        peers = int(s.get("peers") or 0)
        if self.role == "plant":
            return ("Status: %d engineer(s) connected" % peers) if peers \
                else "Status: waiting for an engineer"
        return "Status: connected to the plant" if peers \
            else "Status: not connected"

    def _relay_text(self, *_a) -> str:
        s = self._snapshot()
        # 2026-10-28, revised: the HOST is not shown any more. It was put
        # here so a customer could see where it dialled; the operator has
        # since asked for it off both ends, because the address names the
        # company running the relay to anyone looking at the screen. The
        # state is still useful, the hostname is not - and "Change relay
        # address" below still lets an operator point at their own.
        return "Relay: %s" % ("configured" if s.get("relay") else "direct")

    def _code_text(self, *_a) -> str:
        s = self._snapshot()
        code = str(s.get("code") or "")
        return ("Connection code: %s" % code) if code else "Connection code: none yet"

    def _peer_items(self):
        s = self._snapshot()
        peers: List[Dict[str, Any]] = s.get("peer_list") or []
        if not peers:
            return (pystray.MenuItem("No engineer connected", None, enabled=False),)
        out = []
        for p in peers:
            label = "%s  (%s)" % (p.get("name") or p.get("id") or "engineer",
                                  p.get("since") or "")
            pid = p.get("id")
            # pystray validates the action's arity: it accepts a callable
            # of zero args or of (icon, item). A default-argument capture
            # makes three and is rejected outright, so the id is closed over
            # by a factory instead.
            def _disconnect(pid=pid):
                return lambda icon, item: self._call("disconnect_peer", pid)
            out.append(pystray.MenuItem(
                label,
                pystray.Menu(pystray.MenuItem("Disconnect", _disconnect()))))
        return tuple(out)

    def _share_items(self):
        """What this session is actually carrying, right now."""
        s = self._snapshot()
        rows = s.get("shared") or []
        if not rows:
            return (pystray.MenuItem("Nothing shared yet", None, enabled=False),)
        return tuple(pystray.MenuItem(str(r), None, enabled=False) for r in rows)

    def _device_items(self, action: str):
        """One entry per device found, for the menus that need a target.

        2026-10-28. The console asks "device number or IP"; a tray menu is
        the wrong place to ask a question, so the answer is the menu. With
        nothing scanned yet the only entry scans - which is the step the
        operator would have had to take anyway.
        """
        s = self._snapshot()
        devices: List[Dict[str, Any]] = s.get("devices") or []
        if not devices:
            return (pystray.MenuItem("Find devices on the plant network first",
                                     lambda: self._call("find_devices")),)
        def _go(ip):
            return lambda icon, item: self._call(action, ip)
        out = []
        for d in devices[:20]:
            ip = str(d.get("ip") or "")
            label = "%-16s %s" % (ip, d.get("kind") or "device")
            out.append(pystray.MenuItem(label, _go(ip)))
        return tuple(out)

    def _plc_items(self):
        return self._device_items("connect_plc")

    def _reach_items(self):
        return self._device_items("reach_device")

    def _recent_items(self):
        s = self._snapshot()
        recents: List[str] = s.get("recent") or []
        if not recents:
            return (pystray.MenuItem("No recent plants", None, enabled=False),)
        def _connect(code):
            return lambda icon, item: self._call("connect_to", code)
        return tuple(pystray.MenuItem(code, _connect(code)) for code in recents)

    def _build_menu(self):
        M = pystray.MenuItem
        common_tail = (
            pystray.Menu.SEPARATOR,
            M("Restart service", lambda: self._call("restart")),
            M("Quit", self._quit),
        )
        relay_block = (
            M(self._relay_text, None, enabled=False),
            M("Change relay address…", lambda: self._call("change_relay")),
        )
        # 2026-10-28, requested: the window is the interface, the browser
        # is an option. Both open the SAME page served by the core, so
        # they cannot show different things.
        interface_block = (
            M("Open interface", lambda: self._call("open_interface")),
            M("Open interface in browser",
              lambda: self._call("open_in_browser")),
        )
        if self.role == "idle":
            # One app, both jobs, decided here instead of at a command line.
            return pystray.Menu(
                M(self._status_text, None, enabled=False),
                pystray.Menu.SEPARATOR,
                # 2026-10-28, requested: no role question in the
                # interface - it belongs on this menu, in words that say
                # what the machine DOES rather than "server" and "client",
                # which mean nothing to the person holding the laptop.
                M("Start: this PC is AT THE PLANT (shares the PLC network)",
                  lambda: self._call("become_plant")),
                M("Start: this PC is THE ENGINEER (connects to a plant)",
                  lambda: self._call("become_engineer")),
                pystray.Menu.SEPARATOR,
                *interface_block,
                *relay_block,
                *common_tail,
            )
        if self.role == "plant":
            return pystray.Menu(
                M(self._status_text, None, enabled=False),
                pystray.Menu.SEPARATOR,
                M(self._code_text, lambda: self._call("copy_code")),
                M("Copy connection code", lambda: self._call("copy_code")),
                M("Show QR code", lambda: self._call("show_qr")),
                M("New connection code", lambda: self._call("new_code")),
                pystray.Menu.SEPARATOR,
                *interface_block,
                *relay_block,
                pystray.Menu.SEPARATOR,
                # The relay pairs ONE engineer with ONE plant at a time;
                # a second is answered BUSY rather than queued. The menu
                # says exactly that instead of implying a list.
                M("Connected engineer", pystray.Menu(self._peer_items)),
                M("Unit ID: %s" % (self._snapshot().get("unit_id") or "-"),
                  lambda: self._call("copy_unit_id")),
                M("Session ends: %s" % (self._snapshot().get("expires") or "-"),
                  None, enabled=False),
                M("What is shared", pystray.Menu(self._share_items)),
                M("View logs", lambda: self._call("open_logs")),
                M("Stop sharing", lambda: self._call("stop_sharing")),
                M("Switch role…", lambda: self._call("switch_role")),
                *common_tail,
            )
        # The engineer's menu IS the console menu, in the same order, so
        # an operator who learnt one knows the other. Requested
        # 2026-10-28: "add the 7-8 options that we had before in the tray
        # app including remote desktop connection".
        return pystray.Menu(
            M(self._status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            M("Enter connection code…", lambda: self._call("enter_code")),
            M("Recent plants", pystray.Menu(self._recent_items)),
            pystray.Menu.SEPARATOR,
            M("1  Connect to a PLC  (RSLinx, Studio 5000)",
              pystray.Menu(self._plc_items)),
            M("2  Find devices on the plant network",
              lambda: self._call("find_devices")),
            M("3  Remote Desktop to the plant PC",
              lambda: self._call("remote_desktop")),
            M("4  Reach a device  (all its ports)",
              pystray.Menu(self._reach_items)),
            M("5  What is set up right now",
              lambda: self._call("what_is_set_up")),
            M("6  Share the whole network",
              lambda: self._call("share_network")),
            M("8  Share at Layer 2  (browsing only)",
              lambda: self._call("share_bridge")),
            M("7  Stop everything", lambda: self._call("stop_everything")),
            pystray.Menu.SEPARATOR,
            *interface_block,
            *relay_block,
            pystray.Menu.SEPARATOR,
            M("What is shared", pystray.Menu(self._share_items)),
            M("View logs", lambda: self._call("open_logs")),
            M("Stop sharing", lambda: self._call("stop_sharing")),
            M("Switch role…", lambda: self._call("switch_role")),
            *common_tail,
        )

    # -- lifecycle -------------------------------------------------------
    def _quit(self, *_a):
        self._stop.set()
        self._call("quit")
        try:
            if self._icon is not None:
                self._icon.stop()
        except Exception:
            pass

    def refresh(self) -> None:
        """Re-read the state so the menu and icon colour follow it."""
        if self._icon is None:
            return
        s = self._snapshot()
        state = "stopped"
        if s.get("error"):
            state = "error"
        elif s.get("running"):
            state = "connected" if int(s.get("peers") or 0) > 0 else "waiting"
        try:
            self._icon.icon = _icon_image(state)
            self._icon.title = "TN-Link - " + self._status_text().replace("Status: ", "")
            self._icon.update_menu()
        except Exception:
            pass

    def run(self) -> int:
        """Blocks. Returns 0 when the operator quits."""
        if pystray is None:
            print("  the tray needs pystray and Pillow:")
            print("      pip install pystray pillow")
            return 2
        self._icon = pystray.Icon("tnlink", _icon_image("waiting"),
                                  "TN-Link", self._build_menu())

        def _ticker():
            # The menu is rebuilt from the live state, so a connection that
            # arrives while the menu is closed still shows when it opens.
            while not self._stop.wait(3.0):
                self.refresh()

        threading.Thread(target=_ticker, name="tnlink-tray-tick",
                         daemon=True).start()
        self._icon.run()
        return 0


def _console_hwnd():
    try:
        import ctypes
        return ctypes.windll.kernel32.GetConsoleWindow()
    except Exception:
        return 0


def show_console(visible: bool = True) -> None:
    """Show or hide THIS process's console window.

    2026-10-28. The first version of "Open interface" launched tnlink.py as
    a NEW process - which would have started a SECOND copy of the app,
    competing for the same adapter, the same relay room and the same
    firewall rule. The interface and the tray are one program; the console
    is simply hidden while the tray is doing the talking, and shown again
    when the operator asks for it.
    """
    try:
        import ctypes
        hwnd = _console_hwnd()
        if not hwnd:
            return
        SW_HIDE, SW_SHOW = 0, 5
        ctypes.windll.user32.ShowWindow(hwnd, SW_SHOW if visible else SW_HIDE)
        if visible:
            ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _ui_dir():
    import os, sys
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    d = os.path.join(base, "ui")
    return d if os.path.isdir(d) else ""


def _interface_url() -> str:
    """Where the core is serving the interface for THIS run."""
    import json
    try:
        import tnlink_api
        with open(tnlink_api.endpoint_file(), "r", encoding="utf-8") as fh:
            ep = json.load(fh)
        return "http://127.0.0.1:%d/" % int(ep["port"])
    except Exception:
        return ""


def _electron_binary() -> str:
    """The Electron runtime, if this build ships one or the PC has it."""
    import os, shutil, sys
    here = os.path.dirname(os.path.abspath(sys.executable))
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    for c in (os.path.join(base, "electron", "electron.exe"),
              os.path.join(here, "electron", "electron.exe"),
              os.path.join(here, "ui", "electron", "electron.exe"),
              os.path.join(base, "ui", "node_modules", "electron", "dist",
                           "electron.exe")):
        if os.path.isfile(c):
            return c
    return shutil.which("electron") or shutil.which("electron.cmd") or ""


def _app_window_binary() -> str:
    """Chromium already on the machine, for a window with no runtime.

    2026-10-28. Electron is the interface when a build ships it. When one
    does not, the operator still asked for a WINDOW rather than a browser
    tab - and every Windows 10/11 machine already has Chromium in the form
    of Edge. `--app=` opens exactly that: its own window, its own taskbar
    entry, no address bar and no tabs. Same page, same core, no 250 MB of
    runtime carried to say it twice.
    """
    import os, shutil
    pf = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    pf64 = os.environ.get("ProgramFiles") or r"C:\Program Files"
    local = os.environ.get("LOCALAPPDATA") or ""
    for c in (os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
              os.path.join(pf64, "Microsoft", "Edge", "Application", "msedge.exe"),
              os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
              os.path.join(pf64, "Google", "Chrome", "Application", "chrome.exe"),
              os.path.join(local, "Google", "Chrome", "Application", "chrome.exe")):
        if c and os.path.isfile(c):
            return c
    return shutil.which("msedge") or shutil.which("chrome") or ""


def _endpoint() -> dict:
    """This run's port and token, read once."""
    import json
    try:
        import tnlink_api
        with open(tnlink_api.endpoint_file(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def open_window() -> bool:
    """Open the interface as a WINDOW. This is the default."""
    import os, subprocess
    url = _interface_url()
    if not url:
        return False
    ep = _endpoint()
    # ELECTRON_RUN_AS_NODE makes any Electron binary run as plain Node and
    # exit immediately - no window, no error. It is inherited from whatever
    # started us, so it is cleared for the child rather than trusted. This
    # cost a debugging round: "Open interface" reported success and nothing
    # appeared.
    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    # The window is TOLD where the core is. It used to look the endpoint
    # up in a file that a second instance rewrites, which is why the
    # browser worked and this did not.
    if ep.get("port") and ep.get("token"):
        env["TNLINK_PORT"] = str(ep["port"])
        env["TNLINK_TOKEN"] = str(ep["token"])
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    electron = _electron_binary()
    ui = _ui_dir()
    if electron and ui:
        try:
            subprocess.Popen([electron, ui], env=env, creationflags=flags)
            return True
        except Exception:
            pass
    app = _app_window_binary()
    if app:
        try:
            subprocess.Popen(
                [app, "--app=%s" % url,
                 "--window-size=1100,760",
                 # Its own profile, so the window is not merged into the
                 # operator's browsing session and cannot inherit a policy
                 # that blocks loopback pages.
                 "--user-data-dir=%s" % os.path.join(
                     os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                     "TrustNodeLink", "window")],
                env=env, creationflags=flags)
            return True
        except Exception:
            pass
    return False


def open_in_browser() -> bool:
    """Open the same page as an ordinary browser tab - the tray option.

    Requested 2026-10-28: "I want the interface opening in the browser as
    an option, in the tray, it should have a window using electron as
    default." Same URL, same core: the two front ends cannot drift.
    """
    import webbrowser
    url = _interface_url()
    if not url:
        return False
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def open_console_window(script=None) -> None:
    """The interface: a window first, then a browser, then the console.

    The console remains the last resort rather than the operator being
    told to install something before they can see what is happening.
    """
    if open_window():
        return
    if open_in_browser():
        return
    show_console(True)
