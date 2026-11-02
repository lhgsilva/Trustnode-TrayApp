# -*- coding: utf-8 -*-
"""Does the bridge actually carry our frames onto the plant network?

The bridge now has both members:

    bridge  : Network Bridge
    members : LS-Link, Ethernet 3

That is the configuration. This is the proof. An ARP request is written
into the LS-Link adapter exactly as the remote PC's frames will arrive,
and the PLC's own reply has to come back out of it:

    who-has 192.168.10.240  tell 192.168.10.77
    -> ARP reply from the controller's real MAC

ARP is the right question. It is pure Layer 2, it cannot be faked by
routing or NAT, and it is precisely what every previous approach failed
to deliver. If the controller answers here, any device on that network
is reachable by any protocol - which is what "share the network" means.

Nothing is reconfigured. One ARP request is sent.
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

import tap              # noqa: E402
import tnlink_bridge as B   # noqa: E402

OUT = os.path.join(HERE, "_reach_result.txt")
LOG = []
FAILS = []

PLANT = os.environ.get("TNLINK_PLANT", "192.168.10.0/24")
PLC = os.environ.get("TNLINK_PLC", "192.168.10.240")
FAKE_IP = os.environ.get("TNLINK_FAKE", "192.168.10.77")
FAKE_MAC = bytes.fromhex("02544e4c0001")


def say(m):
    LOG.append(m)
    print(m, flush=True)


def finish(code):
    io.open(OUT, "w", encoding="utf-8").write("\n".join(LOG) + "\n")
    sys.exit(code)


def check(name, ok, detail=""):
    say("  %-50s: %s%s" % (name, "PASS" if ok else "FAIL",
                           (" - " + str(detail)[:180]) if detail else ""))
    if not ok:
        FAILS.append(name)
    return bool(ok)


def ip2b(a):
    return bytes(int(x) for x in a.split("."))


def arp_request(target_ip, src_ip, src_mac):
    return (b"\xff" * 6 + src_mac + b"\x08\x06"
            + b"\x00\x01\x08\x00\x06\x04\x00\x01"
            + src_mac + ip2b(src_ip)
            + b"\x00" * 6 + ip2b(target_ip)
            + b"\x00" * 18)


def _crash(exc_type, exc, tb):
    """Write the traceback where it can be read.

    An elevated process gets its own console that vanishes, and
    Start-Process -Verb RunAs cannot redirect output, so an exception
    left nothing behind but "exit code 1".
    """
    import traceback
    LOG.append("")
    LOG.append("UNHANDLED %s: %s" % (exc_type.__name__, exc))
    for ln in traceback.format_tb(tb):
        for sub in ln.rstrip().splitlines():
            LOG.append("  " + sub)
    try:
        io.open(OUT, "w", encoding="utf-8").write(chr(10).join(LOG))
    except Exception:
        pass


sys.excepthook = _crash

say("LS-Link - does the bridge reach the plant network?")
say("  asking:  who-has %s   tell %s" % (PLC, FAKE_IP))
say("")
check("running elevated", tap.is_admin())
if not tap.is_admin():
    finish(2)

st = B.bridge_state("LS-Link")
say("  bridge  : %s" % (st["bridge"] or "(none)"))
say("  members : %s" % (", ".join(st["members"]) or "(none)"))
if not check("LS-Link is a bridge member", st["ready"],
             "the bridge must contain LS-Link and the plant adapter"):
    finish(2)
check("  the plant adapter is in it too",
      any(m != "LS-Link" for m in st["members"]), st["members"])

plant_ip = B.plant_address(PLANT)
say("  plant address: %s" % (plant_ip or "(none)"))
check("the plant PC still holds an address on that network",
      bool(plant_ip), "without it the plant PC itself is blind")

dev = None
try:
    say("")
    say("[open LS-Link and listen]")
    guid = ""
    for g, nm in tap.list_instances():
        if nm == "LS-Link":
            guid = g
            break
    if not check("found the LS-Link adapter", bool(guid), guid):
        finish(2)
    dev = tap.Tap(guid).open()
    check("opened it, media up", bool(dev.handle))

    got = {}
    stop = threading.Event()

    def listen():
        while not stop.is_set():
            try:
                f = dev.read(2048)
                if not f:
                    continue
            except Exception:
                return
            if len(f) < 42:
                continue
            if int.from_bytes(f[12:14], "big") != 0x0806:
                continue
            if int.from_bytes(f[20:22], "big") != 2:        # reply
                continue
            spa = ".".join(str(b) for b in f[28:32])
            if spa == PLC:
                got["mac"] = ":".join("%02x" % b for b in f[22:28])
                got["frame"] = f
                return

    th = threading.Thread(target=listen, daemon=True)
    th.start()
    time.sleep(0.6)

    say("")
    say("[send the ARP request into the bridge]")
    req = arp_request(PLC, FAKE_IP, FAKE_MAC)
    tries = 0
    deadline = time.time() + 15
    while time.time() < deadline and not got.get("frame"):
        dev.write(req)
        tries += 1
        time.sleep(1.2)
    stop.set()
    th.join(timeout=3)

    if got.get("frame"):
        say("      reply from %s at %s" % (PLC, got["mac"]))
    check("the controller answered our ARP through the bridge",
          bool(got.get("frame")),
          "%d request(s) sent, no reply - the bridge is configured but "
          "is not carrying frames" % tries)

    # The real MAC, for comparison with what the plant PC itself sees.
    ok, out = B._ps("(Get-NetNeighbor -IPAddress '%s' -ErrorAction "
                    "SilentlyContinue | Where-Object { $_.State -ne "
                    "'Unreachable' } | Select-Object -First 1)."
                    "LinkLayerAddress" % PLC)
    real = (out or "").strip().lower().replace("-", ":")
    if got.get("mac") and real:
        check("  and it is the controller's real MAC",
              got["mac"].lower() == real,
              "bridge=%s  plant=%s" % (got["mac"], real))

finally:
    say("")
    try:
        if dev:
            dev.close()
    except Exception:
        pass
    after = B.plant_address(PLANT)
    check("the plant address is still intact", bool(after),
          after or "LOST - the plant PC cannot reach its own PLC")
    ok, plc = B._ps("if (Test-Connection %s -Count 2 -Quiet -ErrorAction "
                    "SilentlyContinue) { 'ok' } else { 'LOST' }" % PLC)
    check("  and the plant PC still reaches the PLC", plc.strip() == "ok",
          plc)

say("")
say("RESULT: %s" % ("PASS - the bridge carries frames onto the plant network"
                    if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
