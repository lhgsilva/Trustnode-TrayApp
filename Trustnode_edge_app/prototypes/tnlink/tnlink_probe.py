# -*- coding: utf-8 -*-
"""A portable TrustNode Link client, for proving the chain from outside.

Runs on Linux, so the VPS can act as a genuinely remote engineer:

    plant PC (agent)  --out-->  relay on the VPS  <--in--  this probe

It is a DELIBERATELY SEPARATE implementation of the wire protocol. If it
can pair and reach the PLC, that is evidence about the protocol itself,
not just about tnlink.py agreeing with its own assumptions.

What it proves, in order:
  1. it can pair through the relay with only the Connection Code,
  2. the agent describes the plant network,
  3. ICMP from the plant side reaches the PLC,
  4. a TCP stream opens to the controller's CIP port,
  5. a real CIP ListIdentity comes back with the controller's name.

Usage:  python3 tnlink_probe.py --relay HOST:7443 --code TN-xxxxx-xxxxx-xxxxx \\
                                [--plc 192.168.10.240]
"""
from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time

from cryptography.hazmat.primitives import hashes, hmac as c_hmac
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = "tnlink-proto-0.1"
R_MAGIC = b"TNLR1"
CTRL, DATA, TUN = 1, 2, 3
_CONFUSE = {"O": "0", "o": "0", "I": "1", "i": "1", "L": "1", "l": "1"}
FAILS = []


