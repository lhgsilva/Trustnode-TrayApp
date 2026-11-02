# -*- coding: utf-8 -*-
"""The userspace stack, driven with real IP packets against the real PLC.

This is what option 6 now does on the plant side. It needs no adapter and
no administrator rights, so it can be tested directly: craft the packets
Windows would put on the TN-Link adapter, hand them to the stack, and see
what comes back.

Proves:
  * ICMP echo is answered, so `ping` works from the engineer's own
    command prompt,
  * a TCP connection completes its handshake,
  * CIP ListIdentity through it returns the SAME controller identity as
    reading it directly off the plant network,
  * a refused port is refused rather than left hanging,
  * a subnet outside the allowlist is not reachable at all.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tnlink_netstack as NS  # noqa: E402

PLC = os.environ.get("TNLINK_PLC", "192.168.10.240")
PLANT = os.environ.get("TNLINK_PLANT", "192.168.10.0/24")
ME = "10.77.0.2"
FAILS = []


def check(name, ok, detail=""):
    print("  %-52s: %s%s" % (name, "PASS" if ok else "FAIL",
                             (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)
    return bool(ok)


class Wire:
    """Stands in for the adapter: collects what the stack sends back."""

    def __init__(self):
        self.packets = []

    def __call__(self, pkt):
        self.packets.append(pkt)

    def take(self, proto, timeout=10.0, match=None):
        end = time.time() + timeout
        while time.time() < end:
            for i, p in enumerate(self.packets):
                if len(p) < 20 or p[9] != proto:
                    continue
                if match and not match(p):
                    continue
                return self.packets.pop(i)
            time.sleep(0.05)
        return None


def ipv4(src, dst, proto, body, ident=1):
    h = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(body), ident,
                    0x4000, 64, proto, 0, socket.inet_aton(src),
                    socket.inet_aton(dst))
    h = h[:10] + struct.pack(">H", NS.csum(h)) + h[12:]
    return h + body


def tcp(src, dst, sport, dport, seq, ack, flags, payload=b""):
    hdr = struct.pack(">HHIIBBHHH", sport, dport, seq, ack, 5 << 4, flags,
                      65535, 0, 0)
    pseudo = (socket.inet_aton(src) + socket.inet_aton(dst)
              + struct.pack(">BBH", 0, 6, len(hdr) + len(payload)))
    c = NS.csum(pseudo + hdr + payload)
    hdr = hdr[:16] + struct.pack(">H", c) + hdr[18:]
    return ipv4(src, dst, 6, hdr + payload)


def tcp_parts(pkt):
    ihl = (pkt[0] & 0x0F) * 4
    seg = pkt[ihl:]
    sport, dport, seq, ack = struct.unpack(">HHII", seg[:12])
    off = (seg[12] >> 4) * 4
    return sport, dport, seq, ack, seg[13], seg[off:]


print("TrustNode Link - the userspace stack, against the real PLC")
print("  plant %s   PLC %s" % (PLANT, PLC))
print()

print("[0. the controller, read directly off the plant network]")
direct = b""
try:
    s = socket.create_connection((PLC, 44818), 6)
    s.sendall(struct.pack("<HHIIQI", 0x63, 0, 0, 0, 0, 0))
    direct = s.recv(512)
    s.close()
except Exception as exc:                                    # noqa: BLE001
    check("the controller is reachable from this PC", False, exc)
    sys.exit(2)
name_direct = bytes(c for c in direct[40:] if 32 <= c < 127).decode(
    "ascii", "replace").strip()
check("the controller answered directly", len(direct) >= 28, name_direct)

wire = Wire()
stack = NS.NetStack(wire, [ipaddress.ip_network(PLANT)], log=lambda m: None)

print()
print("[1. ICMP - so ping works from the engineer's own prompt]")
icmp = struct.pack(">BBHHH", 8, 0, 0, 0x4242, 1) + b"trustnode"
icmp = (icmp[:2] + struct.pack(">H", NS.csum(icmp)) + icmp[4:])
stack.on_packet(ipv4(ME, PLC, 1, icmp))
rep = wire.take(1, 12.0)
check("the stack answered the ping", rep is not None)
if rep:
    ihl = (rep[0] & 0x0F) * 4
    check("  it is an echo REPLY from the PLC's address",
          rep[ihl] == 0 and socket.inet_ntoa(rep[12:16]) == PLC,
          "type=%d src=%s" % (rep[ihl], socket.inet_ntoa(rep[12:16])))

print()
print("[2. TCP handshake to the controller]")
SP, SEQ = 50001, 1000
stack.on_packet(tcp(ME, PLC, SP, 44818, SEQ, 0, 0x02))
sa = wire.take(6, 12.0, lambda p: tcp_parts(p)[4] & 0x12 == 0x12)
if not check("the stack sent SYN-ACK", sa is not None,
             "the connection was never accepted"):
    stack.stop()
    sys.exit(2)
_, _, their_isn, their_ack, _, _ = tcp_parts(sa)
check("  it acknowledged our SYN", their_ack == SEQ + 1,
      "ack=%d expected %d" % (their_ack, SEQ + 1))
SEQ += 1
RCV = (their_isn + 1) & 0xFFFFFFFF
stack.on_packet(tcp(ME, PLC, SP, 44818, SEQ, RCV, 0x10))

print()
print("[3. CIP through the stack]")
req = struct.pack("<HHIIQI", 0x63, 0, 0, 0, 0, 0)
stack.on_packet(tcp(ME, PLC, SP, 44818, SEQ, RCV, 0x18, req))
SEQ += len(req)
body = b""
end = time.time() + 20
while time.time() < end and len(body) < 28:
    p = wire.take(6, 3.0, lambda q: tcp_parts(q)[5])
    if p is None:
        continue
    _, _, seq, _, _, payload = tcp_parts(p)
    body += payload
    RCV = (seq + len(payload)) & 0xFFFFFFFF
    stack.on_packet(tcp(ME, PLC, SP, 44818, SEQ, RCV, 0x10))
good = len(body) >= 28 and body[0] == 0x63 and body[1] == 0
check("CIP ListIdentity came back through the stack", good,
      "%d bytes" % len(body))
name_stack = (bytes(c for c in body[40:] if 32 <= c < 127)
              .decode("ascii", "replace").strip()) if good else ""
if name_stack:
    print("      identity: %s" % name_stack)
check("  it is the SAME controller as read directly",
      bool(name_stack) and name_stack == name_direct,
      "%r vs %r" % (name_stack[:34], name_direct[:34]))
check("  byte for byte", body[:len(direct)] == direct,
      "stack=%d direct=%d" % (len(body), len(direct)))

print()
print("[4. a closed port is refused, not left hanging]")
stack.on_packet(tcp(ME, PLC, 50002, 9, 2000, 0, 0x02))
rst = wire.take(6, 12.0, lambda p: tcp_parts(p)[4] & 0x04)
check("a RST came back for a port nothing listens on", rst is not None,
      "without it the engineer's program waits for a timeout")

print()
print("[5. the allowlist still holds]")
before = stack.stats["refused"]
stack.on_packet(tcp(ME, "8.8.8.8", 50003, 53, 3000, 0, 0x02))
time.sleep(0.5)
check("a target outside the plant network is refused",
      stack.stats["refused"] > before,
      "the plant PC must not become an open proxy")

print()
print("  %s" % stack.summary())
stack.stop()
print()
print("RESULT: %s" % ("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
