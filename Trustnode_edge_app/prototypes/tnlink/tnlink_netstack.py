# -*- coding: utf-8 -*-
"""A userspace network stack on the plant side, so no NAT is needed.

    engineer's PC                         plant PC
    -------------                         --------
    RSLinx -> LS-Link (L3 adapter)        this stack
              raw IP over the relay  -->  terminates the TCP
                                          and opens an ORDINARY socket
                                          to 192.168.10.240:44818

The device answers the plant PC, because as far as it is concerned the
plant PC is who called. That is SNAT, done in the application instead of
by the operating system - which is exactly how Tailscale's subnet routers
work on Windows, where `--snat-subnet-routes` is Linux-only and Windows
routers therefore always SNAT.

Why it is worth the trouble:

  * no Windows bridge - measured, it forwards 0 frames to a TAP port,
  * no WinNAT - needs Hyper-V, which would break the VMware the PLC
    software runs in,
  * no ICS - it engaged and translated nothing,
  * no administrator rights on the plant PC at all, and nothing on the
    plant network changes.

What it handles: ICMP echo (so `ping` works from a normal command
prompt), TCP (any port, any device), and UDP. What it does not: raw
broadcast discovery, because this is Layer 3 - RSLinx must use its
"Ethernet devices" driver, not the broadcast-browsing one.
"""
from __future__ import annotations

import socket
import struct
import threading
import time

MSS = 1300              # keep segments inside the relay's frame budget
WINDOW = 65535
IDLE_CLOSE = 300.0


def csum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    s = 0
    for i in range(0, len(data), 2):
        s += (data[i] << 8) | data[i + 1]
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def ip_hdr(src: bytes, dst: bytes, proto: int, payload_len: int,
           ident: int = 0) -> bytes:
    h = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + payload_len, ident,
                    0x4000, 64, proto, 0, src, dst)
    return h[:10] + struct.pack(">H", csum(h)) + h[12:]


