# -*- coding: utf-8 -*-
"""A command that needs a decision asks with buttons, not with a console prompt.

2026-11-02, from a screenshot: pressing "Layer 2 (browsing)" with two plant
networks printed "which network should be shared?" into the log and
stopped - the window had no way to answer a console prompt. Asked: "all the
options in the prompting should be moved to action buttons so the user has
control of everything".

Under the window the command raises NeedChoice; the service returns it to
the page as {"choice": ...}; the page shows the options with a Select
button each (and a text field where typing is the only way) and runs the
same action again with the answer. The console keeps input().

Also from the same session: a relay answer of "no server is waiting" during
the seconds the plant takes to re-dial was treated as a refused code and
the stored code was thrown away. The relay's outcomes are told apart now.

    python prototypes/tnlink/test_choices.py
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="tnlink_ch_")

import tnlink as T                                              # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-66s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


class _Sec:
    """A session that records what was sent and never answers."""
    def __init__(self):
        self.sent = []

    def send(self, frame):
        self.sent.append(frame)


def _client_with_two_lans():
    c = T.Client("127.0.0.1", 1, "TN-AAAAA-BBBBB-CCCCC")
    c.info = {"host": "PLANT", "subnets": [
        {"cidr": "192.168.0.0/24", "kind": "lan", "desc": "Realtek PCIe GbE"},
        {"cidr": "192.168.10.0/24", "kind": "lan", "desc": "MediaTek Wi-Fi 6"},
    ]}
    c.sec = _Sec()
    c._await = lambda *a, **k: None
    return c


def main() -> int:
    print("TN-Link: prompts become choices the window can answer")
    T._is_admin = lambda: True                    # the bridge needs admin; not the point here

    # ---- the console path is untouched ------------------------------------
    T.UI_CHOICES = False
    c = _client_with_two_lans()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        c.cmd_bridge("")
    check("console: the bridge still prints the question and returns",
          "which network should be shared?" in buf.getvalue() and not c.sec.sent)

    # ---- under the window the same call asks with options -----------------
    T.UI_CHOICES = True
    c = _client_with_two_lans()
    got = None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            c.cmd_bridge("")
    except T.NeedChoice as ch:
        got = ch
    check("window: the bridge raises NeedChoice instead of printing", got is not None)
    pl = got.payload() if got else {}
    check("  with both networks as options",
          [o["value"] for o in pl.get("options", [])] == ["192.168.0.0/24", "192.168.10.0/24"],
          repr(pl.get("options")))
    check("  that re-run share_bridge with the chosen subnet",
          pl.get("action") == "share_bridge" and pl.get("param") == "subnet")
    check("  and the adapter description is on the button",
          any("MediaTek" in o["label"] for o in pl.get("options", [])))
    check("  nothing was sent to the plant before the choice", not c.sec.sent)

    # ---- with the answer, the command proceeds ----------------------------
    c = _client_with_two_lans()
    with contextlib.redirect_stdout(io.StringIO()):
        c.cmd_bridge("192.168.10.0/24")
    check("with the subnet given, the bridge is requested from the plant",
          any(b"bridge_up" in f and b"192.168.10.0/24" in f for f in c.sec.sent))

    # ---- Remote Desktop with several computers found ----------------------
    c = _client_with_two_lans()
    c._server_lans = lambda: [{"cidr": "192.168.0.0/24"}]
    c._await = lambda op, t: {"found": [{"ip": "192.168.0.10", "ports": [44818], "rtt_ms": 3},
                                        {"ip": "192.168.0.20", "ports": [3389], "rtt_ms": 5}]}
    got = None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            c._pick_controller()
    except T.NeedChoice as ch:
        got = ch
    pl = got.payload() if got else {}
    check("Remote Desktop asks which computer, as options",
          got is not None and [o["value"] for o in pl.get("options", [])] == ["192.168.0.10", "192.168.0.20"],
          repr(pl))
    check("  and re-runs remote_desktop with the chosen ip",
          pl.get("action") == "remote_desktop" and pl.get("param") == "ip")

    # ---- the service turns it into a reply, and the page into buttons -----
    with open(os.path.join(HERE, "tnlink_service.py"), encoding="utf-8") as fh:
        svc = fh.read()
    check("the service switches the window mode on", "tnlink.UI_CHOICES = True" in svc)
    check("  and returns a choice instead of failing", "except tnlink.NeedChoice as ch:" in svc
          and 'return {"choice": ch.payload()}' in svc)
    check("  share_bridge and remote_desktop take the answer",
          'def share_bridge(subnet=""):' in svc and 'def remote_desktop(ip=""):' in svc)
    check("  a network with no DHCP asks for an address with a text field",
          '"text_input": {"placeholder": example' in svc and '"bridge_address": bridge_address' in svc)
    check("  only a real code mismatch throws the stored code away",
          'refused = ("does not match" in msg or "code confirmation" in msg)' in svc)
    with open(os.path.join(HERE, "ui", "index.html"), encoding="utf-8") as fh:
        ui = fh.read()
    check("the page has a choice card with Select buttons",
          'id="card-choice"' in ui and "function showChoice(ch)" in ui and 'b.textContent = "Select"' in ui)
    check("  run() shows a choice the action returned", "res.result.choice" in ui)
    check("  and a failed connect is explained on the page, not only in a toast",
          'id="error"' in ui and "s.error" in ui)
    src = open(os.path.join(HERE, "tnlink.py"), encoding="utf-8").read()
    check("the client waits for the plant to re-dial the relay before giving up",
          '"is waiting" in str(exc) and time.time() < deadline' in src)

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
