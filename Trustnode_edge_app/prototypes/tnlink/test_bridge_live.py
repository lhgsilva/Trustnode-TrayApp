# -*- coding: utf-8 -*-
"""Does the bridge forward while the adapter is held open?

The bindings are right - TN-Link matches Ethernet 3 exactly
(ms_implat=True, ms_tcpip=False). But a bridge does not forward to a port
whose media is down, and a TAP adapter only reports connected while a
program holds its device handle open.

So this holds the handle, confirms Windows really sees the adapter as Up
WHILE it is held, and only then sends the ARP. It also prints anything
else arriving on the adapter, because traffic appearing from the plant
network is itself proof the bridge is forwarding, even if the controller
happens not to answer.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tap                 # noqa: E402
import tnlink_bridge as B  # noqa: E402

OUT = os.path.join(HERE, "_live_result.txt")
LOG = []
FAILS = []
PLC = os.environ.get("TNLINK_PLC", "192.168.10.240")
FAKE_IP = "192.168.10.77"
FAKE_MAC = bytes.fromhex("02544e4c0001")


def say(m):
    LOG.append(m)
    print(m, flush=True)


def finish(code):
    io.open(OUT, "w", encoding="utf-8").write(chr(10).join(LOG))
    sys.exit(code)


def _crash(t, e, tb):
    import traceback
    LOG.append("UNHANDLED %s: %s" % (t.__name__, e))
    for ln in traceback.format_tb(tb):
        LOG.extend("  " + x for x in ln.rstrip().splitlines())
    io.open(OUT, "w", encoding="utf-8").write(chr(10).join(LOG))


sys.excepthook = _crash


def check(name, ok, detail=""):
    say("  %-50s: %s%s" % (name, "PASS" if ok else "FAIL",
                           (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)
    return bool(ok)


def ps(cmd):
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-Command", cmd], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True,
                       errors="replace", timeout=60)
    return (r.stdout or "").strip()


def ip2b(a):
    return bytes(int(x) for x in a.split("."))


say("TrustNode Link - does the bridge forward while the port is held open?")
say("")
check("running elevated", tap.is_admin())
if not tap.is_admin():
    finish(2)

guid = ""
for g, nm in tap.list_instances():
    if nm == "TN-Link":
        guid = g
if not check("found TN-Link", bool(guid), guid):
    finish(2)

dev = None
try:
    dev = tap.Tap(guid).open()
    check("opened it", bool(dev.handle))

    st = ""
    for _ in range(30):
        st = ps("(Get-NetAdapter -Name 'TN-Link').Status")
        if st.strip().lower() == "up":
            break
        time.sleep(0.5)
    check("  Windows reports it Up WHILE held open",
          st.strip().lower() == "up",
          "%s - a bridge will not forward to a down port" % st)

    seen = {"frames": 0, "arp_reply": None}
    kinds = {}
    stop = threading.Event()

    def listen():
        while not stop.is_set():
            try:
                f = dev.read(2048, timeout_ms=400)
            except Exception:
                return
            if not f or len(f) < 14:
                continue
            seen["frames"] += 1
            et = int.from_bytes(f[12:14], "big")
            kinds[et] = kinds.get(et, 0) + 1
            if (et == 0x0806 and len(f) >= 42
                    and int.from_bytes(f[20:22], "big") == 2
                    and ".".join(str(b) for b in f[28:32]) == PLC):
                seen["arp_reply"] = ":".join("%02x" % b for b in f[22:28])

    th = threading.Thread(target=listen, daemon=True)
    th.start()

    say("")
    say("[listening for anything the bridge sends us]")
    time.sleep(6)
    say("      %d frame(s) arrived in 6s" % seen["frames"])
    for et, n in sorted(kinds.items(), key=lambda kv: -kv[1])[:5]:
        say("        ethertype 0x%04x  x%d" % (et, n))
    check("the bridge is sending us the segment's traffic",
          seen["frames"] > 0,
          "nothing at all - the bridge is not forwarding to this port")

    say("")
    say("[asking who has %s]" % PLC)
    req = (b"\xff" * 6 + FAKE_MAC + b"\x08\x06"
           + b"\x00\x01\x08\x00\x06\x04\x00\x01"
           + FAKE_MAC + ip2b(FAKE_IP) + b"\x00" * 6 + ip2b(PLC)
           + b"\x00" * 18)
    deadline = time.time() + 12
    tries = 0
    while time.time() < deadline and not seen["arp_reply"]:
        dev.write(req)
        tries += 1
        time.sleep(1.0)
    stop.set()
    th.join(timeout=3)
    if seen["arp_reply"]:
        say("      reply: %s is at %s" % (PLC, seen["arp_reply"]))
    check("the controller answered through the bridge",
          bool(seen["arp_reply"]), "%d request(s) sent" % tries)

finally:
    try:
        if dev:
            dev.close()
    except Exception:
        pass
    ok = ps("if (Test-Connection %s -Count 2 -Quiet -ErrorAction "
            "SilentlyContinue) { 'ok' } else { 'LOST' }" % PLC)
    check("the plant PC still reaches the PLC", ok.strip() == "ok", ok)

say("")
say("RESULT: %s" % ("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
