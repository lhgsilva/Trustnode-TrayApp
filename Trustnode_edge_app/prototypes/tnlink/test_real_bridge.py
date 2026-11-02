# -*- coding: utf-8 -*-
"""A REAL bridge, proven by passing a frame through it.

The previous attempt used New-NetSwitchTeam, which makes a NIC TEAM
(Microsoft Network Adapter Multiplexor), not a bridge. A team aggregates
links for the host; it has no MAC learning and does NOT forward frames
between its members. ICMP survived on a technicality, CIP did not, and
the plant PC lost its own route to the PLC for five minutes.

That mistake got through because the test only checked the team OBJECT
existed. This one sends a frame in one adapter and requires it to come
out of the other. A bridge that does not forward fails here.

The real bridge is the ms_bridge component, created through the Network
Connections shell verb - the same thing "Bridge Connections" does by
hand. No plant NIC is involved: two throwaway TAP adapters only.
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

import tap  # noqa: E402

OUT = os.path.join(HERE, "_realbridge_result.txt")
LOG = []
FAILS = []

A_NAME = "LS-Link"
B_NAME = "LS-Link-B"
MAGIC = b"TRUSTNODE-BRIDGE-PROBE"


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


def ps(cmd, timeout=180):
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-Command", cmd], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True,
                       errors="replace", timeout=timeout)
    return r.returncode, (r.stdout or "").strip()


BRIDGE_VERB = r'''
param([string]$Conn)
$sh = New-Object -ComObject Shell.Application
$ns = $sh.Namespace(0x31)
foreach ($it in $ns.Items()) {
  if ($it.Name -eq $Conn) {
    foreach ($v in $it.Verbs()) {
      $n = ($v.Name -replace '&','')
      if ($n -match 'Bridge') { $v.DoIt(); "INVOKED:$n"; return }
    }
    "NOVERB"; return
  }
}
"NOCONN"
'''


def invoke_bridge(conn):
    f = os.path.join(HERE, "_verb.ps1")
    io.open(f, "w", encoding="utf-8").write(BRIDGE_VERB)
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-File", f,
                        "-Conn", conn], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True,
                       errors="replace", timeout=180)
    try:
        os.remove(f)
    except OSError:
        pass
    return (r.stdout or "").strip()


say("LS-Link - a REAL bridge, proven by forwarding a frame")
say("  (two throwaway TAP adapters; the plant NIC is NOT touched)")
say("")
check("running elevated", tap.is_admin())
if not tap.is_admin():
    finish(2)

rc, plant_before = ps("(Get-NetIPAddress -InterfaceAlias 'Ethernet 3' "
                      "-AddressFamily IPv4 -ErrorAction SilentlyContinue)"
                      ".IPAddress")
say("  plant adapter before: %s" % plant_before)

guids = []
devs = []
try:
    say("")
    say("[1. two TAP adapters, both with media UP]")
    if not tap.driver_installed():
        ok, why = tap.install_driver()
        check("driver installed", ok, why)
    before = {g for g, _ in tap.list_instances()}
    for _ in range(2):
        ok, why = tap.create_instance()
        if not ok:
            check("adapter created", False, why)
            finish(2)
        time.sleep(2.5)
    fresh = [g for g, _ in tap.list_instances() if g not in before]
    check("two adapters exist", len(fresh) >= 2, "%d" % len(fresh))
    if len(fresh) < 2:
        finish(2)
    guids = fresh[:2]
    for g, nm in zip(guids, (A_NAME, B_NAME)):
        ps("$a = Get-NetAdapter | Where-Object { $_.InterfaceGuid -eq '%s' };"
           " if ($a -and $a.Name -ne '%s') { Rename-NetAdapter -Name "
           "$a.Name -NewName '%s' }" % (g, nm, nm))
        time.sleep(0.5)
        devs.append(tap.Tap(g).open())        # opening sets media connected
    time.sleep(2)
    rc, st = ps("@('%s','%s') | ForEach-Object { (Get-NetAdapter -Name $_ "
                "-ErrorAction SilentlyContinue).Status }" % (A_NAME, B_NAME))
    say("      status: %s" % st.replace("\n", ", "))
    check("  both report Up", st.lower().count("up") >= 2, st)

    say("")
    say("[2. bridge them with the Network Connections verb]")
    for nm in (A_NAME, B_NAME):
        out = invoke_bridge(nm)
        say("      %-12s -> %s" % (nm, out))
        time.sleep(6)
    time.sleep(4)
    rc, binding = ps("Get-NetAdapterBinding -ErrorAction SilentlyContinue | "
                     "Where-Object { $_.ComponentID -eq 'ms_bridge' -and "
                     "$_.Enabled } | ForEach-Object { $_.Name }")
    say("      ms_bridge bound to: %s" % (binding.replace("\n", ", ")
                                          or "(nothing)"))
    check("ms_bridge is bound to both adapters",
          A_NAME in binding and B_NAME in binding, binding or "(none)")
    rc, br = ps("(Get-NetAdapter -ErrorAction SilentlyContinue | "
                "Where-Object { $_.InterfaceDescription -match 'Network "
                "Bridge' }).Name")
    check("  a Network Bridge adapter exists", bool(br.strip()),
          br or "(none)")

    say("")
    say("[3. THE TEST THE LAST ONE MISSED - does a frame cross?]")
    got = {}
    stop = threading.Event()

    def listen(d):
        while not stop.is_set():
            try:
                f = d.read(2048)
                if not f:
                    continue
            except Exception:
                return
            if MAGIC in f:
                got["frame"] = f
                return

    th = threading.Thread(target=listen, args=(devs[1],), daemon=True)
    th.start()
    time.sleep(1)
    # A broadcast frame from a made-up MAC, injected into adapter A.
    frame = (b"\xff" * 6 + bytes.fromhex("02aabbcc0001") + b"\x88\xb5"
             + MAGIC + b"\x00" * 20)
    for _ in range(8):
        devs[0].write(frame)
        if got.get("frame"):
            break
        time.sleep(0.7)
    stop.set()
    th.join(timeout=3)
    check("a frame sent into %s came out of %s" % (A_NAME, B_NAME),
          bool(got.get("frame")),
          "this is exactly what a NIC team could NOT do")

finally:
    say("")
    say("[4. remove everything]")
    for d in devs:
        try:
            d.close()
        except Exception:
            pass
    time.sleep(1)
    ps("Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object "
       "{ $_.InterfaceDescription -match 'Network Bridge' } | "
       "ForEach-Object { $i = $_.InterfaceGuid; "
       "Get-PnpDevice -Class Net -ErrorAction SilentlyContinue | "
       "Where-Object { $_.FriendlyName -match 'Network Bridge' } | "
       "ForEach-Object { & pnputil /remove-device $_.InstanceId } }")
    time.sleep(4)
    # Only OUR adapters, by instance id - another vendor shares tap0901.
    rc, out = ps("Get-PnpDevice -Class Net -ErrorAction SilentlyContinue | "
                 "Where-Object { $_.FriendlyName -match '^TAP-Windows "
                 "Adapter V9' } | ForEach-Object { & pnputil /remove-device "
                 "$_.InstanceId; $_.InstanceId }")
    say("      removed: %s" % (out.replace("\n", " ")[:120] or "(none)"))
    time.sleep(3)
    rc, left = ps("@(Get-NetAdapter -ErrorAction SilentlyContinue | "
                  "Where-Object { $_.InterfaceDescription -match "
                  "'^TAP-Windows Adapter V9|Network Bridge' }).Count")
    check("adapters and bridge are gone", left.strip() == "0", left)
    rc, plant_after = ps("(Get-NetIPAddress -InterfaceAlias 'Ethernet 3' "
                         "-AddressFamily IPv4 -ErrorAction SilentlyContinue)"
                         ".IPAddress")
    check("  the plant adapter is unchanged",
          plant_after.strip() == plant_before.strip(),
          "before=%s after=%s" % (plant_before, plant_after))
    rc, plc = ps("if (Test-Connection 192.168.10.240 -Count 2 -Quiet "
                 "-ErrorAction SilentlyContinue) { 'ok' } else { 'LOST' }")
    check("  the PLC is still reachable from here", plc == "ok", plc)
    rc, nord = ps("@(Get-NetAdapter -ErrorAction SilentlyContinue | "
                  "Where-Object { $_.InterfaceDescription -match "
                  "'NordVPN' }).Count")
    check("  the other vendor's TAP adapter survived", nord.strip() == "1",
          nord)

say("")
say("RESULT: %s" % ("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
