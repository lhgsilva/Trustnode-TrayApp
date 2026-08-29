# -*- coding: utf-8 -*-
"""Generic Modbus TCP device driver.

2026-08-28. Modbus TCP was already in the product but locked inside
`power_manager`, usable only by power meters. It is the widest-reach protocol
in industry - VSDs, transmitters, weighing controllers, and the gateway boxes
that front every other fieldbus all speak it - so it becomes a gateway type of
its own here, with the same shape as any other device.

This module knows nothing about gateways or the historian. It takes a register
map and returns decoded values, exactly as `ethernet_ip.py` does for CIP.

Three things it is careful about, each of which has bitten this product before:

* **A datasheet reference is not a wire offset.** `normalize_register_address`
  is shared with the meter path, where typing the printed "30005" and reading
  offset 30005 produced a silent "-" for weeks.
* **A wrong decode returns a plausible number, not an error.** Word order for
  32-bit values is explicit and per-register, never guessed, and `read_once()`
  can return the raw words beside the decoded value so an operator can check
  before saving.
* **A slow device must not stall the collection loop.** The loop caps a read at
  8 s and stamps no progress on timeout, which leaves a gateway at RUNNING with
  no error. Every read here is bounded by an explicit deadline.
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from app.services.meter_registers import normalize_register_address

# What a register can be decoded as. Width is in 16-bit Modbus words.
FORMATS: Dict[str, int] = {
    "int16": 1, "uint16": 1,
    "int32": 2, "uint32": 2,
    "float32": 2,
    "int64": 4, "uint64": 4, "float64": 4,
    "bool": 1,          # a coil/discrete input, or bit 0 of a register
}
DEFAULT_FORMAT = "float32"

# Which Modbus function serves which address space.
FUNCTIONS = ("holding", "input", "coil", "discrete")
DEFAULT_FUNCTION = "holding"

# Registers are read in merged blocks rather than one request each. These bound
# how greedy that merge is: crossing a gap larger than MERGE_GAP costs more than
# a second request, and many devices refuse a count beyond ~125 words.
MERGE_GAP = 16
MAX_SPAN = 120

# A device that stops answering mid-read must not hold the cycle. The loop's own
# cap is 8 s; stay well inside it.
DEFAULT_DEADLINE_S = 4.0


class ModbusReadError(RuntimeError):
    """The device answered with an error, or did not answer."""


@dataclass
class ModbusPoint:
    """One value to read, as the operator described it."""
    name: str
    address: int          # wire offset, already normalised
    function: str         # holding | input | coil | discrete
    kind: str             # see FORMATS
    scale: float = 1.0
    offset: float = 0.0
    unit: str = ""
    word_swap: bool = False   # swap the two 16-bit words of a 32/64-bit value
    byte_swap: bool = False   # swap bytes within each word
    bit: int = 0              # for kind == "bool" on a register

    @property
    def width(self) -> int:
        return FORMATS.get(self.kind, 2)


def points_from_config(raw: List[Dict[str, Any]]) -> List[ModbusPoint]:
    """Config rows -> points, skipping anything unusable rather than raising.

    One malformed row must not stop a gateway with fifty good ones from
    collecting; the row is simply absent and the operator sees a missing tag.
    """
    out: List[ModbusPoint] = []
    for row in raw or []:
        if not isinstance(row, dict):
            continue
        if row.get("enabled") is False:
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        try:
            addr, implied_fn = normalize_register_address(row.get("address"))
        except Exception:
            continue
        fn = str(row.get("function") or "").strip().lower()
        if fn not in FUNCTIONS:
            # A "3x:"/"4x:" style address already told us which space it is.
            fn = implied_fn if implied_fn in FUNCTIONS else DEFAULT_FUNCTION
        kind = str(row.get("kind") or DEFAULT_FORMAT).strip().lower()
        if kind not in FORMATS:
            kind = DEFAULT_FORMAT
        try:
            scale = float(row.get("scale", 1.0) or 1.0)
        except Exception:
            scale = 1.0
        try:
            off = float(row.get("offset", 0.0) or 0.0)
        except Exception:
            off = 0.0
        out.append(ModbusPoint(
            name=name, address=int(addr), function=fn, kind=kind,
            scale=scale, offset=off, unit=str(row.get("unit") or ""),
            word_swap=bool(row.get("word_swap")),
            byte_swap=bool(row.get("byte_swap")),
            bit=int(row.get("bit") or 0),
        ))
    return out


def plan_blocks(points: List[ModbusPoint]) -> Dict[str, List[Tuple[int, int]]]:
    """Merge each function's addresses into as few reads as possible.

    Returns {function: [(start, count), ...]}. Reading 40001-40010 as one
    request instead of ten is the difference between a 20 ms cycle and a 200 ms
    one on a device with any latency at all.
    """
    by_fn: Dict[str, List[Tuple[int, int]]] = {}
    for fn in FUNCTIONS:
        spans = sorted(
            (p.address, p.width) for p in points if p.function == fn
        )
        if not spans:
            continue
        blocks: List[Tuple[int, int]] = []
        start = spans[0][0]
        end = spans[0][0] + spans[0][1]          # exclusive
        for addr, width in spans[1:]:
            stop = addr + width
            if addr - end <= MERGE_GAP and (stop - start) <= MAX_SPAN:
                end = max(end, stop)
            else:
                blocks.append((start, end - start))
                start, end = addr, stop
        blocks.append((start, end - start))
        by_fn[fn] = blocks
    return by_fn


def decode_point(words: List[int], point: ModbusPoint) -> float | bool:
    """Registers -> an engineering value.

    `words` are the raw 16-bit registers for this point, in wire order.
    """
    if not words:
        raise ModbusReadError("no data for %s" % point.name)

    if point.function in ("coil", "discrete"):
        return bool(words[0])

    raw = list(words[: max(1, point.width)])
    if point.byte_swap:
        raw = [((w & 0xFF) << 8) | ((w >> 8) & 0xFF) for w in raw]
    if point.word_swap and len(raw) > 1:
        raw = list(reversed(raw))

    blob = b"".join(struct.pack(">H", w & 0xFFFF) for w in raw)

    if point.kind == "bool":
        return bool((raw[0] >> max(0, min(15, point.bit))) & 1)
    if point.kind == "int16":
        value = struct.unpack(">h", blob[:2])[0]
    elif point.kind == "uint16":
        value = struct.unpack(">H", blob[:2])[0]
    elif point.kind == "int32":
        value = struct.unpack(">i", blob[:4])[0]
    elif point.kind == "uint32":
        value = struct.unpack(">I", blob[:4])[0]
    elif point.kind == "float32":
        value = struct.unpack(">f", blob[:4])[0]
    elif point.kind == "int64":
        value = struct.unpack(">q", blob[:8])[0]
    elif point.kind == "uint64":
        value = struct.unpack(">Q", blob[:8])[0]
    elif point.kind == "float64":
        value = struct.unpack(">d", blob[:8])[0]
    else:
        value = struct.unpack(">f", blob[:4])[0]

    return float(value) * point.scale + point.offset


class ModbusTcpClientWrapper:
    """One connection to one device, with every read bounded.

    pymodbus is imported lazily so a build without it still starts - the same
    treatment every other driver in this product gets.
    """

    def __init__(self, host: str, port: int = 502, unit_id: int = 1,
                 timeout_s: float = 3.0):
        self.host = str(host or "").strip()
        self.port = int(port or 502)
        self.unit_id = int(unit_id or 1)
        self.timeout_s = float(timeout_s or 3.0)
        self._client = None

    def __enter__(self):
        try:
            from pymodbus.client import ModbusTcpClient  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise ModbusReadError(
                "Modbus TCP reader unavailable (pymodbus missing): %s" % exc) from exc
        if not self.host:
            raise ModbusReadError("Modbus read failed: the device address is empty.")
        self._client = ModbusTcpClient(host=self.host, port=self.port,
                                       timeout=self.timeout_s)
        if not self._client.connect():
            raise ModbusReadError(
                "Could not connect to %s:%d. Check the address, the port and "
                "that the device allows a TCP connection from this machine."
                % (self.host, self.port))
        return self

    def __exit__(self, *exc):
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass
        self._client = None
        return False

    def _read_block(self, function: str, start: int, count: int):
        c = self._client
        # pymodbus 3.x renamed the unit kwarg to `slave`; accept both so a
        # version bump does not silently break every Modbus gateway.
        def _call(fn, **kw):
            try:
                return fn(address=start, count=count, slave=self.unit_id, **kw)
            except TypeError:
                return fn(address=start, count=count, unit=self.unit_id, **kw)

        if function == "holding":
            return _call(c.read_holding_registers)
        if function == "input":
            return _call(c.read_input_registers)
        if function == "coil":
            return _call(c.read_coils)
        if function == "discrete":
            return _call(c.read_discrete_inputs)
        raise ModbusReadError("unknown Modbus function %r" % function)

    def read_points(self, points: List[ModbusPoint],
                    deadline_s: float = DEFAULT_DEADLINE_S) -> List[Dict[str, Any]]:
        """Read every point, in merged blocks, within the deadline.

        Returns one row per point: name, value, unit, quality, and on failure
        the reason. A point that fails yields a BAD row rather than taking the
        whole gateway down - one unreadable register must not hide the other
        forty-nine.
        """
        rows: Dict[str, Dict[str, Any]] = {
            p.name: {"name": p.name, "unit": p.unit, "value": None,
                     "quality": False, "error": "not read",
                     "is_bool": p.kind == "bool" or p.function in ("coil", "discrete")}
            for p in points
        }
        if not points:
            return []

        started = time.monotonic()
        cache: Dict[Tuple[str, int], int] = {}
        failed_blocks: Dict[Tuple[str, int, int], str] = {}

        for function, blocks in plan_blocks(points).items():
            for start, count in blocks:
                if time.monotonic() - started > deadline_s:
                    failed_blocks[(function, start, count)] = (
                        "the %.0fs read budget ran out" % deadline_s)
                    continue
                try:
                    res = self._read_block(function, start, count)
                    if res is None or (hasattr(res, "isError") and res.isError()):
                        failed_blocks[(function, start, count)] = str(res)[:120]
                        continue
                    values = getattr(res, "registers", None)
                    if values is None:
                        values = getattr(res, "bits", None) or []
                    for i, word in enumerate(values):
                        cache[(function, start + i)] = int(word)
                except Exception as exc:
                    failed_blocks[(function, start, count)] = str(exc)[:120]

        for p in points:
            words = []
            missing = False
            for i in range(max(1, p.width)):
                key = (p.function, p.address + i)
                if key not in cache:
                    missing = True
                    break
                words.append(cache[key])
            if missing:
                why = "not returned by the device"
                for (fn, start, count), reason in failed_blocks.items():
                    if fn == p.function and start <= p.address < start + count:
                        why = reason
                        break
                rows[p.name].update(value=None, quality=False, error=why)
                continue
            try:
                rows[p.name].update(value=decode_point(words, p), quality=True,
                                    error="", raw_words=words)
            except Exception as exc:
                rows[p.name].update(value=None, quality=False, error=str(exc)[:120])

        return [rows[p.name] for p in points]


def read_once(host: str, port: int, unit_id: int, points: List[ModbusPoint],
              timeout_s: float = 3.0) -> List[Dict[str, Any]]:
    """One-shot read, for the 'check it before you save it' preview.

    Returns the raw words alongside the decoded value, because a wrong word
    order or format produces a believable number rather than an error, and the
    only way to catch that is to look at both.
    """
    with ModbusTcpClientWrapper(host, port, unit_id, timeout_s) as client:
        return client.read_points(points, deadline_s=max(1.0, timeout_s))
