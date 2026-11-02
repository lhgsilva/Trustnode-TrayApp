# -*- coding: utf-8 -*-
"""Can the plant PC NAT for us WITHOUT Hyper-V?

New-NetNat needs Hyper-V on Windows 10 Pro, and Hyper-V would break the
VMware the PLC software runs in. But Internet Connection Sharing has done
NAT on every Windows since XP, needs no optional feature and no reboot.

This is the decisive test, and it is the real data path: a TN-Link adapter
is created, ICS is switched on with the plant NIC as "public" and TN-Link
as "private", and then a packet is INJECTED into TN-Link exactly as the
tunnel would inject one arriving from the remote engineer:

    ICMP echo   src 192.168.137.2 (the far end of the tunnel)
                dst <the PLC>

If a reply comes back out of the adapter, NAT works, the PLC needs no
changes, and the design stands. If nothing comes back, ICS is not the
answer and we stop pretending it is.

Everything is undone at the end: sharing off, adapter removed. The PLC is
only pinged.
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

OUT = os.path.join(HERE, "_ics_result.txt")
LOG = []
FAILS = []

PLANT_CIDR = os.environ.get("TNLINK_PLANT", "192.168.10.0/24")
PLC = os.environ.get("TNLINK_PLC", "192.168.10.240")
ICS_PRIVATE = "192.168.137.1"      # ICS always forces this
FAR_END = "192.168.137.2"


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


def ps(cmd, timeout=90):
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-Command", cmd], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True,
                       errors="replace", timeout=timeout)
    return (r.stdout or "").strip()


SHARE = r'''
param([string]$Public, [string]$Private, [int]$Mode)
$m = New-Object -ComObject HNetCfg.HNetShare
$found = @()
foreach ($c in $m.EnumEveryConnection) {
  $p = $m.NetConnectionProps($c)
  $found += $p.Name
  if ($p.Name -eq $Public -or $p.Name -eq $Private) {
    $cfg = $m.INetSharingConfigurationForINetConnection($c)
    if ($Mode -eq 0) { $cfg.DisableSharing() }
    elseif ($p.Name -eq $Public)  { $cfg.EnableSharing(0) }
    else                          { $cfg.EnableSharing(1) }
  }
}
"CONNECTIONS:" + ($found -join ",")
'''


def set_sharing(public, private, mode):
    """mode 1 = share (public/private), 0 = turn sharing off on both."""
    f = os.path.join(HERE, "_ics.ps1")
    io.open(f, "w", encoding="utf-8").write(SHARE)
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-File", f,
                        "-Public", public, "-Private", private,
                        "-Mode", str(mode)],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, errors="replace", timeout=120)
    try:
        os.remove(f)
    except OSError:
        pass
    return r.returncode == 0, (r.stdout or "").strip()


def ipv4(src, dst, proto, payload):
    hdr = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(payload),
                      0x1234, 0, 64, proto, 0,
                      socket.inet_aton(src), socket.inet_aton(dst))
    c = csum(hdr)
    hdr = hdr[:10] + struct.pack(">H", c) + hdr[12:]
    return hdr + payload


def csum(data):
    if len(data) % 2:
        data += b"\x00"
    s = 0
    for i in range(0, len(data), 2):
        s += (data[i] << 8) | data[i + 1]
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def icmp_echo(ident, seq, payload=b"trustnode-link"):
    h = struct.pack(">BBHHH", 8, 0, 0, ident, seq)
    c = csum(h + payload)
    return struct.pack(">BBHHH", 8, 0, c, ident, seq) + payload


say("TrustNode Link - NAT without Hyper-V, via Internet Connection Sharing")
say("  plant network : %s" % PLANT_CIDR)
say("  PLC           : %s" % PLC)
say("")

check("running elevated", wintun.is_admin())
if not wintun.is_admin():
    finish(2)

lan_idx = TT.lan_interface_for(PLANT_CIDR)
lan_name = ps("(Get-NetAdapter -InterfaceIndex %d).Name" % lan_idx) \
    if lan_idx else ""
check("the plant adapter was found", bool(lan_name),
      "ifIndex=%s name=%r" % (lan_idx, lan_name))
if not lan_name:
    finish(2)

say("  plant adapter : %s (ifIndex %d)" % (lan_name, lan_idx))
say("")

ep = None
shared = False
try:
    say("[1. a TN-Link adapter]")
    ep = TT.Endpoint(ICS_PRIVATE, lambda p: None)
    ep.up()
    check("adapter up", bool(ep.adapter), "ifIndex=%d" % ep.adapter.index)

    say("")
    say("[2. switch ICS on: %s -> %s]" % ("TN-Link", lan_name))
    ok, out = set_sharing(lan_name, "TN-Link", 1)
    shared = ok
    check("ICS accepted the configuration", ok, out[-160:] if out else "")
    time.sleep(3)
    addr = ps("(Get-NetIPAddress -InterfaceIndex %d -AddressFamily IPv4 "
              "-ErrorAction SilentlyContinue).IPAddress -join ','"
              % ep.adapter.index)
    say("      TN-Link now holds: %s" % (addr or "(none)"))
    check("  ICS put its address on TN-Link",
          ICS_PRIVATE in (addr or ""),
          "ICS always uses %s - that becomes the tunnel subnet"
          % ICS_PRIVATE)
    # Enabling sharing does not reliably start the service, and without
    # it nothing is translated - the adapter carries traffic but no NAT
    # happens, which is exactly what the first run showed.
    st = ps("(Get-Service SharedAccess).StartType")
    if st.strip().lower() == "disabled":
        say("      SharedAccess was Disabled - setting it to Manual")
        ps("Set-Service SharedAccess -StartupType Manual")
    ps("Start-Service SharedAccess -ErrorAction SilentlyContinue")
    svc = ""
    for _ in range(20):
        svc = ps("(Get-Service SharedAccess).Status")
        if svc.strip() == "Running":
            break
        time.sleep(0.5)
    check("  the ICS service is running", svc.strip() == "Running",
          "StartType was %s" % st)
    if svc.strip() == "Running":
        time.sleep(3)

    say("")
    say("[3. THE REAL TEST - inject a packet as the tunnel would]")
    say("      ICMP echo  %s -> %s" % (FAR_END, PLC))
    pkt = ipv4(FAR_END, PLC, 1, icmp_echo(0x4242, 1))
    got = None
    deadline = time.time() + 12
    ep.session.send(pkt)
    sent_at = time.time()
    tries = 1
    while time.time() < deadline:
        rx = ep.session.recv(250)
        if rx is None:
            if time.time() - sent_at > 2.0 and tries < 5:
                ep.session.send(pkt)
                sent_at = time.time()
                tries += 1
            continue
        if len(rx) >= 28 and rx[9] == 1 and rx[20] == 0:
            if socket.inet_ntoa(rx[12:16]) == PLC and \
                    socket.inet_ntoa(rx[16:20]) == FAR_END:
                got = rx
                break
        say("      (saw %s)" % TT.describe_packet(rx))
    if got is not None:
        say("      REPLY: %s" % TT.describe_packet(got))
    check("the PLC answered through the NAT", got is not None,
          "%d echo(es) sent; no reply means ICS is not the answer" % tries)

finally:
    say("")
    say("[4. undo everything]")
    if shared:
        ok, out = set_sharing(lan_name, "TN-Link", 0)
        check("ICS sharing switched off", ok, out[-120:] if out else "")
    if ep:
        ep.down()
    time.sleep(2)
    left = ps("if (Get-NetAdapter -Name 'TN-Link' -ErrorAction "
              "SilentlyContinue) { 'present' } else { 'gone' }")
    check("  the adapter is gone", left == "gone", left)
    lan_ip = ps("(Get-NetIPAddress -InterfaceIndex %d -AddressFamily IPv4 "
                "-ErrorAction SilentlyContinue).IPAddress -join ','"
                % lan_idx)
    check("  the plant adapter still has its address", bool(lan_ip), lan_ip)
    net = ps("if (Test-Connection 8.8.8.8 -Count 1 -Quiet -ErrorAction "
             "SilentlyContinue) { 'ok' } else { 'BROKEN' }")
    check("  this machine still has internet", net == "ok", net)

say("")
say("RESULT: %s" % ("PASS - ICS gives us NAT, no Hyper-V needed"
                    if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
