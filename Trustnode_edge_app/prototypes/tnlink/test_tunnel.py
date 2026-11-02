# -*- coding: utf-8 -*-
"""The LS-Link tunnel: framing, conflict refusal, and the agent's end.

The agent's end is the part that can hurt a machine - it enables IP
forwarding and creates a NAT rule - so it is brought up and torn down
here on its own, and the machine is checked afterwards for residue.

What a single machine CAN prove:
  * raw IP packets survive the frame type added to the session protocol,
  * a subnet this PC is already on is REFUSED, not routed (the trap a VM
    on 192.168.10.x falls into),
  * the agent's adapter, forwarding flag and NAT rule all appear...
  * ...and are ALL gone afterwards, with the internet still working.

What it cannot: carry traffic end to end. That needs the two PCs.

Elevated parts are skipped when not run as administrator.
"""
from __future__ import annotations

import io
import ipaddress
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tnlink as T          # noqa: E402
import tnlink_tun as TT     # noqa: E402
import wintun               # noqa: E402

OUT = os.path.join(HERE, "_tunnel_result.txt")
LOG = []
FAILS = []


def say(m):
    LOG.append(m)
    print(m, flush=True)


def finish(code):
    io.open(OUT, "w", encoding="utf-8").write("\n".join(LOG) + "\n")
    sys.exit(code)


def check(name, ok, detail=""):
    say("  %-52s: %s%s" % (name, "PASS" if ok else "FAIL",
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


say("LS-Link - the tunnel")
say("")

# --------------------------------------------------------------------------
say("[1. a raw IP packet survives the session framing]")
pkt = (bytes([0x45, 0x00]) + (40).to_bytes(2, "big") + b"\x00\x00\x40\x00"
       + bytes([64, 6, 0, 0]) + bytes([192, 168, 10, 5])
       + bytes([192, 168, 10, 240]) + b"\xc0\x00\xaf\x12" + b"\x00" * 12)
frame = T.pack_tun(pkt)
kind, a, b = T.parse(frame)
check("it comes back as a TUN frame", kind == T.TUN, kind)
check("  byte-identical", b == pkt, "%d vs %d bytes" % (len(b), len(pkt)))
check("  TUN is distinct from CTRL and DATA",
      len({T.CTRL, T.DATA, T.TUN}) == 3)
say("      reads as: %s" % TT.describe_packet(pkt))
check("  and a packet can be described for the log",
      "192.168.10.240" in TT.describe_packet(pkt))

# --------------------------------------------------------------------------
say("")
say("[2. a subnet this PC is already on is REFUSED]")
mine = TT.local_prefixes()
say("      this PC is on: %s" % ", ".join(mine))
check("this PC's own subnets were found", bool(mine))
if mine:
    bad = TT.conflicts([mine[0]])
    check("routing one of them is refused", bool(bad), bad)
    check("  and it names both sides", bad and len(bad[0]) == 2, bad)
free = "10.88.0.0/24"
while any(ipaddress.ip_network(free).overlaps(ipaddress.ip_network(m))
          for m in mine):
    free = "10.%d.0.0/24" % (int(free.split(".")[1]) + 1)
check("  a subnet it is NOT on is allowed", not TT.conflicts([free]), free)

# --------------------------------------------------------------------------
say("")
say("[3. the adapter on the plant network is identified]")
plant = None
for m in mine:
    if ipaddress.ip_network(m).num_addresses > 2 and m.startswith("192.168"):
        idx = TT.lan_interface_for(m)
        if idx:
            nm = ps("(Get-NetAdapter -InterfaceIndex %d).Name" % idx)
            say("      %s -> ifIndex %d (%s)" % (m, idx, nm))
            plant = (m, idx)
check("a LAN adapter was matched to a subnet", bool(plant), plant)
check("  and an unrelated subnet matches nothing",
      TT.lan_interface_for("203.0.113.0/24") == 0)

# --------------------------------------------------------------------------
say("")
if not wintun.is_admin():
    say("[4. the agent's end]  SKIPPED - not running as administrator")
    say("")
    say("RESULT: %s" % ("PASS (elevated part skipped)" if not FAILS
                        else "FAIL - " + ", ".join(FAILS)))
    finish(0 if not FAILS else 2)

say("[4. bring the agent's end up - adapter, forwarding, NAT]")
before_nat = ps("(Get-NetNat -ErrorAction SilentlyContinue).Name")
say("      NAT rules before: %s" % (before_nat or "(none)"))
sent = []
ep = TT.Endpoint(TT.AGENT_IP, lambda p: sent.append(p))
try:
    ep.up()
    check("the LS-Link adapter came up", bool(ep.adapter and ep.adapter.index),
          "ifIndex=%d" % (ep.adapter.index if ep.adapter else 0))
    st = ps("(Get-NetIPAddress -InterfaceIndex %d -AddressFamily IPv4 | "
            "Where-Object { $_.IPAddress -eq '%s' }).AddressState"
            % (ep.adapter.index, TT.AGENT_IP))
    check("  its address is Preferred", st.strip().lower() == "preferred", st)

    lan_idx = plant[1] if plant else 0
    if lan_idx:
        ok, why = ep.enable_forwarding(lan_idx)
        say("      forwarding: %s" % why)
        fwd = ps("(Get-NetIPInterface -InterfaceIndex %d -AddressFamily "
                 "IPv4).Forwarding" % ep.adapter.index)
        check("  the tunnel adapter forwards", fwd.strip().lower() == "enabled",
              fwd)

    ok, why = ep.enable_nat()
    check("the NAT rule was created", ok, why)
    nat = ps("(Get-NetNat -Name '%s' -ErrorAction SilentlyContinue)."
             "InternalIPInterfaceAddressPrefix" % TT.NAT_NAME)
    check("  Windows reports it for the tunnel subnet",
          nat.strip() == TT.TUNNEL_CIDR, nat or "(absent)")
finally:
    say("")
    say("[5. tear it down and check for residue]")
    ep.down()
    time.sleep(1.5)

gone_ad = ps("if (Get-NetAdapter -Name 'LS-Link' -ErrorAction "
             "SilentlyContinue) { 'present' } else { 'gone' }")
check("the adapter is gone", gone_ad == "gone", gone_ad)
gone_nat = ps("if (Get-NetNat -Name '%s' -ErrorAction SilentlyContinue) "
              "{ 'present' } else { 'gone' }" % TT.NAT_NAME)
check("  the NAT rule is gone", gone_nat == "gone", gone_nat)
after_nat = ps("(Get-NetNat -ErrorAction SilentlyContinue).Name")
check("  no OTHER NAT rule was disturbed",
      (after_nat or "") == (before_nat or ""),
      "before=%r after=%r" % (before_nat, after_nat))
net = ps("if (Test-Connection 8.8.8.8 -Count 1 -Quiet -ErrorAction "
         "SilentlyContinue) { 'ok' } else { 'BROKEN' }")
check("  this machine still has internet", net == "ok", net)

say("")
say("RESULT: %s" % ("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
