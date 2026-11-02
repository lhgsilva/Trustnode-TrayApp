# -*- coding: utf-8 -*-
"""Does CIP survive the forward? The question that decides the design.

Bridging on Windows has proved fragile: "An unexpected error occurred
while configuring the Network Bridge", it moves the plant NIC's address,
it needs a manual step, and it took the plant PC off its own PLC twice.

The plain TCP forward needs none of that - no bridge, no driver, no admin,
no change to the plant network. The only question is whether RSLinx can
actually talk to a controller through it, and that is answerable here:

  agent + client over the real relay, forward 44818, then speak CIP
  ListIdentity to 127.0.0.1 and compare the controller's own identity
  string with the one read directly from the plant network.

If they match, the forward carries CIP intact and RSLinx only needs the
"Ethernet devices" driver pointed at 127.0.0.1.
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

PLC = os.environ.get("TNLINK_PLC", "192.168.10.240")
PORT = 44818
LOCAL = 44818
FAILS = []


def check(name, ok, detail=""):
    print("  %-50s: %s%s" % (name, "PASS" if ok else "FAIL",
                             (" - " + str(detail)[:180]) if detail else ""))
    if not ok:
        FAILS.append(name)
    return bool(ok)


def list_identity(host, port, timeout=8):
    """Encapsulation ListIdentity (0x0063). Returns (raw, product_text)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.sendall(struct.pack("<HHIIQI", 0x63, 0, 0, 0, 0, 0))
        r = s.recv(1024)
        if len(r) < 28 or r[:2] != b"\x63\x00":
            return r, ""
        txt = bytes(c for c in r[40:] if 32 <= c < 127).decode(
            "ascii", "replace")
        return r, txt.strip()
    finally:
        s.close()


print("LS-Link - does CIP survive the forward?")
print("  PLC: %s" % PLC)
print()

print("[1. straight from the plant network, as the controller really is]")
try:
    raw_direct, text_direct = list_identity(PLC, PORT)
    check("the controller answered directly", bool(raw_direct),
          "%d bytes" % len(raw_direct))
    print("      identity: %s" % text_direct)
except Exception as exc:                                    # noqa: BLE001
    check("the controller answered directly", False, exc)
    raise SystemExit(2)

print()
print("[2. bring up agent + client over the real relay]")
agent = subprocess.Popen(
    [os.path.join(HERE, "dist", "tnlink-server.exe"), "--minutes", "10"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    errors="replace", cwd=HERE)
code = ""
t0 = time.time()
while time.time() - t0 < 60:
    line = agent.stdout.readline()
    if not line:
        break
    if "TN-" in line and line.strip().startswith("TN-"):
        code = line.strip()
        break
check("the agent registered with the relay", bool(code), code or "no code")
if not code:
    agent.terminate()
    raise SystemExit(2)
print("      code: %s" % code)

client = subprocess.Popen(
    [os.path.join(HERE, "dist", "tnlink-client.exe"), "--code", code],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT, text=True, errors="replace", cwd=HERE)
time.sleep(8)
client.stdin.write("map %d %s:%d\n" % (LOCAL, PLC, PORT))
client.stdin.flush()
time.sleep(4)

print()
print("[3. the same question, through the tunnel]")
raw_fwd, text_fwd = b"", ""
err = ""
try:
    raw_fwd, text_fwd = list_identity("127.0.0.1", LOCAL, timeout=15)
except Exception as exc:                                    # noqa: BLE001
    err = "%s: %s" % (type(exc).__name__, exc)
check("the controller answered through the forward", bool(raw_fwd),
      err or "%d bytes" % len(raw_fwd))
if raw_fwd:
    print("      identity: %s" % text_fwd)
    check("  it is the SAME controller, byte for byte",
          raw_fwd[24:] == raw_direct[24:],
          "direct=%d fwd=%d bytes" % (len(raw_direct), len(raw_fwd)))
    check("  and the product name came through",
          text_fwd == text_direct and bool(text_fwd),
          "%r vs %r" % (text_fwd[:40], text_direct[:40]))

print()
print("[4. a bigger exchange - what going online actually does]")
# Register a CIP session, which is the first thing RSLinx does for real
# work. If this works, explicit messaging works.
ok_sess = False
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(15)
    s.connect(("127.0.0.1", LOCAL))
    s.sendall(struct.pack("<HHIIQI", 0x65, 4, 0, 0, 0, 0)
              + struct.pack("<HH", 1, 0))
    r = s.recv(256)
    s.close()
    ok_sess = len(r) >= 8 and r[:2] == b"\x65\x00"
    if ok_sess:
        sess = struct.unpack("<I", r[4:8])[0]
        print("      RegisterSession -> handle 0x%08x" % sess)
except Exception as exc:                                    # noqa: BLE001
    err = str(exc)
check("a CIP session registers through the forward", ok_sess,
      err if not ok_sess else "")

try:
    client.stdin.write("quit\n")
    client.stdin.flush()
except Exception:
    pass
time.sleep(1)
for p in (client, agent):
    try:
        p.terminate()
    except Exception:
        pass

print()
print("RESULT: %s" % ("PASS - CIP goes through the forward intact"
                      if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
