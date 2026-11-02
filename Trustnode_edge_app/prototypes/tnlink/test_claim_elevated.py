# -*- coding: utf-8 -*-
"""Verify the ELEVATED half of `claim`, and MEASURE how long it takes.

First run found the real problem: netsh adds the address and Windows lists
it, but binding fails at once with WSAEADDRNOTAVAIL ("not valid in its
context"). A freshly added IPv4 address goes through Duplicate Address
Detection and is Tentative until that completes - and a Tentative address
cannot be bound.

So the question is not "does it work" but "how long until it does", and
whether DAD can be skipped. Both decide what cmd_claim has to do.

Uses 192.168.77.240, never the real PLC.
"""
import io
import os
import socket
import subprocess
import sys
import threading
import time

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "elev_result.txt")
LOG = []


def say(msg):
    LOG.append(msg)
    print(msg, flush=True)


def finish(code):
    io.open(OUT, "w", encoding="utf-8").write("\n".join(LOG) + "\n")
    sys.exit(code)


sys.path.insert(0, r"D:\Trustnode\Trustnode-AB\Tray_app\Trustnode_edge_app"
                   r"\prototypes\tnlink")
import tnlink as T          # noqa: E402

IP = "192.168.77.240"
PORT = 44818
FAILS = []


def check(name, ok, detail=""):
    say("  %-52s: %s%s" % (name, "PASS" if ok else "FAIL",
                           (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)
    return bool(ok)


def addr_state(ip):
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-NetIPAddress -IPAddress %s -ErrorAction SilentlyContinue"
             ").AddressState" % ip],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            errors="replace", timeout=25)
        return (r.stdout or "").strip() or "(absent)"
    except Exception as exc:                                # noqa: BLE001
        return "err %s" % exc


def can_bind(ip, port=0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((ip, port))
        return True, ""
    except OSError as exc:
        return False, (exc.strerror or str(exc))
    finally:
        s.close()


say("TrustNode Link - elevated claim mechanism")
say("  test address: %s  (NOT the PLC)" % IP)
say("")
check("this process is elevated", T._is_admin())
if not T._is_admin():
    finish(2)

T._remove_local_ip(IP)      # in case the previous run left it behind
time.sleep(0.5)
idx = T._primary_if_index()
check("an adapter index was found", bool(idx), "ifIndex=%s" % idx)

say("")
say("[1. add it, then watch DAD]")
t0 = time.time()
ok, why = T._add_local_ip(idx, IP)
if not check("netsh added the address", ok, why):
    finish(2)

bindable_at = None
states = []
while time.time() - t0 < 20:
    st = addr_state(IP)
    ok_bind, err = can_bind(IP)
    if not states or states[-1][1] != st:
        states.append((round(time.time() - t0, 2), st))
    if ok_bind:
        bindable_at = time.time() - t0
        break
    time.sleep(0.25)

for when, st in states:
    say("      t+%5.2fs  AddressState = %s" % (when, st))
check("the address became bindable", bindable_at is not None,
      "%.2fs" % bindable_at if bindable_at is not None
      else "still not bindable after 20s - %s" % can_bind(IP)[1])
if bindable_at is None:
    say("")
    say("[trying with DAD disabled on the interface]")
    subprocess.run(["netsh", "interface", "ipv4", "set", "interface",
                    str(idx), "dadtransmits=0"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    T._remove_local_ip(IP)
    time.sleep(0.5)
    t1 = time.time()
    T._add_local_ip(idx, IP)
    while time.time() - t1 < 10:
        if can_bind(IP)[0]:
            bindable_at = time.time() - t1
            break
        time.sleep(0.25)
    say("      dadtransmits=0 -> bindable after %s"
        % ("%.2fs" % bindable_at if bindable_at is not None else "never"))

if bindable_at is None:
    T._remove_local_ip(IP)
    say("")
    say("RESULT: FAIL - the address never became bindable")
    finish(2)

say("")
say("[2. a listener on the device's own address]")
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    srv.bind((IP, PORT))
    srv.listen(4)
    check("bound %s:%d" % (IP, PORT), True)
except OSError as exc:
    check("bound %s:%d" % (IP, PORT), False, exc.strerror or exc)
    T._remove_local_ip(IP)
    finish(2)

got = {}


def accept_one():
    try:
        c, a = srv.accept()
        got["peer"] = a
        got["data"] = c.recv(64)
        c.sendall(b"CIP-REPLY")
        c.close()
    except OSError as exc:
        got["err"] = str(exc)


th = threading.Thread(target=accept_one, daemon=True)
th.start()
time.sleep(0.4)

say("")
say("[3. connect to the REAL address, as RSLinx would]")
try:
    c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    c.settimeout(8)
    tc = time.time()
    c.connect((IP, PORT))
    c.sendall(b"ListIdentity")
    reply = c.recv(64)
    c.close()
    check("connecting to %s:%d reached our listener" % (IP, PORT),
          reply == b"CIP-REPLY",
          "%.0f ms" % ((time.time() - tc) * 1000))
except OSError as exc:
    check("connecting to %s:%d reached our listener" % (IP, PORT), False,
          exc.strerror or exc)
th.join(timeout=5)
check("  the listener saw the request", got.get("data") == b"ListIdentity",
      got)
# The source address is NOT the claimed one, and must not be: the address
# is added with skipassource=true precisely so Windows never picks it as a
# source. What matters is that the connection arrived at all, which proves
# local delivery - it was never ARPed on the wire, because nothing on this
# network owns that address.
check("  delivered locally (connection arrived at all)",
      bool(got.get("peer")) and got.get("data") == b"ListIdentity",
      "source was %s, which is correct with skipassource=true"
      % (got.get("peer") or ("?",))[0])
try:
    srv.close()
except OSError:
    pass

say("")
say("[4. remove it again]")
ok, why = T._remove_local_ip(IP)
check("netsh removed the address", ok, why)
time.sleep(1.0)
check("  it is really gone",
      not any(a.get("ip") == IP for a in T.local_subnets()),
      "a leftover would hijack that IP on this PC")

say("")
say("  >>> cmd_claim must wait at least %.1fs for the address <<<"
    % (bindable_at or 0))
say("")
say("RESULT: %s" % ("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
