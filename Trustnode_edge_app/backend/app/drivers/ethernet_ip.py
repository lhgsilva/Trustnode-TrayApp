"""Generic EtherNet/IP device driver — EDS import + assembly mapping.

This is the CODESYS / ifm AE3100 model, in Python: point the edge at any
EtherNet/IP *adapter* (an ifm IO-Link block, a remote I/O rack, a drive, a
standalone sensor), import its EDS, and its cyclic assembly becomes named tags.
No PLC in the middle — the edge plays the originator role itself.

WHY EXPLICIT (Class 3) AND NOT IMPLICIT (Class 1)
-------------------------------------------------
An EtherNet/IP scanner normally opens a Class 1 *implicit* connection: a
Forward_Open, then cyclic UDP on 2222 at an RPI, with heartbeats and a
connection lifecycle to keep alive. That is the right shape for 10-50 ms control
data, and it is a large amount of state to get right inside an edge collector
that samples once a second.

The Assembly object is also readable with ordinary Class 3 *explicit* messaging:
Get_Attribute_Single on class 0x04, the assembly instance, attribute 3 returns
the very same bytes, over TCP, request/response. At a 1 s interval that is
identical data at a fraction of the complexity, on the transport our whole
collection loop is already built around — and via pycomm3, which this app
already ships for Allen-Bradley. No new dependency, nothing new to bundle.

Class 1 remains the correct answer below ~100 ms, and this module is structured
so an implicit transport can be added beside `read_assembly` later without any
of the EDS/mapping work being redone.

Standalone by design: imports nothing from the rest of the app.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# CIP constants
ASSEMBLY_CLASS = 0x04
IDENTITY_CLASS = 0x01
GET_ATTRIBUTE_SINGLE = 0x0E
ASSEMBLY_DATA_ATTRIBUTE = 3

DEFAULT_CIP_PORT = 44818
DEFAULT_TIMEOUT_S = 3.0

# Assembly data is a byte buffer in CIP's little-endian order. `struct` codes
# and widths for the types an EDS/vendor manual actually names.
CIP_TYPES: Dict[str, Tuple[str, int]] = {
    "BOOL": ("B", 1),
    "SINT": ("b", 1),
    "USINT": ("B", 1),
    "BYTE": ("B", 1),
    "INT": ("<h", 2),
    "UINT": ("<H", 2),
    "WORD": ("<H", 2),
    "DINT": ("<i", 4),
    "UDINT": ("<I", 4),
    "DWORD": ("<I", 4),
    "LINT": ("<q", 8),
    "ULINT": ("<Q", 8),
    "REAL": ("<f", 4),
    "LREAL": ("<d", 8),
}


class EdsParseError(ValueError):
    """The file is not an EDS we can read."""


class AssemblyDecodeError(ValueError):
    """The signal map does not fit the assembly data actually received."""


# ---------------------------------------------------------------------------
# EDS
# ---------------------------------------------------------------------------
def parse_eds(text: str) -> Dict[str, Any]:
    """Pull the useful facts out of an EDS file.

    EDS is an INI-like format with entries that run across lines and end in ';'.
    Real files from real vendors are inconsistent, so this is deliberately
    tolerant: it extracts identity and the assembly instances (number, name,
    size in bytes) and ignores everything it does not recognise rather than
    refusing the file. What an operator needs from an EDS is "which assembly
    instance, and how many bytes" — the rest is for a configuration tool.
    """
    if not text or "[" not in text:
        raise EdsParseError("This does not look like an EDS file.")

    # strip comments ($ to end of line) without eating '$' inside quotes
    cleaned_lines = []
    for line in text.splitlines():
        out, in_quote = [], False
        for ch in line:
            if ch == '"':
                in_quote = not in_quote
            if ch == "$" and not in_quote:
                break
            out.append(ch)
        cleaned_lines.append("".join(out))
    cleaned = "\n".join(cleaned_lines)

    sections: Dict[str, str] = {}
    current = ""
    buf: List[str] = []
    for line in cleaned.splitlines():
        stripped = line.strip()
        m = re.match(r"^\[([^\]]+)\]", stripped)
        if m:
            if current:
                sections[current.lower()] = "\n".join(buf)
            current = m.group(1).strip()
            buf = []
        elif current:
            buf.append(line)
    if current:
        sections[current.lower()] = "\n".join(buf)

    def entries(section: str) -> Dict[str, str]:
        raw = sections.get(section.lower(), "")
        out: Dict[str, str] = {}
        # entries end with ';' and may span lines
        for chunk in raw.split(";"):
            if "=" not in chunk:
                continue
            key, _, value = chunk.partition("=")
            out[key.strip().lower()] = value.strip()
        return out

    device = entries("Device")

    def _num(value: str) -> int:
        text_v = (value or "").strip().strip('"').strip()
        if not text_v:
            return 0
        try:
            if text_v.lower().startswith("0x"):
                return int(text_v, 16)
            return int(float(text_v))
        except Exception:
            return 0

    def _str(value: str) -> str:
        return (value or "").strip().strip('"').strip()

    assemblies: List[Dict[str, Any]] = []
    for key, value in entries("Assembly").items():
        m = re.match(r"^assem(\d+)$", key)
        if not m:
            continue
        instance = int(m.group(1))
        # Assem101 = "Input Data", , 64, 0x0000, ...  -> name is field 0,
        # size in BYTES is the first plain number after it.
        parts = [p.strip() for p in value.split(",")]
        name = _str(parts[0]) if parts else ""
        size = 0
        for p in parts[1:]:
            if p and re.match(r"^(0x[0-9a-fA-F]+|\d+)$", p.strip()):
                size = _num(p)
                break
        assemblies.append({"instance": instance, "name": name, "size_bytes": size})
    assemblies.sort(key=lambda a: a["instance"])

    return {
        "vendor_id": _num(device.get("vendcode", "")),
        "vendor_name": _str(device.get("vendname", "")),
        "product_code": _num(device.get("prodcode", "")),
        "product_name": _str(device.get("prodname", "")),
        "product_type": _num(device.get("prodtype", "")),
        "catalog": _str(device.get("catalog", "")),
        "assemblies": assemblies,
    }


def guess_assemblies(parsed: Dict[str, Any]) -> Dict[str, int]:
    """Best guess at which assembly is input and which is output.

    EDS does not mark direction in a way every vendor agrees on, so this reads
    the assembly NAMES — which vendors do label "Input"/"Output" — and otherwise
    offers the two largest. The UI shows the guess and lets the operator change
    it; guessing silently would be worse than asking.
    """
    out: Dict[str, int] = {}
    for asm in parsed.get("assemblies") or []:
        label = str(asm.get("name") or "").lower()
        if "input" in label and "input_assembly" not in out:
            out["input_assembly"] = int(asm["instance"])
        elif ("output" in label or "consum" in label) and "output_assembly" not in out:
            out["output_assembly"] = int(asm["instance"])
        elif "config" in label and "config_assembly" not in out:
            out["config_assembly"] = int(asm["instance"])
    if "input_assembly" not in out and parsed.get("assemblies"):
        biggest = max(parsed["assemblies"], key=lambda a: int(a.get("size_bytes") or 0))
        out["input_assembly"] = int(biggest["instance"])
    return out


# ---------------------------------------------------------------------------
# Signals — a slice of the assembly buffer as an engineering value
# ---------------------------------------------------------------------------
@dataclass
class EipSignal:
    """One named value inside an assembly's byte buffer.

    `byte_offset` is from the start of the assembly data, which is how every
    vendor manual documents it. `bit` selects a bit within that byte for BOOL.
    """
    name: str
    byte_offset: int = 0
    kind: str = "INT"
    bit: int = 0
    scale: float = 1.0
    offset: float = 0.0
    unit: str = ""

    @staticmethod
    def from_dict(raw: Dict[str, Any]) -> "EipSignal":
        return EipSignal(
            name=str(raw.get("name") or "").strip(),
            byte_offset=max(0, int(raw.get("byte_offset") or 0)),
            kind=str(raw.get("kind") or "INT").strip().upper(),
            bit=max(0, min(7, int(raw.get("bit") or 0))),
            scale=float(raw.get("scale") if raw.get("scale") is not None else 1.0),
            offset=float(raw.get("offset") or 0.0),
            unit=str(raw.get("unit") or ""),
        )


def decode_signal(data: bytes, sig: EipSignal) -> float | bool:
    """Read one signal out of an assembly buffer."""
    kind = (sig.kind or "INT").upper()
    if kind not in CIP_TYPES:
        raise AssemblyDecodeError(f"signal '{sig.name}': unknown type {sig.kind!r}")
    fmt, width = CIP_TYPES[kind]
    end = sig.byte_offset + width
    if end > len(data):
        raise AssemblyDecodeError(
            f"signal '{sig.name}' needs bytes {sig.byte_offset}..{end} but the "
            f"assembly returned only {len(data)} bytes")
    chunk = data[sig.byte_offset:end]
    if kind == "BOOL":
        return bool(chunk[0] & (1 << sig.bit))
    value = struct.unpack(fmt if len(fmt) > 1 else "<" + fmt, chunk)[0]
    return float(value) * sig.scale + sig.offset


def signals_from_config(raw_signals: List[Dict[str, Any]]) -> List[EipSignal]:
    out: List[EipSignal] = []
    for item in raw_signals or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out.append(EipSignal.from_dict(item))
    return out


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
@dataclass
class EipDeviceClient:
    """Reads an EtherNet/IP adapter's assembly with explicit CIP messaging.

    One Get_Attribute_Single per cycle returns the whole input assembly, which
    is then sliced into signals. That is one request for the entire device, no
    matter how many tags are mapped.
    """
    host: str
    slot: int = 0
    timeout_s: float = DEFAULT_TIMEOUT_S

    def _path(self) -> str:
        host = (self.host or "").strip()
        if not host:
            raise ValueError("EtherNet/IP device address is empty")
        return host

    def _driver(self):
        try:
            from pycomm3 import CIPDriver  # already a dependency (Allen-Bradley)
        except Exception as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                f"EtherNet/IP driver unavailable (pycomm3 missing): {exc}") from exc
        return CIPDriver(self._path())

    def read_assembly(self, instance: int) -> bytes:
        """The raw bytes of one assembly instance."""
        from pycomm3 import Services, ClassCode  # type: ignore
        with self._driver() as drv:
            reply = drv.generic_message(
                service=Services.get_attribute_single,
                class_code=ClassCode.assembly,
                instance=int(instance),
                attribute=ASSEMBLY_DATA_ATTRIBUTE,
                connected=False,
                name=f"assembly_{int(instance)}",
            )
            if not reply:
                raise RuntimeError(
                    f"assembly {instance} read failed: {getattr(reply, 'error', 'no reply')}")
            value = getattr(reply, "value", None)
            if isinstance(value, bytes):
                return value
            if isinstance(value, (bytearray, memoryview)):
                return bytes(value)
            raise RuntimeError(
                f"assembly {instance} returned {type(value).__name__}, expected bytes")

    def identify(self) -> Dict[str, Any]:
        """Identity object (class 0x01) — vendor, product code, serial, name.

        Used to confirm the device on the other end is the one the EDS
        describes, before an operator maps 60 bytes of offsets against it.
        """
        from pycomm3 import CIPDriver  # type: ignore
        try:
            with self._driver() as drv:
                info = drv.list_identity(self._path()) if hasattr(drv, "list_identity") else None
        except Exception:
            info = None
        if not info:
            try:
                info = CIPDriver.list_identity(self._path())
            except Exception:
                info = None
        return dict(info or {})

    def read_signals(self, instance: int, signals: List[EipSignal]) -> List[Dict[str, Any]]:
        """One assembly read, sliced into every mapped signal.

        A failure is reported per signal so the caller can mark those tags BAD
        without pretending the device returned a value.
        """
        if not signals:
            return []
        try:
            data = self.read_assembly(instance)
        except Exception as exc:
            return [{"name": s.name, "value": None, "unit": s.unit, "quality": 0,
                     "raw": "", "error": f"{type(exc).__name__}: {exc}"} for s in signals]

        raw_hex = data.hex()
        out: List[Dict[str, Any]] = []
        for sig in signals:
            try:
                value = decode_signal(data, sig)
            except AssemblyDecodeError as exc:
                out.append({"name": sig.name, "value": None, "unit": sig.unit,
                            "quality": 0, "raw": raw_hex, "error": str(exc)})
                continue
            out.append({"name": sig.name,
                        "value": float(value) if not isinstance(value, bool) else float(value),
                        "unit": sig.unit, "quality": 192, "raw": raw_hex,
                        "error": "", "is_bool": isinstance(value, bool)})
        return out


def discover_devices(broadcast: str = "255.255.255.255") -> List[Dict[str, Any]]:
    """EtherNet/IP devices answering a ListIdentity broadcast on the subnet.

    This is how a configuration tool finds adapters without being told their
    addresses; pycomm3 implements the broadcast for us.
    """
    try:
        from pycomm3 import CIPDriver  # type: ignore
        return [dict(d) for d in (CIPDriver.discover() or [])]
    except Exception:
        return []
