# -*- coding: utf-8 -*-
"""TrustNode Link relay - the rendezvous that makes this work anywhere.

Runs on a host both sides can reach (your VPS). Both the agent (the PC on
the PLC network) and the client (the engineer) dial OUT to it, so neither
needs an open port, a port-forward, a firewall rule, or administrator
rights. That is the whole point: at a customer site you will not be allowed
to touch the firewall.

WHAT THE RELAY CAN AND CANNOT SEE
---------------------------------
It is a byte pipe. It learns:

  * a 16-byte room id, which is HMAC-SHA256(Connection Code)[:16] - the
    code itself never reaches it,
  * the two IP addresses, and how many bytes flowed.

It CANNOT read the traffic. The agent and the client run their X25519 +
ChaCha20-Poly1305 handshake end to end THROUGH this pipe, and that
handshake is confirmed with an HMAC keyed by the Connection Code. A relay
that tried to sit in the middle would have to produce that HMAC, which it
cannot without the code. So a compromised relay leaks metadata, not data.

PROTOCOL
--------
Each side opens a TCP connection and sends exactly 22 bytes:

    "TNLR1" | role ('a' agent | 'c' client) | room (16 bytes)

The relay then sends single bytes until the session starts:

    \\x00  ping     keeps an idle agent's connection alive through NAT
    \\x01  paired   the other side has arrived; everything after this
                   byte is passed through untouched
    \\x02  no agent nobody is waiting with that Connection Code
    \\x03  busy     an agent is already paired in that room
    \\x04  full     the relay is at capacity

Run:  python3 tnlink_relay.py --port 7443
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import socket
import struct
import sys
import threading
import time

MAGIC = b"TNLR1"
HELLO = len(MAGIC) + 1 + 16
PING, PAIRED, NO_AGENT, BUSY, FULL = (b"\x00", b"\x01", b"\x02", b"\x03",
                                      b"\x04")
PING_EVERY = 25.0
MAX_ROOMS = 200
PAIR_TIMEOUT = 20.0

_rooms = {}
_lock = threading.Lock()
_stats = {"paired": 0, "rejected": 0, "bytes": 0}


def log(msg: str) -> None:
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


class Room:
    __slots__ = ("agent", "client", "arrived", "ready", "since")

    def __init__(self, agent):
        self.agent = agent
        self.client = None
        self.arrived = threading.Event()
        self.ready = threading.Event()
        self.since = time.time()


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return buf


def pump(a: socket.socket, b: socket.socket, counter: list) -> None:
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
            counter[0] += len(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(sock: socket.socket, addr) -> None:
    rid = ""
    try:
        sock.settimeout(30)
        hello = recv_exact(sock, HELLO)
        if hello[:len(MAGIC)] != MAGIC:
            _stats["rejected"] += 1
            log("%s:%d sent a bad header - dropped" % addr)
            sock.close()
            return
        role = hello[len(MAGIC):len(MAGIC) + 1]
        room = hello[len(MAGIC) + 1:]
        rid = room.hex()[:12]
        sock.settimeout(None)

        if role == b"a":
            _serve_agent(sock, addr, room, rid)
        elif role == b"c":
            _serve_client(sock, addr, room, rid)
        else:
            _stats["rejected"] += 1
            sock.close()
    except (ConnectionError, OSError, socket.timeout):
        try:
            sock.close()
        except OSError:
            pass
    except Exception as exc:                       # noqa: BLE001
        log("room %s unexpected: %s: %s" % (rid, type(exc).__name__, exc))
        try:
            sock.close()
        except OSError:
            pass


def _serve_agent(sock, addr, room, rid) -> None:
    with _lock:
        if len(_rooms) >= MAX_ROOMS and room not in _rooms:
            sock.sendall(FULL)
            sock.close()
            log("room %s refused - relay is full (%d rooms)"
                % (rid, len(_rooms)))
            return
        old = _rooms.get(room)
        if old is not None:
            # A re-connecting agent replaces its own stale registration.
            try:
                old.agent.close()
            except OSError:
                pass
        r = Room(sock)
        _rooms[room] = r
    log("room %s agent waiting from %s:%d" % (rid, addr[0], addr[1]))

    try:
        # Only this thread writes to the agent socket until it is paired,
        # so a keepalive can never interleave with the pairing byte.
        while not r.arrived.wait(PING_EVERY):
            sock.sendall(PING)
        sock.sendall(PAIRED)
        r.ready.set()
    except OSError:
        with _lock:
            if _rooms.get(room) is r:
                del _rooms[room]
        log("room %s agent went away while waiting" % rid)
        try:
            sock.close()
        except OSError:
            pass
        return

    counter = [0]
    client = r.client
    t = threading.Thread(target=pump, args=(sock, client, counter),
                         daemon=True)
    t.start()
    pump(client, sock, counter)
    t.join(timeout=5)
    with _lock:
        if _rooms.get(room) is r:
            del _rooms[room]
        _stats["bytes"] += counter[0]
    log("room %s session ended, %s bytes" % (rid, "{:,}".format(counter[0])))
    for s in (sock, client):
        try:
            s.close()
        except OSError:
            pass


def _serve_client(sock, addr, room, rid) -> None:
    with _lock:
        r = _rooms.get(room)
        if r is None:
            _stats["rejected"] += 1
            sock.sendall(NO_AGENT)
            sock.close()
            log("room %s client from %s:%d - no agent waiting"
                % (rid, addr[0], addr[1]))
            return
        if r.client is not None:
            _stats["rejected"] += 1
            sock.sendall(BUSY)
            sock.close()
            log("room %s client from %s:%d - already busy"
                % (rid, addr[0], addr[1]))
            return
        r.client = sock
        r.arrived.set()
    if not r.ready.wait(PAIR_TIMEOUT):
        sock.sendall(NO_AGENT)
        sock.close()
        log("room %s agent did not complete pairing" % rid)
        return
    _stats["paired"] += 1
    sock.sendall(PAIRED)
    log("room %s PAIRED %s:%d <-> agent" % (rid, addr[0], addr[1]))
    # The agent thread owns the pumping from here.


def reaper() -> None:
    while True:
        time.sleep(60)
        now = time.time()
        with _lock:
            stale = [k for k, r in _rooms.items()
                     if r.client is None and now - r.since > 24 * 3600]
            for k in stale:
                try:
                    _rooms[k].agent.close()
                except OSError:
                    pass
                del _rooms[k]
        if stale:
            log("reaped %d room(s) idle for over a day" % len(stale))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("TNLINK_RELAY_PORT", "7443")))
    ap.add_argument("--bind", default="0.0.0.0")
    a = ap.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((a.bind, a.port))
    except OSError as exc:
        print("cannot listen on %s:%d - %s"
              % (a.bind, a.port, exc.strerror or exc), file=sys.stderr)
        return 2
    srv.listen(128)
    log("TrustNode Link relay listening on %s:%d" % (a.bind, a.port))
    log("it pairs by room id and never sees a Connection Code")
    threading.Thread(target=reaper, daemon=True).start()
    try:
        while True:
            conn, addr = srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=handle, args=(conn, addr),
                             daemon=True).start()
    except KeyboardInterrupt:
        log("stopping (paired=%d rejected=%d bytes=%s)"
            % (_stats["paired"], _stats["rejected"],
               "{:,}".format(_stats["bytes"])))
        return 0
    finally:
        srv.close()


if __name__ == "__main__":
    raise SystemExit(main())
