# -*- coding: utf-8 -*-
"""Allen-Bradley POINT I/O behind a 1734-AENTR, read WITHOUT a PLC.

POINT I/O is normally consumed over a Class 1 implicit connection: a scanner
opens a Forward_Open and the adapter streams the rack's input image. That is
why every explicit read of the adapter's own assemblies returns 0 bytes, and
why TrustNode could not see this hardware at all.

But each MODULE is a CIP node in its own right, reachable by routing an
ordinary explicit request through the adapter's backplane with Unconnected_Send
(port 1, address = slot). Measured on a live 1734-AENTR/C on 2026-08-30:

    slot 1  A-B 1734-IB8S 8 In
    slot 2  1734-IB8 8 PT 24VDC SINK IN     Assembly instance 4 -> 1 byte
    slot 3  1734-OB8 8 PT 24VDC SOURCE OUT  Discrete Output Point 1..8

    one whole-module assembly read: 4.0 ms

So no implicit connection is needed, and no PLC has to own the rack. Polling is
enough, it is read-only by construction (nothing here writes an output), and a
rack costs a handful of milliseconds per cycle.

This module is deliberately standalone - it shares no code path with the
EtherNet/IP or ifm drivers, so nothing that works today can be disturbed by it.
"""
from __future__ import annotations

import socket
import struct
import re
from typing import Any, Dict, List

# CIP object classes we need.
CLS_IDENTITY = 0x01
CLS_ASSEMBLY = 0x04
CLS_DIN = 0x08          # Discrete Input Point
CLS_DOUT = 0x09         # Discrete Output Point
CLS_AIN = 0x0A          # Analog Input Point
CLS_AOUT = 0x0B         # Analog Output Point

ATTR_DATA = 3           # "value" on a point, "data" on an assembly
ATTR_STATUS = 5         # CIP status WORD - not the device-state enum
ATTR_PRODUCT_NAME = 7

DATA_ASSEMBLY = 4       # the whole-module image, when a module offers one
DEFAULT_PORT = 44818


class PointIoError(RuntimeError):
    """Anything that stops a read. Callers turn this into BAD quality."""


# --------------------------------------------------------------- encapsulation

def _encap(cmd: int, session: int, payload: bytes = b"") -> bytes:
    return struct.pack("<HHIIQI", cmd, len(payload), session, 0, 0, 0) + payload


def _get_attr(cls: int, inst: int, attr: int) -> bytes:
    """Get_Attribute_Single, 8-bit logical segments."""
    return bytes([0x0E, 0x03, 0x20, cls & 0xFF, 0x24, inst & 0xFF, 0x30, attr & 0xFF])