def tcp_seg(src: bytes, dst: bytes, sport: int, dport: int, seq: int,
            ack: int, flags: int, payload: bytes = b"",
            window: int = WINDOW, opts: bytes = b"") -> bytes:
    if len(opts) % 4:
        opts += b"\x00" * (4 - len(opts) % 4)
    off = (5 + len(opts) // 4) << 4
    hdr = struct.pack(">HHIIBBHHH", sport, dport, seq, ack, off, flags,
                      window, 0, 0) + opts
    pseudo = src + dst + struct.pack(">BBH", 0, 6, len(hdr) + len(payload))
    c = csum(pseudo + hdr + payload)
    hdr = hdr[:16] + struct.pack(">H", c) + hdr[18:]
    return ip_hdr(src, dst, 6, len(hdr) + len(payload)) + hdr + payload


class Conn:
    """One TCP connection, terminated here and re-opened as a socket."""

    __slots__ = ("key", "src", "dst", "sport", "dport", "sock", "state",
                 "snd_nxt", "snd_una", "rcv_nxt", "sent", "last", "lock",
                 "reader", "closing")

    def __init__(self, key, src, dst, sport, dport, isn):
        self.key = key
        self.src, self.dst = src, dst
        self.sport, self.dport = sport, dport
        self.sock = None
        self.state = "SYN_RCVD"
        self.snd_nxt = isn
        self.snd_una = isn
        self.rcv_nxt = 0
        self.sent = []          # [(seq, payload)] awaiting acknowledgement
        self.last = time.time()
        self.lock = threading.Lock()
        self.reader = None
        self.closing = False


class NetStack:
    """IPv4 in, IPv4 out. Everything in between uses ordinary sockets."""

    def __init__(self, send_packet, allow, log=print):
        self.send = send_packet          # callable(bytes) -> to the engineer
        self.allow = allow               # [ip_network] the engineer may reach
        self.log = log
        self.conns = {}
        self.udp = {}
        self.lock = threading.Lock()
        self.alive = True
        self.stats = {"tcp": 0, "udp": 0, "icmp": 0, "refused": 0}
        threading.Thread(target=self._reaper, daemon=True).start()

    # -- policy -----------------------------------------------------------
    def _permitted(self, dst4: bytes) -> bool:
        import ipaddress
        try:
            a = ipaddress.ip_address(socket.inet_ntoa(dst4))
        except ValueError:
            return False
        return any(a in n for n in self.allow)

    # -- entry point ------------------------------------------------------
    def on_packet(self, pkt: bytes) -> None:
        if len(pkt) < 20 or (pkt[0] >> 4) != 4:
            return
        ihl = (pkt[0] & 0x0F) * 4
        proto = pkt[9]
        src, dst = pkt[12:16], pkt[16:20]
        if not self._permitted(dst):
            self.stats["refused"] += 1
            return
        body = pkt[ihl:]
        try:
            if proto == 1:
                self._icmp(src, dst, body)
            elif proto == 6:
                self._tcp(src, dst, body)
            elif proto == 17:
                self._udp(src, dst, body)
        except Exception as exc:                            # noqa: BLE001
            self.log("netstack: %s: %s" % (type(exc).__name__, exc))

    # -- ICMP: make ping work from the engineer's own prompt --------------
    def _icmp(self, src, dst, body) -> None:
        if len(body) < 8 or body[0] != 8:
            return
        self.stats["icmp"] += 1
        target = socket.inet_ntoa(dst)

        def go():
            from tnlink import icmp_once
            r = icmp_once(target, 1200)
            if not r.get("ok"):
                return                      # silence reads as packet loss
            rep = bytearray(body)
            rep[0] = 0                      # echo reply
            rep[2:4] = b"\x00\x00"
            rep[2:4] = struct.pack(">H", csum(bytes(rep)))
            self.send(ip_hdr(dst, src, 1, len(rep)) + bytes(rep))

        threading.Thread(target=go, daemon=True).start()

    # -- TCP --------------------------------------------------------------
    def _tcp(self, src, dst, seg) -> None:
        if len(seg) < 20:
            return
        sport, dport, seq, ack = struct.unpack(">HHII", seg[:12])
        off = (seg[12] >> 4) * 4
        flags = seg[13]
        payload = seg[off:]
        key = (src, dst, sport, dport)
        syn, ackf, fin, rst = (flags & 2), (flags & 16), (flags & 1), (flags & 4)

        with self.lock:
            conn = self.conns.get(key)

        if rst:
            if conn:
                self._drop(conn)
            return

        if syn and not ackf:
            if conn:
                return
            self.stats["tcp"] += 1
            conn = Conn(key, src, dst, sport, dport,
                        int(time.time() * 1000) & 0xFFFFFFFF)
            conn.rcv_nxt = (seq + 1) & 0xFFFFFFFF
            with self.lock:
                self.conns[key] = conn
            threading.Thread(target=self._open, args=(conn,),
                             daemon=True).start()
            return

        if not conn:
            # Nothing here knows this connection; tell the far side so it
            # stops retrying instead of timing out.
            self.send(tcp_seg(dst, src, dport, sport, ack, 0, 0x04))
            return

        conn.last = time.time()
        if ackf:
            with conn.lock:
                conn.snd_una = ack
                conn.sent = [(s, p) for s, p in conn.sent
                             if ((s + len(p)) & 0xFFFFFFFF) > ack
                             or (s + len(p)) > 0xFFFFFFFF]
            if conn.state == "SYN_RCVD":
                conn.state = "ESTABLISHED"

        if payload and conn.sock:
            if seq == conn.rcv_nxt:
                try:
                    conn.sock.sendall(payload)
                except OSError:
                    self._drop(conn)
                    return
                conn.rcv_nxt = (conn.rcv_nxt + len(payload)) & 0xFFFFFFFF
            # Acknowledge what we have, in order. Anything out of order is
            # left for the far side to retransmit - correct, if plain.
            self.send(tcp_seg(dst, src, dport, sport, conn.snd_nxt,
                              conn.rcv_nxt, 0x10))

        if fin:
            conn.rcv_nxt = (conn.rcv_nxt + 1) & 0xFFFFFFFF
            self.send(tcp_seg(dst, src, dport, sport, conn.snd_nxt,
                              conn.rcv_nxt, 0x11))
            conn.snd_nxt = (conn.snd_nxt + 1) & 0xFFFFFFFF
            self._drop(conn)

    def _open(self, conn: Conn) -> None:
        target = socket.inet_ntoa(conn.dst)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(8.0)
        try:
            s.connect((target, conn.dport))
        except OSError:
            s.close()
            # Refused here must look refused there, or the engineer's
            # program waits for a timeout that never explains itself.
            self.send(tcp_seg(conn.dst, conn.src, conn.dport, conn.sport,
                              conn.snd_nxt, conn.rcv_nxt, 0x14))
            self._drop(conn)
            return
        s.settimeout(None)
        conn.sock = s
        opts = struct.pack(">BBH", 2, 4, MSS)
        self.send(tcp_seg(conn.dst, conn.src, conn.dport, conn.sport,
                          conn.snd_nxt, conn.rcv_nxt, 0x12, b"", WINDOW,
                          opts))
        conn.snd_nxt = (conn.snd_nxt + 1) & 0xFFFFFFFF
        conn.reader = threading.Thread(target=self._pump, args=(conn,),
                                       daemon=True)
        conn.reader.start()

    def _pump(self, conn: Conn) -> None:
        while self.alive and not conn.closing:
            try:
                data = conn.sock.recv(MSS)
            except OSError:
                break
            if not data:
                break
            with conn.lock:
                seq = conn.snd_nxt
                conn.snd_nxt = (conn.snd_nxt + len(data)) & 0xFFFFFFFF
                conn.sent.append((seq, data))
            conn.last = time.time()
            self.send(tcp_seg(conn.dst, conn.src, conn.dport, conn.sport,
                              seq, conn.rcv_nxt, 0x18, data))
        # The device closed: pass that on rather than leaving it hanging.
        if not conn.closing:
            self.send(tcp_seg(conn.dst, conn.src, conn.dport, conn.sport,
                              conn.snd_nxt, conn.rcv_nxt, 0x11))
            conn.snd_nxt = (conn.snd_nxt + 1) & 0xFFFFFFFF
        self._drop(conn)

    def _drop(self, conn: Conn) -> None:
        conn.closing = True
        with self.lock:
            self.conns.pop(conn.key, None)
        try:
            if conn.sock:
                conn.sock.close()
        except OSError:
            pass

    # -- UDP --------------------------------------------------------------
    def _udp(self, src, dst, seg) -> None:
        if len(seg) < 8:
            return
        sport, dport = struct.unpack(">HH", seg[:4])
        payload = seg[8:]
        key = (src, dst, sport, dport)
        with self.lock:
            s = self.udp.get(key)
            if s is None:
                self.stats["udp"] += 1
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.settimeout(30)
                self.udp[key] = s
                threading.Thread(target=self._udp_back,
                                 args=(key, s, src, dst, sport, dport),
                                 daemon=True).start()
        try:
            s.sendto(payload, (socket.inet_ntoa(dst), dport))
        except OSError:
            pass

    def _udp_back(self, key, s, src, dst, sport, dport) -> None:
        while self.alive:
            try:
                data, _ = s.recvfrom(2048)
            except OSError:
                break
            body = struct.pack(">HHHH", dport, sport, 8 + len(data), 0)
            self.send(ip_hdr(dst, src, 17, len(body) + len(data))
                      + body + data)
        with self.lock:
            self.udp.pop(key, None)
        try:
            s.close()
        except OSError:
            pass

    # -- housekeeping -----------------------------------------------------
    def _reaper(self) -> None:
        while self.alive:
            time.sleep(30)
            now = time.time()
            with self.lock:
                stale = [c for c in self.conns.values()
                         if now - c.last > IDLE_CLOSE]
            for c in stale:
                self._drop(c)

    def summary(self) -> str:
        with self.lock:
            live = len(self.conns)
        return ("%d TCP connection(s) served, %d UDP flow(s), %d ping(s), "
                "%d refused; %d open now"
                % (self.stats["tcp"], self.stats["udp"], self.stats["icmp"],
                   self.stats["refused"], live))

    def down(self) -> None:
        """Alias for stop().

        2026-10-22, from a live session: the server tears a share down with
        `self.tunnel.down()`, because on the CLIENT the tunnel is an
        Endpoint (tnlink_tun.py) and that is its name there. On the SERVER
        the tunnel is this NetStack, which only had stop() - so ending a
        share raised

            AttributeError: 'NetStack' object has no attribute 'down'

        and the operator saw "LINK DROPPED" with the session killed instead
        of closed. Both ends now answer to the same name.
        """
        self.stop()

    def stop(self) -> None:
        self.alive = False
        with self.lock:
            conns = list(self.conns.values())
            socks = list(self.udp.values())
            self.conns.clear()
            self.udp.clear()
        for c in conns:
            try:
                c.closing = True
                if c.sock:
                    c.sock.close()
            except OSError:
                pass
        for s in socks:
            try:
                s.close()
            except OSError:
                pass
