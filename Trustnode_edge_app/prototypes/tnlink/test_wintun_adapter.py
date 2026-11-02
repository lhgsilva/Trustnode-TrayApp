# -*- coding: utf-8 -*-
"""The LS-Link adapter, on its own, before any tunnel is wired to it.

Proves the four things the whole design rests on:

  1. a real Windows network adapter can be created and is called LS-Link -
     no driver vendor's name anywhere a user would look,
  2. an address can be put on it,
  3. packets Windows sends INTO it arrive in this program (receive path),
  4. packets this program sends arrive back in Windows (send path) -
     checked by answering Windows' own ping.exe and watching it succeed.

If (3) and (4) work, no userspace TCP stack is needed: Windows does all
the TCP at both ends and the tunnel only moves IP packets.

Creates its own adapter and removes it again. Touches no existing adapter,
no route outside its own /24, and no NAT.

Needs administrator rights.
"""
from __future__ import annotations

import io
import os
import struct
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import wintun  # noqa: E402

OUT = os.path.join(HERE, "_wintun_result.txt")
LOG = []
FAILS = []

ADAPTER = "LS-Link"
OURS = "10.77.0.1"
PEER = "10.77.0.2"
PREFIX = 24


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
                       errors="replace", timeout=40)
    return (r.stdout or "").strip()


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    s = 0
    for i in range(0, len(data), 2):
        s += (data[i] << 8) | data[i + 1]
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def icmp_echo_reply(pkt: bytes):
    """Turn an IPv4 ICMP echo request into its reply, or None."""
    if len(pkt) < 20 or (pkt[0] >> 4) != 4:
        return None
    ihl = (pkt[0] & 0x0F) * 4
    if pkt[9] != 1 or len(pkt) < ihl + 8:
        return None
    if pkt[ihl] != 8:                      # not an echo request
        return None
    ip = bytearray(pkt[:ihl])
    ip[12:16], ip[16:20] = pkt[16:20], pkt[12:16]     # swap src/dst
    ip[10:12] = b"\x00\x00"
    ip[10:12] = struct.pack(">H", checksum(bytes(ip)))
    icmp = bytearray(pkt[ihl:])
    icmp[0] = 0                            # echo reply
    icmp[2:4] = b"\x00\x00"
    icmp[2:4] = struct.pack(">H", checksum(bytes(icmp)))
    return bytes(ip) + bytes(icmp)


say("LS-Link - the LS-Link adapter, standalone")
say("  adapter : %s" % ADAPTER)
say("  address : %s/%d   (peer %s)" % (OURS, PREFIX, PEER))
say("")

check("running elevated", wintun.is_admin())
if not wintun.is_admin():
    finish(2)

ad = wintun.Adapter(ADAPTER, ADAPTER)
sess = None
try:
    say("[1. create it]")
    t0 = time.time()
    ad.create()
    check("the adapter was created", bool(ad.handle),
          "%.1fs, ifIndex=%d, driver %s"
          % (time.time() - t0, ad.index, wintun.driver_version()))
    check("  Windows gave it an interface index", ad.index > 0, ad.index)

    info = ps("$a = Get-NetAdapter -Name '%s' -ErrorAction SilentlyContinue;"
              "if ($a) { $a.Name + '|' + $a.InterfaceDescription + '|' +"
              " $a.Status } else { 'absent' }" % ADAPTER)
    say("      Windows reports: %s" % info)
    check("  it appears in Windows under our name",
          info.split("|")[0] == ADAPTER, info)
    low = info.lower()
    check("  no driver vendor name is shown to the user",
          "wireguard" not in low and "wintun" not in low,
          "the licence also forbids using their name to promote this")

    say("")
    say("[2. open the packet ring FIRST]")
    sess = wintun.Session(ad).start()
    check("the session started", bool(sess.handle))
    for _ in range(20):
        st = ps("(Get-NetAdapter -Name '%s').Status" % ADAPTER)
        if st.strip().lower() == "up":
            break
        time.sleep(0.25)
    check("  the adapter now reports Up, not Disconnected",
          st.strip().lower() == "up", st)

    say("")
    say("[3. give it an address]")
    ok, why = ad.set_ip(OURS, PREFIX)
    check("the address was set", ok, why)
    ad.set_mtu(1380)
    for _ in range(40):
        got = ps("(Get-NetIPAddress -InterfaceIndex %d -AddressFamily IPv4 "
                 "-ErrorAction SilentlyContinue |"
                 " Where-Object { $_.IPAddress -eq '%s' }).AddressState"
                 % (ad.index, OURS))
        if got.strip().lower() == "preferred":
            break
        time.sleep(0.5)
    check("  Windows accepted it (Preferred, not Tentative)",
          got.strip().lower() == "preferred", got or "(no state)")

    import socket as _sk
    bound, berr = False, ""
    for _ in range(20):
        _s = _sk.socket(_sk.AF_INET, _sk.SOCK_STREAM)
        try:
            _s.bind((OURS, 0))
            bound = True
        except OSError as exc:
            berr = exc.strerror or str(exc)
        finally:
            _s.close()
        if bound:
            break
        time.sleep(0.25)
    check("  a TCP socket can BIND to it", bound,
          berr or "a Tentative address refuses this, which is what broke "
                  "the earlier approach")

    say("")
    say("[4. answer Windows' own ping through the adapter]")
    seen = {"req": 0, "rep": 0}
    stop = threading.Event()

    def pump():
        while not stop.is_set():
            try:
                pkt = sess.recv(200)
            except Exception as exc:                        # noqa: BLE001
                seen["err"] = str(exc)
                return
            if not pkt:
                continue
            rep = icmp_echo_reply(pkt)
            if rep:
                seen["req"] += 1
                sess.send(rep)
                seen["rep"] += 1

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    time.sleep(0.5)

    r = subprocess.run(["ping", "-n", "4", "-w", "2000", PEER],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, errors="replace", timeout=60)
    stop.set()
    th.join(timeout=3)
    tail = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    for ln in tail[-4:]:
        say("      %s" % ln)
    check("packets Windows sent reached this program",
          seen["req"] > 0, "%d echo request(s) received" % seen["req"])
    check("  and our replies reached Windows",
          r.returncode == 0 and seen["rep"] > 0,
          "ping exit %d, %d reply(ies) sent" % (r.returncode, seen["rep"]))
    if seen.get("err"):
        say("      receive error: %s" % seen["err"])

finally:
    say("")
    say("[5. remove it again]")
    try:
        if sess:
            sess.end()
    except Exception:
        pass
    try:
        ad.close()
    except Exception:
        pass
    time.sleep(1.5)
    left = ps("if (Get-NetAdapter -Name '%s' -ErrorAction SilentlyContinue)"
              " { 'present' } else { 'gone' }" % ADAPTER)
    check("the adapter is gone", left == "gone", left)

say("")
say("RESULT: %s" % ("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