def _unconnected_send(embedded: bytes, slot: int) -> bytes:
    """Carry `embedded` to the module in `slot` behind the adapter.

    The route is what the earlier attempts were missing: without it every
    request is answered by the ADAPTER, which is why probing slots returned the
    same identity over and over.
    """
    route = bytes([0x01, slot & 0xFF])              # port 1 = backplane
    body = bytes([0x0A, 0xF8])                      # priority/tick, timeout
    body += struct.pack("<H", len(embedded)) + embedded
    if len(embedded) % 2:
        body += b"\x00"                             # pad to a word boundary
    body += bytes([len(route) // 2, 0x00]) + route
    return b"\x52\x02\x20\x06\x24\x01" + body


class PointIoClient:
    """One TCP session, reused for every read in a cycle.

    Reconnecting per read would cost more than the reads themselves; the
    session is opened on first use and dropped on any error so the next cycle
    starts clean.
    """

    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout_s: float = 3.0):
        self.host = str(host or "").strip()
        self.port = int(port or DEFAULT_PORT)
        self.timeout_s = float(timeout_s or 3.0)
        #: Set when a rack walk stopped early on a transport failure, so the
        #: caller can say the listing is incomplete instead of implying it is
        #: the whole rack.
        self.scan_error = ""
        self._sock: socket.socket | None = None
        self._session: int = 0

    # -- session ----------------------------------------------------------
    def close(self) -> None:
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        self._sock, self._session = None, 0

    def _connect(self) -> None:
        if self._sock is not None:
            return
        if not self.host:
            raise PointIoError("no adapter IP configured")
        try:
            s = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
            s.settimeout(self.timeout_s)
            s.sendall(_encap(0x0065, 0, struct.pack("<HH", 1, 0)))
            head = s.recv(24)
            if len(head) < 24:
                raise PointIoError("no RegisterSession reply")
            _cmd, ln, session, status = struct.unpack("<HHII", head[:12])
            if ln:
                s.recv(ln)
            if status != 0:
                raise PointIoError("RegisterSession status 0x%08X" % status)
            self._sock, self._session = s, session
        except PointIoError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise PointIoError("cannot reach %s:%s - %s" % (self.host, self.port, exc)) from exc

    def _rr(self, cip: bytes) -> bytes | None:
        self._connect()
        assert self._sock is not None
        cpf = struct.pack("<HH", 0, 0) + struct.pack("<HH", 0x00B2, len(cip)) + cip
        body = struct.pack("<IH", 0, 0) + struct.pack("<H", 2) + cpf
        try:
            self._sock.sendall(_encap(0x006F, self._session, body))
            head = self._sock.recv(24)
            if len(head) < 24:
                raise PointIoError("short reply header")
            _cmd, ln, _s, _st = struct.unpack("<HHII", head[:12])
            data = b""
            while len(data) < ln:
                chunk = self._sock.recv(ln - len(data))
                if not chunk:
                    break
                data += chunk
        except Exception as exc:
            self.close()                     # a broken session is not reusable
            raise PointIoError(str(exc)) from exc
        if len(data) < 8:
            return None
        count = struct.unpack("<H", data[6:8])[0]
        off = 8
        for _ in range(count):
            if off + 4 > len(data):
                break
            tid, tlen = struct.unpack("<HH", data[off:off + 4])
            off += 4
            item = data[off:off + tlen]
            off += tlen
            if tid == 0x00B2:
                return item
        return None

    def read(self, slot: int, cls: int, inst: int, attr: int = ATTR_DATA) -> bytes | None:
        """One routed Get_Attribute_Single. None when the object is absent."""
        rep = self._rr(_unconnected_send(_get_attr(cls, inst, attr), slot))
        if not rep or len(rep) < 3 or rep[2] != 0:
            return None                       # CIP error = "not this object"
        return rep[4:]

    # -- discovery --------------------------------------------------------
    def scan(self, max_slots: int = 8) -> List[Dict[str, Any]]:
        """Walk the backplane and describe every module that answers.

        Identity first: it proves the route reached the MODULE rather than the
        adapter, which is the failure that made an earlier attempt look like an
        empty rack.
        """
        out: List[Dict[str, Any]] = []
        for slot in range(1, max(1, int(max_slots or 8)) + 1):
            try:
                raw = self.read(slot, CLS_IDENTITY, 1, ATTR_PRODUCT_NAME)
            except PointIoError as exc:
                # An empty slot answers with a CIP error, which read() turns
                # into None - so reaching here is a TRANSPORT failure, never an
                # empty rack. Before the first module that means the adapter is
                # unreachable, and reporting it as "answered but reported no
                # modules" sent the operator to inspect a powered rack that was
                # not the problem.
                if not out:
                    raise
                self.scan_error = str(exc)    # truncated walk - say so
                break
            if not raw or len(raw) < 2:
                continue
            name = raw[1:1 + raw[0]].decode("ascii", "replace").strip()
            if not name:
                continue
            mod = {"slot": slot, "name": name}
            raw_st = self.read(slot, CLS_IDENTITY, 1, ATTR_STATUS)
            if raw_st and len(raw_st) >= 2:
                mod["health"] = decode_status(struct.unpack("<H", raw_st[:2])[0])
            # Direction from the module's own name, BEFORE probing, so the
            # probe only looks at objects that can legitimately hold its data.
            upper = name.upper()
            named = "input" if " IN" in upper else ("output" if "OUT" in upper else "")
            mod.update(self._probe_points(slot, named, name))
            if named:
                mod["kind"] = named
            # Modules with nothing readable are still REPORTED - the operator
            # should see the card is there - but they offer no datapoints, so
            # no tag is created that cannot be trusted.
            out.append(mod)
        return out

    def _probe_points(self, slot: int, named: str = "", name: str = "") -> Dict[str, Any]:
        """How this module's points are readable, and how many there are.

        Preference is one read for the whole module (Assembly instance 4, a
        single byte holding every point) because that is 4 ms instead of 8
        round trips. Modules that do not offer it fall back to the per-point
        objects.
        """
        blk = self.read(slot, CLS_ASSEMBLY, DATA_ASSEMBLY, ATTR_DATA)
        if blk is not None and len(blk) >= 1:
            cap = declared_points(name)
            return {"mode": "assembly", "cls": CLS_ASSEMBLY, "inst": DATA_ASSEMBLY,
                    "bytes": len(blk), "points": min(len(blk) * 8, cap or len(blk) * 8),
                    "kind": named or "input", "data": "bool"}
        # Only the class that MATCHES the module's declared direction. Trying
        # the other one "just in case" is what invented four input points on a
        # safety module that had nothing wired to it.
        # Discrete AND analog, but only in the direction the module declares.
        candidates = [(CLS_DIN, "input", "bool"), (CLS_AIN, "input", "analog"),
                      (CLS_DOUT, "output", "bool"), (CLS_AOUT, "output", "analog")]
        if named:
            candidates = [c for c in candidates if c[1] == named]
        cap = declared_points(name)
        for cls, kind, data in candidates:
            n = 0
            for inst in range(1, (cap or 32) + 1):
                if self.read(slot, cls, inst, ATTR_DATA) is None:
                    break
                n += 1
            if n:
                return {"mode": "points", "cls": cls, "points": min(n, cap or n),
                        "kind": kind, "data": data}
        upper = str(name or "").upper()
        safety = "IB8S" in upper or "OB8S" in upper or " SAFETY" in upper
        why = ("this is a safety module - its I/O travels over CIP Safety, which this "
               "driver does not speak" if safety
               else "no standard CIP object answered for this module's %s data"
                    % (named or "I/O"))
        return {"mode": "", "points": 0, "kind": named, "unreadable": why}

    # -- reading ----------------------------------------------------------
    def read_module(self, mod: Dict[str, Any]) -> List[float]:
        """Every point on one module, in point order."""
        slot = int(mod.get("slot") or 0)
        if str(mod.get("mode")) == "assembly":
            raw = self.read(slot, int(mod.get("cls") or CLS_ASSEMBLY),
                            int(mod.get("inst") or DATA_ASSEMBLY), ATTR_DATA)
            if raw is None:
                raise PointIoError("slot %d did not answer" % slot)
            bits: List[float] = []
            for byte in raw:
                bits.extend(1.0 if byte & (1 << b) else 0.0 for b in range(8))
            return bits[:int(mod.get("points") or len(bits))]
        cls = int(mod.get("cls") or CLS_DIN)
        analog = str(mod.get("data") or "bool") == "analog"
        out: List[float] = []
        for inst in range(1, int(mod.get("points") or 0) + 1):
            raw = self.read(slot, cls, inst, ATTR_DATA)
            if raw is None:
                raise PointIoError("slot %d point %d did not answer" % (slot, inst))
            if not analog:
                out.append(1.0 if (raw and raw[0] & 0x01) else 0.0)
            elif len(raw) >= 4:
                out.append(float(struct.unpack("<i", raw[:4])[0]))
            elif len(raw) >= 2:
                out.append(float(struct.unpack("<h", raw[:2])[0]))
            else:
                out.append(float(raw[0] if raw else 0))
        return out


# ------------------------------------------------------------------ helpers

def declared_points(name: str) -> int:
    """How many points the module says it has. 0 when it does not say.

    "1734-IE4C 4 PT CURRENT INPUT" -> 4, "A-B 1734-IB8S 8 In" -> 8. Counting
    instances until one fails over-counts: an IE4C answers on instances 5 and 6
    as well, which would have invented two channels the card does not have.
    """
    text = str(name or "")
    m = re.search(r"(\d+)\s*PT\b", text, re.I) or re.search(r"(\d+)\s*IN\b", text, re.I)
    if not m:
        return 0
    try:
        n = int(m.group(1))
    except Exception:
        return 0
    return n if 0 < n <= 64 else 0


def decode_status(word: int) -> Dict[str, Any]:
    """The Identity status word, in the terms an operator cares about."""
    w = int(word or 0)
    faulted = bool(w & 0x0400) or bool(w & 0x0100)
    return {
        "owned": bool(w & 0x0001),
        "configured": bool(w & 0x0004),
        "minor_fault": bool(w & 0x0100),
        "major_fault": bool(w & 0x0400),
        "healthy": not faulted,
        "word": w,
    }


PID_FULL_SCALE = 16383      # "Scaled for PID": 0..16383 across the range


def suggest_scale(module_name: str, data: str) -> Dict[str, Any]:
    """A starting scale for an analog channel, from the module's own name.

    Verified on a 1734-IE4C on 2026-08-30: a generator set to 10.000 mA read
    8191 counts, so 0-20 mA maps to 0..16383. Reading the raw count as
    microamps - which looked plausible because it lands inside 0..20000 - gave
    8.191 mA and sent someone looking for a wiring fault that did not exist.

    Marked `suggested` so the dialog shows it as a proposal. Anything not
    recognised stays at 1.0 and reads out in counts: a wrong scale is worse
    than an honest raw number.
    """
    if str(data) != "analog":
        return {"scale": 1.0, "offset": 0.0, "unit": "", "scale_source": "none"}
    upper = str(module_name or "").upper()
    if "CURRENT" in upper:
        return {"scale": 20.0 / PID_FULL_SCALE, "offset": 0.0, "unit": "mA",
                "scale_source": "suggested: 0-20 mA over 0..%d" % PID_FULL_SCALE}
    if "VOLTAGE" in upper or " IV" in upper:
        return {"scale": 10.0 / PID_FULL_SCALE, "offset": 0.0, "unit": "V",
                "scale_source": "suggested: 0-10 V over 0..%d" % PID_FULL_SCALE}
    return {"scale": 1.0, "offset": 0.0, "unit": "counts",
            "scale_source": "unrecognised module - raw counts, set the scale yourself"}


def tag_name(slot: int, point: int) -> str:
    """`Slot2_Pt3`. Stable, sortable, and says where to look on the panel."""
    return "Slot%d_Pt%d" % (int(slot), int(point))


def datapoints_from_scan(modules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Turn a scan into the datapoint shape the gateway config stores.

    Same field names the ifm driver produces, so the tag picker, the collect
    ticks and the widgets need no special case for POINT I/O.
    """
    out: List[Dict[str, Any]] = []
    for mod in modules or []:
        slot = int(mod.get("slot") or 0)
        data = str(mod.get("data") or "bool")
        sug = suggest_scale(str(mod.get("name") or ""), data)
        for pt in range(1, int(mod.get("points") or 0) + 1):
            out.append({
                # `name` is the operator's to change; `address` never moves,
                # so a renamed tag can still be matched back to its terminal.
                "name": tag_name(slot, pt),
                "address": tag_name(slot, pt),
                "slot": slot,
                "point": pt,
                "kind": "REAL" if data == "analog" else "BOOL",
                "channel": ("AI" if data == "analog" else "DI")
                           if str(mod.get("kind")) == "input"
                           else ("AO" if data == "analog" else "DO"),
                "source": "slot %d (%s) point %d" % (slot, mod.get("name"), pt),
                # Collect by default; unticking is how a point costs nothing.
                "enabled": True,
                "scale": sug["scale"],
                "offset": sug["offset"],
                "unit": sug["unit"],
                "scale_source": sug["scale_source"],
            })
    return out


def read_once(host: str, modules: List[Dict[str, Any]], port: int = DEFAULT_PORT,
              timeout_s: float = 3.0) -> Dict[str, float]:
    """One cycle: every configured module, keyed by tag name.

    Used by the smoke test and by anything that wants a single sample without
    holding a client. The gateway worker keeps its own client so the TCP
    session survives between cycles.
    """
    cli = PointIoClient(host, port=port, timeout_s=timeout_s)
    try:
        values: Dict[str, float] = {}
        for mod in modules or []:
            for idx, val in enumerate(cli.read_module(mod), start=1):
                values[tag_name(int(mod.get("slot") or 0), idx)] = float(val)
        return values
    finally:
        cli.close()
