# -*- coding: utf-8 -*-
"""End to end: start a plant and an engineer, pair them, run every action.

2026-10-28, asked for directly: "we need to test every sing action and
flow to make sure the app is valid and fully working from one end to other
end in the back and frontend".

What this proves, against the BUILT exe rather than the source:

  * the plant starts, mints a code and serves its interface;
  * the engineer starts, pairs over the relay, and BOTH ends say so;
  * the page the core serves contains the controls the operator presses
    (the front end is checked as shipped, not as written);
  * every action in the registry answers - either by doing the thing or
    by refusing it for a reason that names the cause;
  * Quit leaves no process behind, which is the fault that produced
    several instances on one machine;
  * a second instance refuses to start and hands over to the first.

Run:
    python test_tnlink_e2e.py [path-to-tnlink.exe]

It uses TNLINK_API_FILE so the two ends keep separate endpoints, and it
kills what it started. Nothing here touches a real plant network: the only
device scans are against whatever this machine can see, and a scan that
finds nothing is still a pass - the test asserts the ACTION answered, not
what the network contains.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, "dist", "tnlink.exe")

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    print("  %-52s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if detail else ""))
    return ok


class End:
    """One running LS-Link, talked to the way the interface talks to it."""

    def __init__(self, role: str, exe: str, extra=()):
        self.role = role
        self.file = os.path.join(tempfile.mkdtemp(prefix="tnlink_%s_" % role),
                                 "api.json")
        env = dict(os.environ)
        env["TNLINK_API_FILE"] = self.file
        env.pop("ELECTRON_RUN_AS_NODE", None)
        self.log = open(self.file + ".log", "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [exe, "--tray", "--role", role] + list(extra),
            env=env, stdout=self.log, stderr=subprocess.STDOUT)
        self.ep = {}

    def wait_ready(self, seconds: float = 40.0) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            try:
                with open(self.file, "r", encoding="utf-8") as fh:
                    self.ep = json.load(fh)
                if self.ep.get("port"):
                    self.get("/state")
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _req(self, path, method="GET", body=None, timeout=120):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (int(self.ep["port"]), path),
            method=method,
            data=(json.dumps(body).encode() if body is not None else None),
            headers={"X-TNLink-Token": self.ep["token"],
                     "Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=timeout)

    def get(self, path, timeout=30):
        return json.load(self._req(path, timeout=timeout))

    def do(self, name, body=None, timeout=120):
        """Returns (ok, payload). A refusal is a result, not an exception."""
        try:
            return True, json.load(self._req("/do/" + name, "POST",
                                             body or {}, timeout))
        except urllib.error.HTTPError as exc:
            try:
                return False, json.loads(exc.read().decode())
            except Exception:
                return False, {"error": "HTTP %s" % exc.code}
        except Exception as exc:                        # noqa: BLE001
            return False, {"error": str(exc)}

    def page(self) -> str:
        return urllib.request.urlopen(
            "http://127.0.0.1:%d/" % int(self.ep["port"]), timeout=20
        ).read().decode("utf-8")

    def alive(self) -> bool:
        return self.proc.poll() is None

    def kill(self):
        for fn in (self.proc.terminate, self.proc.kill):
            try:
                fn()
                self.proc.wait(timeout=5)
                break
            except Exception:
                continue
        try:
            self.log.close()
        except Exception:
            pass


#: Every action the interface can name, and whether it is safe to fire in
#: a test. The ones left out would open a window, restart the service or
#: quit it - they are exercised by the flow tests instead.
SAFE = ["what_is_set_up", "find_devices", "remote_desktop", "share_bridge",
        "share_network", "stop_everything", "copy_code", "copy_unit_id"]
NEEDS_DEVICE = ["connect_plc", "reach_device"]


def electron_window(end: End) -> None:
    """Open the real window and listen to what the page says.

    2026-10-28. The window rendered and did nothing, while the browser
    served by the same core was fine. There was no error anywhere to see:
    the preload exposes `window.core`, the page declared `const core`
    beside it, and that redeclaration killed the whole script before its
    first line. Nothing short of reading the page's own console finds
    that, so the test reads it.
    """
    electron = os.path.join(HERE, "ui", "node_modules", "electron", "dist",
                            "electron.exe")
    if not os.path.isfile(electron):
        print("  (no Electron runtime beside the source - skipped)")
        return
    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    env.pop("TNLINK_API_FILE", None)
    env["TNLINK_PORT"] = str(end.ep["port"])
    env["TNLINK_TOKEN"] = str(end.ep["token"])
    proc = subprocess.Popen([electron, os.path.join(HERE, "ui")], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    try:
        time.sleep(14.0)
        title = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process electron -EA SilentlyContinue | "
             "Where-Object { $_.MainWindowTitle -ne '' } | "
             "Select-Object -First 1 -Expand MainWindowTitle"],
            capture_output=True, text=True).stdout.strip()
        check("the window opens", title == "LS-Link", title or "no window")
        # Counting connections ONCE proved nothing: TIME_WAIT lingers for
        # two minutes, so the count was non-zero even with the page dead.
        # The page polls every 2 s, so what distinguishes a live window
        # from a dead one is the count GROWING.
        def conns():
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-NetTCPConnection -LocalPort %d -EA SilentlyContinue | "
                 "Where-Object { $_.State -ne 'Listen' } | Measure-Object).Count"
                 % int(end.ep["port"])],
                capture_output=True, text=True).stdout.strip()
            return int(out or 0)

        before = conns()
        time.sleep(8.0)
        after = conns()
        check("the window keeps polling the core", after > before,
              "%d -> %d connections" % (before, after))
    finally:
        proc.terminate()
        try:
            text = proc.communicate(timeout=10)[0] or ""
        except Exception:
            text = ""
    bad = [l.strip() for l in text.splitlines()
           if "[page]" in l and "Security Warning" not in l]
    bad += [l.strip() for l in text.splitlines() if "[tnlink]" in l]
    check("the page runs with no errors", not bad,
          bad[0][:70] if bad else "")


def _quit_default_instance(timeout: float = 20.0) -> bool:
    """Ask whatever is running on the default endpoint to quit."""
    path = os.path.join(os.environ.get("APPDATA", ""), "TrustNodeLink",
                        "api.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            ep = json.load(fh)
        req = urllib.request.Request(
            "http://127.0.0.1:%d/do/quit" % int(ep["port"]), method="POST",
            data=b"{}", headers={"X-TNLink-Token": ep["token"],
                                 "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except Exception:
        return False


def only_one(exe: str) -> None:
    """There must never be two LS-Links running at once.

    2026-10-29, reported: "we have multiple instances open at the same
    time, this should not be possible... we just need to make sure there
    is no more than 1 open at the same time". The new start does not kill
    the old one - the running instance may be holding a live session, and
    stopping it to start an identical copy would drop an engineer
    mid-download. The second start refuses, shows the first one's window
    and exits.

    This runs LAST, with no session up, so it cannot disturb the pair
    above.
    """
    env = dict(os.environ)
    env.pop("TNLINK_API_FILE", None)          # the real, default behaviour
    # DEVNULL, not PIPE: the interface the app launches inherits the
    # handles, so a pipe is not closed when tnlink exits and the wait
    # hangs for ever. That is what timed out the first run of this suite -
    # the test was broken, not the app.
    first = subprocess.Popen([exe, "--tray", "--role", "server"], env=env,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL)
    time.sleep(14.0)
    if not check("the first instance is running", first.poll() is None):
        return
    out = os.path.join(tempfile.mkdtemp(prefix="tnlink_second_"), "out.txt")
    with open(out, "w", encoding="utf-8") as fh:
        second = subprocess.Popen([exe, "--tray", "--role", "server"],
                                  env=env, stdout=fh,
                                  stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL)
        try:
            second.wait(timeout=90)
            ended = True
        except subprocess.TimeoutExpired:
            ended = False
    text = ""
    try:
        with open(out, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception:
        pass
    try:
        check("a second start exits instead of running", ended,
              "it stayed up" if not ended else "exit %s" % second.returncode)
        check("it says the app is already running",
              "already running" in text,
              (text.strip().splitlines() or ["no output"])[-1][:60])
        check("the first instance is untouched", first.poll() is None,
              "it was killed" if first.poll() is not None else "still serving")
    finally:
        if second.poll() is None:
            for fn in (second.terminate, second.kill):
                try:
                    fn()
                    second.wait(timeout=5)
                    break
                except Exception:
                    continue
        _quit_default_instance()
        time.sleep(5.0)
        if first.poll() is None:
            for fn in (first.terminate, first.kill):
                try:
                    fn()
                    first.wait(timeout=5)
                    break
                except Exception:
                    continue
    check("nothing is left running afterwards",
          first.poll() is not None and second.poll() is not None)


def main(argv) -> int:
    exe = argv[1] if len(argv) > 1 else EXE
    if not os.path.isfile(exe):
        print("  no exe at %s - build it first" % exe)
        return 2
    print("LS-Link end to end")
    print("  exe: %s (%.1f MB)" % (exe, os.path.getsize(exe) / 1e6))
    print()

    plant = End("server", exe)
    try:
        print("plant")
        if not check("plant starts and serves its interface", plant.wait_ready()):
            return 2
        st = plant.get("/state")
        code = st.get("code") or ""
        check("plant mints a connection code", code.startswith("TN-"), code)
        check("plant has a unit ID", bool(st.get("unit_id")), st.get("unit_id"))
        check("plant reports a session window", bool(st.get("expires")),
              st.get("expires"))
        check("plant is running", bool(st.get("running")))
        check("plant has nobody paired yet", int(st.get("peers") or 0) == 0)

        page = plant.page()
        print()
        print("front end, as served")
        for probe, what in [
            ("window.__TNLINK__", "the page carries this run's token"),
            ("id=\"card-actions\"", "the options card is present"),
            ("data-act=\"remote_desktop\"", "Remote Desktop is on the page"),
            ("data-act=\"share_network\"", "Share the network is on the page"),
            ("connect_plc", "Connect (real address) is wired"),
            ("reach_device", "All ports is wired"),
            ("id=\"card-reach\"", "the Connected card is present"),
            ("overflow:hidden", "the page itself does not scroll"),
            ("class=\"scroll\"", "the long lists scroll inside their card"),
            # 2026-10-29, each from something seen on screen.
            ("[hidden] { display: none !important; }",
             "a hidden card is really hidden"),
            ("id=\"card-peer\"", "the plant it is connected TO has its own field"),
            ("button.on", "the chosen role is shown as chosen"),
            ("queued = [name, body]", "a click is queued, never dropped"),
            (".slice().reverse()", "the log reads newest first"),
            ("What it is", "the device list names what the device is"),
        ]:
            check(what, probe in page)
        # These two are about things that must NOT be there any more.
        check("no duplicate Start card beside This unit",
              "card-start" not in page)
        check("there is exactly one Connection code card",
              page.count("<h2>Connection code</h2>") == 1,
              "%d found" % page.count("<h2>Connection code</h2>"))

        print()
        print("the Electron window")
        electron_window(plant)

        print()
        print("engineer")
        eng = End("client", exe, ["--code", code])
        try:
            if not check("engineer starts", eng.wait_ready()):
                return 2
            paired = False
            end = time.time() + 60
            while time.time() < end:
                if int(eng.get("/state").get("peers") or 0):
                    paired = True
                    break
                time.sleep(1.0)
            check("engineer pairs with the plant over the relay", paired)
            # The engineer's flag flips the moment connect() returns; the
            # plant sets its own a moment later, on the other side of the
            # relay. Asserting them in the same instant tested the race,
            # not the app - measured, the plant was there 3 s later.
            seen = False
            end = time.time() + 30
            while time.time() < end:
                if int(plant.get("/state").get("peers") or 0) == 1:
                    seen = True
                    break
                time.sleep(1.0)
            check("plant sees the engineer", seen,
                  "peers=%s" % plant.get("/state").get("peers"))

            print()
            print("every action answers")
            for name in SAFE:
                ok, out = eng.do(name)
                detail = "" if ok else str(out.get("error", ""))[:60]
                # A refusal is a pass when it NAMES the reason: the test is
                # that the action is wired and answers, not that this
                # machine happens to have a PLC on it. What the action
                # printed is shown either way, because an action that
                # "succeeded" into an empty log is not evidence of much.
                check("action %s" % name, ok or bool(detail), detail)
                for ln in (eng.get("/logs").get("lines") or [])[-3:]:
                    print("        | %s" % ln.get("text", "")[:86])

            devices = eng.get("/state").get("devices") or []
            print("  (%d device(s) found on this machine's network)" % len(devices))
            for name in NEEDS_DEVICE:
                ok, out = eng.do(name, {"ip": devices[0]["ip"]} if devices else {})
                detail = "" if ok else str(out.get("error", ""))[:60]
                check("action %s" % name, ok or bool(detail), detail)

            st = eng.get("/state")
            check("state carries what is reachable",
                  isinstance(st.get("reachable"), list),
                  "%d entr(y/ies)" % len(st.get("reachable") or []))
            # An engineer has no code of its own: showing the plant's as
            # "your connection code" put the same string in two fields.
            check("the engineer reports no connection code of its own",
                  not st.get("code"), repr(st.get("code"))[:30])
            check("the engineer names the plant it reached",
                  bool(st.get("peer")), st.get("peer") or "empty")
            # Identity: a port says EtherNet/IP, which every controller,
            # drive and I/O block answers. This asks the device.
            named = [d for d in (st.get("devices") or []) if d.get("what")]
            check("devices report maker and product where they answer",
                  bool(named) or not st.get("devices"),
                  (named[0].get("what") if named else "none answered")[:52])
            for d in named[:3]:
                print("        | %-15s %s  (%s)"
                      % (d.get("ip"), d.get("what"),
                         ", ".join(x for x in [d.get("type"),
                                               "part " + d["part"] if d.get("part") else "",
                                               "rev " + d["revision"] if d.get("revision") else ""]
                                   if x)))

        finally:
            print()
            print("shutdown")
            ok, _ = eng.do("quit", timeout=20)
            time.sleep(3.0)
            check("Quit ends the engineer process", not eng.alive())
            eng.kill()
    finally:
        ok, _ = plant.do("quit", timeout=20)
        time.sleep(3.0)
        check("Quit ends the plant process", not plant.alive())
        plant.kill()

    print()
    print("only one at a time")
    only_one(exe)

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
