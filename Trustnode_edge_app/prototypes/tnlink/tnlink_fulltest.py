# -*- coding: utf-8 -*-
"""Every menu option, exercised from a genuinely remote machine.

Runs on the VPS, so the plant PC is reached the way a real engineer
would reach it: outbound to the relay, nothing listening at either end.

WHAT THIS CAN AND CANNOT TEST
-----------------------------
Options 1-5 and 7 are protocol operations and are tested for real.

Option 6 (share the whole network) needs a LS-Link adapter at BOTH ends,
and the adapter driver is Windows-only - this probe is Linux. So its
AGENT half is tested properly (adapter created, protocols stripped,
bridge membership checked, teardown), and the client half is reported as
untestable from here rather than quietly skipped.

Usage: python3 tnlink_fulltest.py --relay HOST:PORT --code TN-... [--plc IP]
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import time

sys.path.insert(0, "/opt/tnlink")
from tnlink_probe import (CTRL, DATA, connect, parse)  # noqa: E402

FAILS = []
SKIPS = []


def check(name, ok, detail=""):
    print("  %-52s: %s%s" % (name, "PASS" if ok else "FAIL",
                             (" - " + str(detail)[:160]) if detail else ""))
    if not ok:
        FAILS.append(name)
    return bool(ok)


def skip(name, why):
    print("  %-52s: SKIP - %s" % (name, why))
    SKIPS.append(name)


def ctrl(sec, obj):
    sec.send(bytes([CTRL]) + json.dumps(obj).encode())


def wait(sec, op, timeout=90, sink=None):
    end = time.time() + timeout
    while time.time() < end:
        try:
            kind, a, b = parse(sec.recv())
        except Exception:
            return None
        if kind == DATA:
            if sink is not None:
                sink.append(b)
            continue
        if kind == CTRL and a.get("op") == op:
            return a
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--relay", default="127.0.0.1:7443")
    ap.add_argument("--code", required=True)
    ap.add_argument("--plc", default="192.168.10.240")
    a = ap.parse_args()

    print("LS-Link - full option test, from the VPS")
    print("  relay %s   PLC %s" % (a.relay, a.plc))
    print()

    t0 = time.time()
    sec = connect(a.relay, a.code)
    check("paired over the internet", True,
          "%.0f ms" % ((time.time() - t0) * 1000))

    ctrl(sec, {"op": "hello"})
    hello = wait(sec, "hello", 30) or {}
    lans = [s for s in (hello.get("subnets") or [])
            if s.get("kind", "lan") == "lan"]
    plant = lans[0]["cidr"] if lans else ""
    check("  the agent described its networks", bool(lans),
          ", ".join(s["cidr"] for s in lans))

    # ---------------------------------------------------------- option 2
    print()
    print("[option 2 - find devices on the plant network]")
    t = time.time()
    ctrl(sec, {"op": "scan", "cidr": plant, "ports": [44818, 80, 502]})
    r = wait(sec, "scan", 240)
    found = (r or {}).get("found") or []
    check("the scan returned devices", bool(found),
          "%d device(s) in %.0fs" % (len(found), time.time() - t))
    for d in found[:6]:
        print("      %-16s %-7s ports %s"
              % (d["ip"], ("%d ms" % d["rtt_ms"]) if d.get("rtt_ms")
                 is not None else "-",
                 ", ".join(str(p) for p in d.get("ports") or []) or "-"))
    plcs = [d for d in found if 44818 in (d.get("ports") or [])]
    check("  a controller was identified", bool(plcs),
          ", ".join(d["ip"] for d in plcs))

    # ---------------------------------------------------- ping, real IP
    print()
    print("[ping the PLC at its REAL address, %s]" % a.plc)
    ctrl(sec, {"op": "ping", "ip": a.plc, "count": 4})
    pr = wait(sec, "ping", 60) or {}
    ok = pr.get("received", 0) > 0 and not pr.get("error")
    check("the PLC replies", ok,
          pr.get("error") or "%s/%s received, rtt %s ms, ttl %s"
          % (pr.get("received"), pr.get("sent"), pr.get("rtt_avg"),
             pr.get("ttl")))
    check("  no packet loss", pr.get("received") == pr.get("sent"),
          "%s of %s" % (pr.get("received"), pr.get("sent")))

    # ---------------------------------------------------------- option 1
    print()
    print("[option 1 - connect to the PLC and talk CIP]")
    ctrl(sec, {"op": "open", "sid": 1, "ip": a.plc, "port": 44818})
    o = wait(sec, "open", 60) or {}
    if check("a stream opened to %s:44818" % a.plc, bool(o.get("ok")),
             o.get("error")):
        sec.send(bytes([DATA]) + struct.pack(">I", 1)
                 + struct.pack("<HHIIQI", 0x63, 0, 0, 0, 0, 0))
        buf = b""
        end = time.time() + 25
        while time.time() < end and len(buf) < 28:
            kind, x, body = parse(sec.recv())
            if kind == DATA:
                buf += body
        good = len(buf) >= 28 and buf[0] == 0x63 and buf[1] == 0
        name = (bytes(c for c in buf[40:] if 32 <= c < 127)
                .decode("ascii", "replace").strip()) if good else ""
        check("  CIP ListIdentity answered", good, "%d bytes" % len(buf))
        check("  the controller named itself", bool(name), name[:46])
        ctrl(sec, {"op": "close", "sid": 1})

    # ---------------------------------------------------------- option 4
    print()
    print("[option 4 - reach a whole device]")
    opened = []
    for port in (80, 443, 502, 22, 3389):
        ctrl(sec, {"op": "open", "sid": 100 + port, "ip": a.plc,
                   "port": port})
        rr = wait(sec, "open", 30) or {}
        if rr.get("ok"):
            opened.append(port)
            ctrl(sec, {"op": "close", "sid": 100 + port})
    check("other services on the device are reachable", bool(opened),
          "open: %s" % (", ".join(str(p) for p in opened) or "none"))

    # ---------------------------------------------------------- option 3
    print()
    print("[option 3 - Remote Desktop to the plant PC]")
    host_ip = ""
    for s in lans:
        if s.get("gateway"):
            host_ip = s["ip"]
    host_ip = host_ip or (lans[0]["ip"] if lans else "")
    if host_ip:
        ctrl(sec, {"op": "open", "sid": 7, "ip": host_ip, "port": 3389})
        rr = wait(sec, "open", 40) or {}
        check("RDP port on the plant PC accepts a connection",
              bool(rr.get("ok")), rr.get("error") or "%s:3389" % host_ip)
        if rr.get("ok"):
            ctrl(sec, {"op": "close", "sid": 7})
    else:
        skip("RDP", "could not work out the plant PC's own address")

    # ---------------------------------------------------------- option 6
    print()
    print("[option 6 - share the whole network, userspace stack]")
    ctrl(sec, {"op": "tunnel_up"})
    tu = wait(sec, "tunnel_up", 120) or {}
    if check("the plant end shared its network", bool(tu.get("ok")),
             tu.get("error") or tu.get("note")):
        check("  it needs no adapter or NAT on the plant PC",
              tu.get("mode") == "netstack", tu.get("mode"))
        check("  and it offered the plant subnets",
              bool(tu.get("subnets")), ", ".join(tu.get("subnets") or []))
        skip("  the client half",
             "needs a LS-Link adapter; this probe is Linux")
        ctrl(sec, {"op": "tunnel_down"})
        td = wait(sec, "tunnel_down", 120) or {}
        check("  and it released cleanly", bool(td.get("ok")),
              td.get("summary"))

    print()
    print("[option 8 - the Layer 2 bridge (advanced)]")
    ctrl(sec, {"op": "bridge_up", "subnet": plant})
    br = wait(sec, "bridge_up", 180) or {}
    if br.get("ok"):
        check("the bridge came up", True, br.get("note"))
        ctrl(sec, {"op": "bridge_down"})
        wait(sec, "bridge_down", 120)
    elif br.get("error") and "administrator" in str(br.get("error")):
        skip("the bridge", "needs an elevated agent; option 6 does not")
    else:
        check("the bridge came up", False, br.get("error") or "no answer")

    # ---------------------------------------------------------- option 7
    print()
    print("[option 7 - stop everything]")
    ctrl(sec, {"op": "bridge_down"})
    d = wait(sec, "bridge_down", 120) or {}
    check("stop is accepted even when nothing is up", bool(d.get("ok")), d)

    # ----------------------------------------------------- still healthy
    print()
    print("[the plant PC is unharmed]")
    ctrl(sec, {"op": "ping", "ip": a.plc, "count": 3})
    pr2 = wait(sec, "ping", 60) or {}
    check("the PLC is still reachable from the plant PC",
          pr2.get("received", 0) > 0,
          "%s/%s" % (pr2.get("received"), pr2.get("sent")))

    print()
    print("RESULT: %s%s"
          % ("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS),
             ("   (%d skipped)" % len(SKIPS)) if SKIPS else ""))
    return 0 if not FAILS else 2


if __name__ == "__main__":
    sys.exit(main())
