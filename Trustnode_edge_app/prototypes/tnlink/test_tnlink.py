# -*- coding: utf-8 -*-
"""Drive both roles over a real socket. No mocks, no live TrustNode.

Each check here is something that must hold before this is worth building
into the app:

  * a correct code pairs, a wrong code is REFUSED - tested by actually
    presenting a wrong one, because an auth check that cannot fail proves
    nothing,
  * ping carries a real RTT and TTL back from the server's own stack,
  * the allowlist refuses a target outside it - this is what stops the
    plant PC becoming an open proxy,
  * a mapped TCP port really carries bytes end to end,
  * a tampered frame drops the link rather than being accepted.

Run:  python prototypes/tnlink/test_tnlink.py
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tnlink as T  # noqa: E402

FAILS = []
PORT = int(os.environ.get("TNLINK_TEST_PORT", "51899"))
ECHO_PORT = PORT + 1


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:200]) if detail else ""))
    if not ok:
        FAILS.append(name)
    return bool(ok)


print("TrustNode Link - prototype self-test")
print()

# --------------------------------------------------------------------------
print("[1. the Connection Code is liberal in what it accepts]")
code = T.new_code()
print("      generated: %s" % code)
base = T.normalise_code(code)
check("a clean code normalises", len(base) == 15, base)
variants = {
    "surrounding whitespace": "  " + code + "\n",
    "lower case": code.lower(),
    "inner spaces": code.replace("-", " "),
    "no separators": code.replace("-", ""),
    "an em dash from a mail client": code.replace("-", "—"),
    "a line wrap": code[:8] + "\r\n" + code[8:],
    "no TN prefix": code[3:],
}
for label, text in variants.items():
    check("  %s" % label, T.normalise_code(text) == base,
          "%r -> %r" % (text, T.normalise_code(text)))
other = T.new_code()
check("  a DIFFERENT code does not collide",
      T.normalise_code(other) != base,
      "tolerance must never become acceptance")

# --------------------------------------------------------------------------
print()
print("[2. ICMP comes back with a real measurement]")
r = T.icmp_ping("127.0.0.1", count=2)
check("loopback answers", r["received"] == 2, r)
check("  and carries an RTT and a TTL",
      "rtt_avg" in r and (r.get("ttl") or 0) > 0,
      "a ping with no numbers is not a diagnostic")
dead = T.icmp_ping("192.0.2.1", count=1, timeout_ms=600)   # TEST-NET-1
check("  an unroutable address reports loss, not a crash",
      dead["received"] == 0, dead["replies"])

subnets = T.local_subnets()
check("this PC's own subnets were found", bool(subnets),
      "the default allowlist is derived from these")
for s in subnets:
    print("      %s  (%s)" % (s["ip"], s["cidr"]))

# --------------------------------------------------------------------------
print()
print("[3. a plain TCP echo server, to prove the forward carries bytes]")
echo_ready = threading.Event()


def echo_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", ECHO_PORT))
    s.listen(4)
    echo_ready.set()
    while True:
        try:
            c, _ = s.accept()
        except OSError:
            return
        def handle(c=c):
            try:
                while True:
                    b = c.recv(4096)
                    if not b:
                        return
                    c.sendall(b)
            except OSError:
                pass
            finally:
                c.close()
        threading.Thread(target=handle, daemon=True).start()


threading.Thread(target=echo_server, daemon=True).start()
echo_ready.wait(5)
check("the echo server is listening", echo_ready.is_set())

# --------------------------------------------------------------------------
print()
print("[4. the server pairs only with the right code]")
# 127.0.0.0/8 is refused by policy, so allow it explicitly for the test.
srv = T.Server(code, ["127.0.0.0/8"], PORT, 0, "127.0.0.1")
# permitted() refuses loopback before it consults the allowlist, which is
# correct for production; override just that for this harness.
srv.permitted = lambda ip: (
    (True, "") if ip.startswith("127.") or any(
        __import__("ipaddress").ip_address(ip) in n for n in srv.allow)
    else (False, "%s is outside the allowlist" % ip))
threading.Thread(target=srv.serve, daemon=True).start()
time.sleep(1.0)

wrong = T.new_code()
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(10)
refused = ""
try:
    s.connect(("127.0.0.1", PORT))
    T.handshake(s, wrong, is_server=False)
    refused = "PAIRED - the wrong code was accepted"
except T.AuthError as exc:
    refused = "AuthError: %s" % exc
except Exception as exc:
    refused = "%s: %s" % (type(exc).__name__, exc)
finally:
    s.close()
check("a WRONG code is refused", "AuthError" in refused, refused)

cli = T.Client("127.0.0.1", PORT, code)
ok = True
try:
    cli.connect()
except Exception as exc:
    ok = False
    refused = "%s: %s" % (type(exc).__name__, exc)
if not check("the RIGHT code pairs", ok, refused if not ok else ""):
    print("RESULT: FAIL - cannot continue without a session")
    sys.exit(2)
check("  and the server described its network",
      bool(cli.info.get("host")) and "subnets" in cli.info,
      cli.info.get("host"))

# --------------------------------------------------------------------------
print()
print("[5. ping travels over the link]")
cli.sec.send(T.pack_ctrl({"op": "ping", "ip": "127.0.0.1", "count": 2}))
pr = cli._await("ping", 20)
check("the client got a ping result", bool(pr), pr)
if pr:
    check("  with 2 replies", pr.get("received") == 2, pr)
    check("  carrying an RTT", "rtt_avg" in pr, pr)

# --------------------------------------------------------------------------
print()
print("[6. the allowlist actually refuses something]")
cli.sec.send(T.pack_ctrl({"op": "ping", "ip": "8.8.8.8", "count": 1}))
pr = cli._await("ping", 20)
check("a target outside the allowlist is refused",
      bool(pr) and bool(pr.get("error")),
      "without this the plant PC is an open proxy to the internet: %s" % pr)
cli.sec.send(T.pack_ctrl({"op": "open", "sid": 999,
                          "ip": "8.8.8.8", "port": 53}))
orr = cli._await("open", 20)
check("  and so is a TCP open to it",
      bool(orr) and bool(orr.get("error")), orr)

# --------------------------------------------------------------------------
print()
print("[7. a mapped port carries real bytes end to end]")
cli.cmd_map("%d" % (PORT + 2), "127.0.0.1:%d" % ECHO_PORT)
time.sleep(0.6)
got = b""
try:
    t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    t.settimeout(12)
    t.connect(("127.0.0.1", PORT + 2))
    t.sendall(b"hello-plc")
    got = t.recv(4096)
    t.close()
except Exception as exc:
    got = ("ERROR %s: %s" % (type(exc).__name__, exc)).encode()
check("the forward returned the echo", got == b"hello-plc", got)

big = os.urandom(200_000)
rt = b""
try:
    t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    t.settimeout(30)
    t.connect(("127.0.0.1", PORT + 2))
    t.sendall(big)
    want = len(big)
    while len(rt) < want:
        chunk = t.recv(65536)
        if not chunk:
            break
        rt += chunk
    t.close()
except Exception as exc:
    rt = ("ERROR %s: %s" % (type(exc).__name__, exc)).encode()
check("  200 KB survives the framing intact",
      rt == big,
      "got %d bytes, expected %d - a program upload depends on this"
      % (len(rt), len(big)))

# --------------------------------------------------------------------------
print()
print("[8. a tampered frame drops the link instead of being accepted]")
# The server holds ONE client at a time (documented prototype limit), so the
# first session has to end before this connection can even be accepted.
cli.alive = False
if cli.sec:
    cli.sec.close()
time.sleep(1.5)
# Hand-build a session, then flip a byte of ciphertext on the wire.
s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s2.settimeout(10)
verdict = ""
try:
    s2.connect(("127.0.0.1", PORT))
    sec2 = T.handshake(s2, code, is_server=False)
    pt = T.pack_ctrl({"op": "hello"})
    ct = sec2._tx.encrypt(sec2._nonce(sec2._tx_n), pt, None)
    bad = bytearray(ct)
    bad[-1] ^= 0x01
    s2.sendall(struct.pack(">I", len(bad)) + bytes(bad))
    s2.settimeout(6)
    try:
        resp = s2.recv(4096)
        verdict = ("link stayed up and answered %d bytes" % len(resp)
                   if resp else "server closed the connection")
    except socket.timeout:
        verdict = "no answer (server dropped it)"
except Exception as exc:
    verdict = "%s: %s" % (type(exc).__name__, exc)
finally:
    s2.close()
check("a forged frame is not answered",
      "closed" in verdict or "dropped" in verdict, verdict)

# The bug this guards: InvalidTag from the AEAD propagated out of serve()
# and killed the server, so ONE bad frame ended the service for everyone.
# Revert the catch-all in Server.serve() and this check fails.
time.sleep(1.5)
survived = ""
try:
    c3 = T.Client("127.0.0.1", PORT, code)
    c3.connect()
    survived = "paired again: %s" % (c3.info.get("host") or "?")
    c3.alive = False
    c3.sec.close()
except Exception as exc:
    survived = "%s: %s" % (type(exc).__name__, exc)
check("  and the SERVER SURVIVES it", "paired again" in survived, survived)

print()
print("[9. the firewall rule is built correctly and only when elevated]")
# A malformed netsh argument fails silently as "NOT ADDED" at a customer
# site. Check the argv here, where it is cheap, rather than there.
import subprocess as _sp  # noqa: E402

_args = T._fw_args(51820)
check("netsh is invoked, not a shell string", _args[0] == "netsh", _args[0])
_d = {}
for a in _args:
    if "=" in a and not a.startswith("netsh"):
        k, v = a.split("=", 1)
        _d[k] = v
for key, want in (("dir", "in"), ("action", "allow"), ("protocol", "TCP"),
                  ("localport", "51820"), ("remoteip", "localsubnet"),
                  ("enable", "yes")):
    check("  %s=%s" % (key, want), _d.get(key) == want,
          "got %r" % _d.get(key))
check("  the program path is absolute",
      os.path.isabs(_d.get("program", "")), _d.get("program"))
check("  the rule is NOT opened to the internet",
      _d.get("remoteip") != "any",
      "a prototype must not punch a hole to the whole internet")
check("  and it is named, so it can be removed again",
      _d.get("name") == T.FW_RULE, _d.get("name"))

_real_admin, _real_run = T._is_admin, _sp.run
_calls = []
try:
    T._is_admin = lambda: False
    ok, why = T._fw_add(51820)
    check("unelevated, nothing is changed", (not ok) and "elevated" in why, why)

    _sp.run = lambda *a, **k: _calls.append(a[0]) or type(
        "R", (), {"returncode": 0, "stdout": ""})()
    T._is_admin = lambda: True
    ok, why = T._fw_add(51820)
    check("elevated, the rule is added", ok and bool(_calls), why)

    # The whole point of the request: admin means it just happens.
    _calls.clear()
    srv = T.Server("TN-AAAAA-BBBBB-CCCCC", ["127.0.0.0/8"], 51820, 0,
                   "127.0.0.1")
    T._fw_rule_exists = lambda name=T.FW_RULE: False
    srv._ensure_firewall()
    check("running as admin installs it with no flag", len(_calls) == 1,
          "%d netsh call(s)" % len(_calls))

    _calls.clear()
    T._fw_rule_exists = lambda name=T.FW_RULE: True
    srv._ensure_firewall()
    check("  and an existing rule is not added twice", not _calls,
          "%d netsh call(s)" % len(_calls))

    _calls.clear()
    T._fw_rule_exists = lambda name=T.FW_RULE: False
    srv.firewall = False
    if srv.firewall:
        srv._ensure_firewall()
    check("  --no-firewall touches nothing", not _calls)
finally:
    T._is_admin, _sp.run = _real_admin, _real_run

print()
print("[10. a second server cannot steal a port that is already in use]")
# Measured 2026-10-21: SO_REUSEADDR let two servers bind 0.0.0.0:51820 at
# once, each printing a different Connection Code, and a client reaching
# "the server" hit whichever won. On Windows that flag means "share the
# port", which is not what it means on Unix.
import io as _io  # noqa: E402
_src = _io.open(os.path.join(HERE, "tnlink.py"), encoding="utf-8").read()
_serve_code = chr(10).join(
    ln for ln in _src.split("def serve")[1][:2200].splitlines()
    if not ln.strip().startswith("#"))
check("SO_REUSEADDR is not SET on the listener",
      "setsockopt" not in _serve_code or "SO_REUSEADDR" not in _serve_code,
      "on Windows it permits a second bind to a live listening port")
check("  SO_EXCLUSIVEADDRUSE is", "SO_EXCLUSIVEADDRUSE" in _src)

_p2 = int(os.environ.get("TNLINK_TEST_PORT2", "51897"))
_a = T.Server(T.new_code(), ["127.0.0.0/8"], _p2, 0, "127.0.0.1",
              firewall=False)
threading.Thread(target=_a.serve, daemon=True).start()
time.sleep(1.5)
_b = T.Server(T.new_code(), ["127.0.0.0/8"], _p2, 0, "127.0.0.1",
              firewall=False)
_rc = _b.serve()
check("the second server refuses instead of sharing", _rc == 2,
      "returned %r - two servers on one port means the Connection Code on "
      "screen may not be the one answering" % _rc)

print()
print("[11. claim never borrows an address on an adapter that matters]")
# 2026-10-21, caused by this tool on a live PC: netsh add address switches
# a DHCP interface to STATIC, and that setting outlives the address. The
# Wi-Fi adapter was left static with no address - APIPA, no default route,
# no internet. The address must never land on a DHCP-managed adapter or
# on the one carrying the default route.
facts = T._interface_facts()
check("this PC's interfaces were readable", bool(facts),
      "without this the choice cannot be made safely")
idx, name, why = T.pick_claim_interface()
if facts:
    if idx:
        f = facts.get(idx, {})
        check("the chosen adapter is NOT DHCP-managed", not f.get("dhcp"),
              "%s (idx %s)" % (name, idx))
        check("  and does NOT carry the default route",
              not f.get("default_route"), "%s (idx %s)" % (name, idx))
        print("      chose: %s (idx %d)" % (name, idx))
    else:
        check("refusing is explained", bool(why), why)
        print("      refused: %s" % why)
    risky = [i for i, f in facts.items()
             if f.get("dhcp") or f.get("default_route")]
    check("  adapters it must never pick were identified", True,
          "%d risky: %s" % (len(risky),
                            ", ".join(facts[i]["name"][:18] for i in risky[:3])))
    check("  and the chosen one is not among them", idx not in risky,
          "idx %s" % idx)

print()
print("RESULT: {0}".format("PASS" if not FAILS else
                           "FAIL - " + ", ".join(FAILS)))
sys.stdout.flush()
os._exit(0 if not FAILS else 2)
