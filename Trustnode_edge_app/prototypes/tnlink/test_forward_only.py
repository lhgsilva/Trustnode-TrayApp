# -*- coding: utf-8 -*-
"""Does the PLC need NAT at all, or is plain IP forwarding enough?

Every NAT attempt so far assumed the PLC cannot reply to a foreign
subnet, because it has no default gateway. That was an ASSUMPTION and it
was never tested. If the PLC does have a gateway - and on this network
the gateway candidates are this very PC - then replies for the tunnel
subnet come back here on their own and no NAT, no Hyper-V and no ICS are
needed at all.

So: a TN-Link adapter, IP forwarding (already on), and one packet
injected exactly as the tunnel would deliver it:

    ICMP echo   src 10.77.0.2   dst <the PLC>

A reply means the whole NAT problem was imaginary.

Also tries the PLC's own subnet as the source, which tells us whether a
tunnel address inside 192.168.10.0/24 would work - the shape the operator
originally asked for.

Removes the adapter afterwards. Changes no address on any real adapter.
"""
from __future__ import annotations

import io
import os
import socket
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import wintun               # noqa: E402
import tnlink_tun as TT     # noqa: E402

OUT = os.path.join(HERE, "_fwd_result.txt")
LOG = []
FAILS = []

PLANT_CIDR = os.environ.get("TNLINK_PLANT", "192.168.10.0/24")
PLC = os.environ.get("TNLINK_PLC", "192.168.10.240")


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


def ps(cmd, timeout=60):
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-Command", cmd], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True,
                       errors="replace", timeout=timeout)
    return (r.stdout or "").strip()


def csum(data):
    if len(data) % 2:
        data += b"\x00"
    s = 0
    for i in range(0, len(data), 2):
        s += (data[i] << 8) | data[i + 1]
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def echo_packet(src, dst, ident):
    payload = b"trustnode-link-fwd"
    h = struct.pack(">BBHHH", 8, 0, 0, ident, 1)
    c = csum(h + payload)
    icmp = struct.pack(">BBHHH", 8, 0, c, ident, 1) + payload
    hdr = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(icmp), 0x3344,
                      0, 64, 1, 0, socket.inet_aton(src),
                      socket.inet_aton(dst))
    hdr = hdr[:10] + struct.pack(">H", csum(hdr)) + hdr[12:]
    return hdr + icmp


say("TrustNode Link - is forwarding alone enough?")
say("  PLC: %s" % PLC)
say("")
check("running elevated", wintun.is_admin())
if not wintun.is_admin():
    finish(2)

say("[0. what the PLC's neighbours look like]")
say("      gateway candidates on the plant network:")
for cand in ("192.168.10.1", "192.168.10.110", "192.168.10.254"):
    st = ps("if (Test-Connection %s -Count 1 -Quiet -ErrorAction "
            "SilentlyContinue) { 'answers' } else { 'silent' }" % cand)
    say("        %-16s %s" % (cand, st))

ep = None
try:
    say("")
    say("[1. a TN-Link adapter, forwarding only - NO NAT]")
    ep = TT.Endpoint(TT.AGENT_IP, lambda p: None)
    ep.up()
    check("adapter up at %s" % TT.AGENT_IP, bool(ep.adapter),
          "ifIndex=%d" % ep.adapter.index)
    lan_idx = TT.lan_interface_for(PLANT_CIDR)
    ok, why = ep.enable_forwarding(lan_idx)
    say("      %s" % why)

    for src, label in ((TT.CLIENT_IP, "a separate tunnel subnet"),
                       ("192.168.10.77", "an address INSIDE the plant subnet")):
        say("")
        say("[2. inject  %s -> %s   (%s)]" % (src, PLC, label))
        got = None
        tries = 0
        deadline = time.time() + 10
        last = 0.0
        while time.time() < deadline:
            if time.time() - last > 2.0:
                ep.session.send(echo_packet(src, PLC, 0x5151))
                last = time.time()
                tries += 1
            rx = ep.session.recv(250)
            if rx is None:
                continue
            if len(rx) >= 28 and rx[9] == 1 and rx[20] == 0 and \
                    socket.inet_ntoa(rx[12:16]) == PLC and \
                    socket.inet_ntoa(rx[16:20]) == src:
                got = rx
                break
        if got:
            say("      REPLY: %s" % TT.describe_packet(got))
        check("  the PLC replied to %s" % src, got is not None,
              "%d echo(es), no reply" % tries)

finally:
    say("")
    say("[3. remove it]")
    if ep:
        ep.down()
    time.sleep(1.5)
    left = ps("if (Get-NetAdapter -Name 'TN-Link' -ErrorAction "
              "SilentlyContinue) { 'present' } else { 'gone' }")
    check("the adapter is gone", left == "gone", left)
    net = ps("if (Test-Connection 8.8.8.8 -Count 1 -Quiet -ErrorAction "
             "SilentlyContinue) { 'ok' } else { 'BROKEN' }")
    check("  internet still fine", net == "ok", net)

say("")
say("RESULT: %s" % ("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
