# -*- coding: utf-8 -*-
"""The LS-Link Ethernet adapter, standalone - the piece that can be BRIDGED.

Proves, before any bridge or tunnel is involved:

  1. the signed driver installs and an adapter instance appears,
  2. the connection is called LS-Link in Network Connections,
  3. the device opens and reports media CONNECTED (without that the
     adapter reads "cable unplugged" and Windows will not bridge it),
  4. Ethernet frames arrive from Windows - including ARP, which is the
     whole reason for going to Layer 2,
  5. frames we write are accepted by Windows - checked by answering its
     ARP and then its ping, and watching ping.exe succeed.

If ARP works here, the remote PC really can sit on the plant segment and
the PLC will answer it without NAT, routes, or any change to the PLC.

Removes the adapter instance afterwards. The driver stays in the Windows
driver store (that is what --keep-driver controls).
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

import tap  # noqa: E402

OUT = os.path.join(HERE, "_tap_result.txt")
LOG = []
FAILS = []

OURS = "10.66.0.1"
PEER = "10.66.0.2"
PEER_MAC = bytes.fromhex("02aabbccdd02")
PREFIX = 24


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


def ps(cmd, timeout=120):
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


def ip2b(a):
    return bytes(int(x) for x in a.split("."))


say("LS-Link - the LS-Link Ethernet (L2) adapter")
say("")
check("running elevated", tap.is_admin())
if not tap.is_admin():
    finish(2)

created = False
dev = None
guid = ""
try:
    say("[1. the signed driver]")
    if tap.driver_installed():
        check("the driver is already in the store", True)
    else:
        ok, why = tap.install_driver()
        check("the driver installed", ok, why)
        if not ok:
            finish(2)

    say("")
    say("[2. an adapter instance]")
    before = {g for g, _ in tap.list_instances()}
    ok, why = tap.create_instance()
    check("tapinstall created one", ok, why)
    time.sleep(3)
    now = tap.list_instances()
    fresh = [g for g, _ in now if g not in before]
    guid = fresh[0] if fresh else (now[0][0] if now else "")
    created = bool(fresh)
    check("  it has a GUID", bool(guid), guid)
    if not guid:
        finish(2)

    ok, why = tap.rename_connection(guid, tap.CONN_NAME)
    time.sleep(1)
    shown = ps("$a = Get-NetAdapter | Where-Object { $_.InterfaceGuid -eq "
               "'%s' }; if ($a) { $a.Name + '|' + $a.InterfaceDescription "
               "+ '|' + $a.Status }" % guid)
    say("      Windows shows: %s" % shown)
    check("  the connection is named LS-Link",
          shown.split("|")[0] == tap.CONN_NAME, shown)

    say("")
    say("[3. open it and bring the media up]")
    dev = tap.Tap(guid).open()
    check("the device opened", bool(dev.handle))
    check("  it reported a MAC", len(dev.mac) == 6,
          ":".join("%02x" % b for b in dev.mac))
    st = ""
    for _ in range(30):
        st = ps("(Get-NetAdapter | Where-Object { $_.InterfaceGuid -eq "
                "'%s' }).Status" % guid)
        if st.strip().lower() == "up":
            break
        time.sleep(0.5)
    check("  Windows now sees it as Up, not unplugged",
          st.strip().lower() == "up", st)

    idx = ps("(Get-NetAdapter | Where-Object { $_.InterfaceGuid -eq '%s' })"
             ".ifIndex" % guid).strip()
    ps("netsh interface ipv4 set interface %s dadtransmits=0 store=active"
       % idx)
    ps("netsh interface ipv4 set address name=%s source=static addr=%s "
       "mask=255.255.255.0" % (idx, OURS))
    time.sleep(2)
    addr = ps("(Get-NetIPAddress -InterfaceIndex %s -AddressFamily IPv4 | "
              "Where-Object { $_.IPAddress -eq '%s' }).AddressState"
              % (idx, OURS))
    check("  an address sticks on it", addr.strip().lower() == "preferred",
          addr or "(none)")

    say("")
    say("[4. answer Windows at LAYER 2 - ARP then ping]")
    seen = {"arp": 0, "icmp": 0}
    stop = threading.Event()

    def pump():
        while not stop.is_set():
            try:
                f = dev.read(2048)
                if not f:
                    continue
            except Exception:
                return
            if len(f) < 14:
                continue
            et = int.from_bytes(f[12:14], "big")
            if et == 0x0806 and len(f) >= 42 and \
                    int.from_bytes(f[20:22], "big") == 1 and \
                    f[38:42] == ip2b(PEER):
                seen["arp"] += 1
                # ARP reply: we are PEER at PEER_MAC
                r = (f[6:12] + PEER_MAC + b"\x08\x06"
                     + b"\x00\x01\x08\x00\x06\x04\x00\x02"
                     + PEER_MAC + ip2b(PEER) + f[6:12] + f[28:32])
                dev.write(r + b"\x00" * max(0, 60 - len(r)))
            elif et == 0x0800 and len(f) >= 34 and f[23] == 1 \
                    and f[34] == 8 and f[30:34] == ip2b(PEER):
                seen["icmp"] += 1
                ihl = (f[14] & 0x0F) * 4
                ip = bytearray(f[14:14 + ihl])
                ip[12:16], ip[16:20] = f[30:34], f[26:30]
                ip[10:12] = b"\x00\x00"
                ip[10:12] = struct.pack(">H", csum(bytes(ip)))
                ic = bytearray(f[14 + ihl:])
                ic[0] = 0
                ic[2:4] = b"\x00\x00"
                ic[2:4] = struct.pack(">H", csum(bytes(ic)))
                out = (f[6:12] + PEER_MAC + b"\x08\x00" + bytes(ip)
                       + bytes(ic))
                dev.write(out)

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    time.sleep(0.5)
    r = subprocess.run(["ping", "-n", "4", "-w", "2000", PEER],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, errors="replace", timeout=60)
    stop.set()
    for ln in [l.strip() for l in (r.stdout or "").splitlines() if l.strip()][-3:]:
        say("      %s" % ln)
    check("Windows ARPed for the peer and we answered",
          seen["arp"] > 0, "%d ARP request(s) - this is what L3 could not do"
                           % seen["arp"])
    check("  ICMP arrived as Ethernet frames", seen["icmp"] > 0,
          "%d echo(es)" % seen["icmp"])
    check("  and ping.exe succeeded", r.returncode == 0,
          "exit %d" % r.returncode)

finally:
    say("")
    say("[5. remove the adapter]")
    try:
        if dev:
            dev.close()
    except Exception:
        pass
    time.sleep(1)
    if created:
        ok, why = tap.remove_instance(guid=guid)
        check("the adapter instance was removed", ok, why)
    time.sleep(2)
    left = ps("if (Get-NetAdapter -Name '%s' -ErrorAction SilentlyContinue)"
              " { 'present' } else { 'gone' }" % tap.CONN_NAME)
    check("  LS-Link is gone", left == "gone", left)
    net = ps("if (Test-Connection 8.8.8.8 -Count 1 -Quiet -ErrorAction "
             "SilentlyContinue) { 'ok' } else { 'BROKEN' }")
    check("  internet still fine", net == "ok", net)
    plant = ps("(Get-NetIPAddress -InterfaceIndex 5 -AddressFamily IPv4 "
               "-ErrorAction SilentlyContinue).IPAddress")
    check("  the plant adapter is untouched", "192.168.10.110" in plant,
          plant or "(none)")

say("")
say("RESULT: %s" % ("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