def check(name, ok, detail=""):
    print("  %-50s: %s%s" % (name, "PASS" if ok else "FAIL",
                             (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)
    return bool(ok)


def normalise_code(text):
    s = str(text or "")
    for d in ("‐", "‑", "‒", "–", "—", "−"):
        s = s.replace(d, "-")
    s = "".join(ch for ch in s if not ch.isspace())
    s = s.replace("-", "").replace("_", "")
    if s[:2].upper() == "TN":
        s = s[2:]
    return "".join(_CONFUSE.get(ch, ch) for ch in s).upper()


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            raise ConnectionError("peer closed")
        buf += c
    return buf


def mac(code, transcript, label):
    h = c_hmac.HMAC(code, hashes.SHA256())
    h.update(label + b"|" + transcript)
    return h.finalize()


class Secure:
    def __init__(self, sock, send_key, recv_key):
        self.sock = sock
        self._tx = ChaCha20Poly1305(send_key)
        self._rx = ChaCha20Poly1305(recv_key)
        self._tn = 0
        self._rn = 0

    @staticmethod
    def _nonce(n):
        return b"\x00\x00\x00\x00" + struct.pack(">Q", n)

    def send(self, pt):
        ct = self._tx.encrypt(self._nonce(self._tn), pt, None)
        self._tn += 1
        self.sock.sendall(struct.pack(">I", len(ct)) + ct)

    def recv(self):
        (n,) = struct.unpack(">I", recv_exact(self.sock, 4))
        ct = recv_exact(self.sock, n)
        pt = self._rx.decrypt(self._nonce(self._rn), ct, None)
        self._rn += 1
        return pt


def parse(frame):
    k = frame[0]
    if k == CTRL:
        return CTRL, json.loads(frame[1:].decode()), None
    if k == DATA:
        (sid,) = struct.unpack(">I", frame[1:5])
        return DATA, sid, frame[5:]
    return TUN, None, frame[1:]


def connect(relay, code_text, timeout=40):
    host, _, port = relay.partition(":")
    port = int(port or 7443)
    code = normalise_code(code_text).encode()
    room = c_hmac.HMAC(code, hashes.SHA256())
    room.update(b"tnlink-room")
    room = room.finalize()[:16]

    s = socket.create_connection((host, port), timeout)
    s.sendall(R_MAGIC + b"c" + room)
    s.settimeout(timeout)
    while True:
        b = recv_exact(s, 1)
        if b == b"\x00":
            continue
        if b == b"\x01":
            break
        raise RuntimeError({b"\x02": "no agent is waiting with that code",
                            b"\x03": "someone else is connected",
                            b"\x04": "the relay is full"}.get(b, "relay said %r" % b))
    s.settimeout(None)

    priv = X25519PrivateKey.generate()
    mine = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    peer_hello = recv_exact(s, 24)
    theirs = recv_exact(s, 32)
    s.sendall(VERSION.encode().ljust(24, b"\x00") + mine)
    if peer_hello.rstrip(b"\x00").decode("ascii", "replace") != VERSION:
        raise RuntimeError("version mismatch")

    shared = priv.exchange(X25519PublicKey.from_public_bytes(theirs))
    transcript = VERSION.encode() + theirs + mine
    keys = HKDF(algorithm=hashes.SHA256(), length=64, salt=transcript,
                info=b"tnlink-v1-keys").derive(shared)
    s2c, c2s = keys[:32], keys[32:]
    got = recv_exact(s, 32)
    if got != mac(code, transcript, b"server"):
        raise RuntimeError("the Connection Code does not match")
    s.sendall(mac(code, transcript, b"client"))
    return Secure(s, c2s, s2c)


def await_op(sec, op, timeout=60, sink=None):
    end = time.time() + timeout
    while time.time() < end:
        kind, a, b = parse(sec.recv())
        if kind == DATA and sink is not None:
            sink.append(b)
            continue
        if kind == CTRL and a.get("op") == op:
            return a
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--relay", default="127.0.0.1:7443")
    ap.add_argument("--code", required=True)
    ap.add_argument("--plc", default="192.168.10.240")
    a = ap.parse_args()

    print("TrustNode Link - remote probe")
    print("  relay : %s" % a.relay)
    print("  PLC   : %s" % a.plc)
    print()

    t0 = time.time()
    try:
        sec = connect(a.relay, a.code)
    except Exception as exc:
        check("paired through the relay", False, exc)
        return 2
    check("paired through the relay", True, "%.0f ms" % ((time.time()-t0)*1000))

    sec.send(bytes([CTRL]) + json.dumps({"op": "hello"}).encode())
    hello = await_op(sec, "hello", 30)
    check("  the agent described itself", bool(hello),
          (hello or {}).get("host"))
    if hello:
        for s in hello.get("subnets") or []:
            if s.get("kind", "lan") == "lan":
                print("      plant network: %s  (%s)"
                      % (s["cidr"], str(s.get("desc") or "")[:34]))

    print()
    print("[ICMP from the plant side]")
    sec.send(bytes([CTRL]) + json.dumps(
        {"op": "ping", "ip": a.plc, "count": 3}).encode())
    pr = await_op(sec, "ping", 40)
    ok = bool(pr) and not pr.get("error") and pr.get("received", 0) > 0
    check("the PLC answers on the plant network", ok,
          (pr or {}).get("error") or "%s/%s received, rtt %s ms"
          % ((pr or {}).get("received"), (pr or {}).get("sent"),
             (pr or {}).get("rtt_avg")))

    print()
    print("[TCP to the controller's CIP port]")
    sec.send(bytes([CTRL]) + json.dumps(
        {"op": "open", "sid": 1, "ip": a.plc, "port": 44818}).encode())
    o = await_op(sec, "open", 40)
    if not check("a stream opened to %s:44818" % a.plc,
                 bool(o) and o.get("ok"), (o or {}).get("error")):
        return 2

    sink = []
    sec.send(bytes([DATA]) + struct.pack(">I", 1)
             + struct.pack("<HHIIQI", 0x63, 0, 0, 0, 0, 0))
    end = time.time() + 25
    reply = b""
    while time.time() < end and len(reply) < 28:
        kind, x, body = parse(sec.recv())
        if kind == DATA:
            reply += body
    good = len(reply) >= 28 and reply[0] == 0x63 and reply[1] == 0
    name = ""
    if good:
        name = bytes(c for c in reply[40:] if 32 <= c < 127).decode(
            "ascii", "replace").strip()
    check("the controller answered CIP ListIdentity", good,
          "%d bytes" % len(reply))
    if name:
        print("      identity: %s" % name)
    check("  and it named itself", bool(name) and "1769" in name or bool(name),
          name[:50])

    print()
    print("RESULT: %s" % ("PASS - the PLC is reachable from here"
                          if not FAILS else "FAIL - " + ", ".join(FAILS)))
    return 0 if not FAILS else 2


if __name__ == "__main__":
    sys.exit(main())
