"""IFM IO-Link master (AL13xx family) over the IoT Core HTTP/JSON interface.

See docs/ifm-iolink-gateway-integration-plan-2026-08-24.md for the investigation
behind this. The short version:

  * An ifm IO-Link master speaks EtherNet/IP to a PLC, and *separately* serves a
    JSON tree over plain HTTP on its IoT port. We read the JSON one: it needs no
    CIP stack and no EDS file, and it leaves the customer's PLC control path
    completely untouched.
  * A port's process data comes back as a HEX STRING (`pdin`), whose layout is
    defined by that sensor's IODD. Turning "03C9" into 24.2 degC is our job, and
    it is the substance of this module.

Deliberately standalone: this module imports nothing from the rest of the app.
It knows about HTTP and bit fields, not about gateways, historians or tags. The
caller (plc_manager) adapts its output into GatewayReading, so this file can be
unit-tested against a fake master with no product wiring at all.
"""
from __future__ import annotations

import json
import struct
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ifm's own vendor id, used to recognise their sensors when offering profiles.
IFM_VENDOR_ID = 310

DEFAULT_PORT_COUNT = 8
DEFAULT_TIMEOUT_S = 3.0

# ---------------------------------------------------------------------------
# Datapoint addresses
# ---------------------------------------------------------------------------
def port_pdin_adr(port: int) -> str:
    return f"/iolinkmaster/port[{int(port)}]/iolinkdevice/pdin/getdata"


def port_productname_adr(port: int) -> str:
    return f"/iolinkmaster/port[{int(port)}]/iolinkdevice/productname/getdata"


def port_vendorid_adr(port: int) -> str:
    return f"/iolinkmaster/port[{int(port)}]/iolinkdevice/vendorid/getdata"


def port_deviceid_adr(port: int) -> str:
    return f"/iolinkmaster/port[{int(port)}]/iolinkdevice/deviceid/getdata"


def port_pin2in_adr(port: int) -> str:
    return f"/iolinkmaster/port[{int(port)}]/pin2in/getdata"


MASTER_TEMPERATURE_ADR = "/processdatamaster/temperature/getdata"
MASTER_CURRENT_ADR = "/processdatamaster/current/getdata"
DEVICE_PRODUCTCODE_ADR = "/deviceinfo/productcode/getdata"
DEVICE_SERIAL_ADR = "/deviceinfo/serialnumber/getdata"
DEVICE_APPTAG_ADR = "/devicetag/applicationtag/getdata"


# ---------------------------------------------------------------------------
# Field mapping — how a slice of pdin becomes an engineering value
# ---------------------------------------------------------------------------
@dataclass
class IfmField:
    """One decoded value inside a port's process data.

    `bit_offset` counts from the LEAST SIGNIFICANT bit of the process data, which
    is what IODD RecordItem/@bitOffset means. Getting this backwards is the
    likeliest cause of "the number is nonsense", so it is implemented once,
    here, and pinned by a test against ifm's own worked example: pdin 03C9 with
    offset 2 / length 14 is 0b11110010 = 242 counts, and at 0.1 degC per count
    that is 24.2 degC. (An MSB-first reading of the same field yields 96.9 —
    plausible enough to ship and wrong, which is why the test exists.)
    """
    name: str
    port: int
    bit_offset: int = 0
    bit_length: int = 16
    kind: str = "uint"          # uint | int | bool | float32
    scale: float = 1.0
    offset: float = 0.0
    unit: str = ""

    @staticmethod
    def from_dict(raw: Dict[str, Any]) -> "IfmField":
        return IfmField(
            name=str(raw.get("name") or "").strip(),
            port=int(raw.get("port") or 0),
            bit_offset=max(0, int(raw.get("bit_offset") or 0)),
            bit_length=max(1, int(raw.get("bit_length") or 16)),
            kind=str(raw.get("kind") or "uint").strip().lower(),
            scale=float(raw.get("scale") if raw.get("scale") is not None else 1.0),
            offset=float(raw.get("offset") or 0.0),
            unit=str(raw.get("unit") or ""),
        )


class DecodeError(ValueError):
    """The mapping does not fit the process data actually received."""


def decode_field(pdin_hex: str, fld: IfmField) -> float | bool:
    """Pull one field out of a `pdin` hex string.

    Raises DecodeError when the field runs past the end of the data — that means
    the mapping and the sensor disagree, which the operator needs told rather
    than papered over with a zero.
    """
    text = (pdin_hex or "").strip().replace(" ", "")
    if text.lower().startswith("0x"):
        text = text[2:]
    if not text:
        raise DecodeError("process data is empty")
    try:
        raw_int = int(text, 16)
    except ValueError as exc:
        raise DecodeError(f"process data is not hexadecimal: {pdin_hex!r}") from exc

    total_bits = len(text) * 4
    end = fld.bit_offset + fld.bit_length
    if end > total_bits:
        raise DecodeError(
            f"field '{fld.name}' needs bits {fld.bit_offset}..{end} but the port "
            f"returned only {total_bits} bits ({len(text)} hex chars)")

    # IODD counts bit 0 as the LSB of the process data, so the field is simply
    # shifted down by its offset and masked to its own width.
    mask = (1 << fld.bit_length) - 1
    chunk = (raw_int >> fld.bit_offset) & mask

    kind = (fld.kind or "uint").lower()
    if kind == "bool":
        return bool(chunk)
    if kind == "float32":
        if fld.bit_length != 32:
            raise DecodeError(f"field '{fld.name}': float32 needs bit_length 32")
        return float(struct.unpack(">f", struct.pack(">I", chunk))[0]) * fld.scale + fld.offset
    if kind == "int":
        # two's complement over the field's own width
        if chunk >= (1 << (fld.bit_length - 1)):
            chunk -= (1 << fld.bit_length)
    return float(chunk) * fld.scale + fld.offset


# ---------------------------------------------------------------------------
# Built-in profiles — the common case without hand-counting bits
# ---------------------------------------------------------------------------
# Each profile is a named set of fields for one sensor, with {port} substituted
# when applied. These cover ifm sensors documented in the sources; anything else
# is still fully supported by writing the fields by hand, which is the point of
# the declarative mapping.
BUILTIN_PROFILES: List[Dict[str, Any]] = [
    {
        "id": "ifm-temperature-0.1c",
        "label": "ifm temperature sensor (0.1 °C resolution)",
        "vendor_id": IFM_VENDOR_ID,
        "device_ids": [446],
        "description": "Single temperature value, 14-bit, 0.1 °C per count.",
        "fields": [
            {"name": "Temperature", "bit_offset": 2, "bit_length": 14,
             "kind": "int", "scale": 0.1, "unit": "degC"},
        ],
    },
    {
        "id": "ifm-vibration-vvb",
        "label": "ifm vibration sensor (acceleration + velocity)",
        "vendor_id": IFM_VENDOR_ID,
        "device_ids": [416, 417],
        "description": "16-bit acceleration and velocity, 0.01 per count.",
        "fields": [
            {"name": "Acceleration", "bit_offset": 0, "bit_length": 16,
             "kind": "uint", "scale": 0.01, "unit": "g"},
            {"name": "Velocity", "bit_offset": 16, "bit_length": 16,
             "kind": "uint", "scale": 0.01, "unit": "mm/s"},
            {"name": "Diagnosis", "bit_offset": 32, "bit_length": 8,
             "kind": "uint", "scale": 1.0, "unit": ""},
        ],
    },
    {
        "id": "raw-uint16",
        "label": "Raw 16-bit value (no scaling)",
        "vendor_id": 0,
        "device_ids": [],
        "description": "Starting point when the sensor's IODD is unknown.",
        "fields": [
            {"name": "Value", "bit_offset": 0, "bit_length": 16,
             "kind": "uint", "scale": 1.0, "unit": ""},
        ],
    },
]


def profile_for(vendor_id: int, device_id: int) -> Optional[Dict[str, Any]]:
    """The built-in profile matching a sensor's identity, if there is one."""
    for prof in BUILTIN_PROFILES:
        if int(prof.get("vendor_id") or 0) != int(vendor_id or 0):
            continue
        if int(device_id or 0) in [int(d) for d in prof.get("device_ids") or []]:
            return prof
    return None


def fields_from_profile(profile_id: str, port: int, prefix: str = "") -> List[IfmField]:
    """Expand a profile into concrete fields for one port."""
    prof = next((p for p in BUILTIN_PROFILES if p["id"] == profile_id), None)
    if not prof:
        return []
    out: List[IfmField] = []
    for raw in prof.get("fields") or []:
        spec = dict(raw)
        spec["port"] = port
        base = str(spec.get("name") or "value")
        spec["name"] = f"{prefix}{base}" if prefix else f"Port{port}_{base}"
        out.append(IfmField.from_dict(spec))
    return out


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
@dataclass
class IfmMasterClient:
    """Talks to one IO-Link master over the IoT Core.

    Uses `getdatamulti` so a cycle costs ONE request no matter how many ports are
    mapped — at a 1 s interval across 8 ports the per-port alternative would be
    8+ round trips every second. Masters that reject it fall back automatically
    and remember the choice.
    """
    host: str
    port: int = 80
    timeout_s: float = DEFAULT_TIMEOUT_S
    use_https: bool = False
    username: str = ""
    password: str = ""
    verify_tls: bool = False        # self-signed certificates are the norm here
    _multi_supported: Optional[bool] = field(default=None, repr=False)

    # -- low level ---------------------------------------------------------
    def _base(self) -> str:
        scheme = "https" if self.use_https else "http"
        host = (self.host or "").strip()
        if not host:
            raise ValueError("IFM master address is empty")
        default_port = 443 if self.use_https else 80
        suffix = "" if int(self.port or default_port) == default_port else f":{int(self.port)}"
        return f"{scheme}://{host}{suffix}"

    def _opener(self):
        handlers: List[Any] = []
        if self.use_https and not self.verify_tls:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        if self.username:
            mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            mgr.add_password(None, self._base(), self.username, self.password or "")
            handlers.append(urllib.request.HTTPBasicAuthHandler(mgr))
        return urllib.request.build_opener(*handlers)

    def _post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self._base() + "/", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with self._opener().open(req, timeout=self.timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    def _get(self, adr: str) -> Dict[str, Any]:
        url = self._base() + adr
        req = urllib.request.Request(url, method="GET")
        with self._opener().open(req, timeout=self.timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    # -- services ----------------------------------------------------------
    def get_value(self, adr: str) -> Any:
        """One datapoint's value, or None when the master reports a fault."""
        payload = self._get(adr)
        if int(payload.get("code") or 200) >= 400:
            return None
        return ((payload.get("data") or {}) or {}).get("value")

    def get_many(self, adrs: Iterable[str]) -> Dict[str, Tuple[Any, int]]:
        """{address: (value, diagnostic_code)} for several datapoints.

        One request when the master supports `getdatamulti`. The per-address
        code is preserved rather than collapsed: a single unplugged port must
        not make the whole cycle look broken.
        """
        wanted = [a for a in adrs if a]
        if not wanted:
            return {}
        if self._multi_supported is not False:
            try:
                reply = self._post({
                    "code": "request", "cid": 1,
                    "adr": "/getdatamulti",
                    "data": {"datatosend": wanted},
                })
                items = (reply.get("data") or {})
                if isinstance(items, dict) and items:
                    self._multi_supported = True
                    out: Dict[str, Tuple[Any, int]] = {}
                    for adr, entry in items.items():
                        if isinstance(entry, dict):
                            out[adr] = (entry.get("data"), int(entry.get("code") or 200))
                        else:
                            out[adr] = (entry, 200)
                    if out:
                        return out
                self._multi_supported = False
            except Exception:
                # Older firmware, or a master that does not implement it. Fall
                # back once and stop paying the failed attempt every cycle.
                self._multi_supported = False

        out = {}
        for adr in wanted:
            try:
                payload = self._get(adr)
                code = int(payload.get("code") or 200)
                out[adr] = (((payload.get("data") or {}) or {}).get("value"), code)
            except Exception as exc:
                out[adr] = (None, _code_from_exception(exc))
        return out

    # -- higher level ------------------------------------------------------
    def identify(self) -> Dict[str, Any]:
        """Block identity, for the dialog and for diagnostics."""
        info: Dict[str, Any] = {}
        for key, adr in (("product_code", DEVICE_PRODUCTCODE_ADR),
                         ("serial_number", DEVICE_SERIAL_ADR),
                         ("application_tag", DEVICE_APPTAG_ADR)):
            try:
                info[key] = self.get_value(adr)
            except Exception:
                info[key] = None
        return info

    def scan_ports(self, port_count: int = DEFAULT_PORT_COUNT) -> List[Dict[str, Any]]:
        """What is plugged into each port, and whether we have a profile for it.

        This is what the "Scan ports" button in the gateway dialog calls, so an
        operator sees their actual hardware instead of typing port numbers and
        hoping.
        """
        adrs: List[str] = []
        for p in range(1, max(1, int(port_count)) + 1):
            adrs += [port_productname_adr(p), port_vendorid_adr(p),
                     port_deviceid_adr(p), port_pdin_adr(p)]
        values = self.get_many(adrs)

        ports: List[Dict[str, Any]] = []
        for p in range(1, max(1, int(port_count)) + 1):
            name, name_code = values.get(port_productname_adr(p), (None, 0))
            vendor, _ = values.get(port_vendorid_adr(p), (None, 0))
            device, _ = values.get(port_deviceid_adr(p), (None, 0))
            pdin, pdin_code = values.get(port_pdin_adr(p), (None, 0))
            connected = bool(name) and int(name_code or 0) < 400
            prof = profile_for(_as_int(vendor), _as_int(device)) if connected else None
            ports.append({
                "port": p,
                "connected": connected,
                "product_name": name or "",
                "vendor_id": _as_int(vendor),
                "device_id": _as_int(device),
                "pdin": pdin or "",
                "pdin_code": int(pdin_code or 0),
                "suggested_profile": (prof or {}).get("id", ""),
                "suggested_profile_label": (prof or {}).get("label", ""),
            })
        return ports

    def read_fields(self, fields: List[IfmField]) -> List[Dict[str, Any]]:
        """Read every mapped field in ONE cycle.

        Returns one entry per field: {name, value, unit, quality, error, raw}.
        A port that fails marks only its own fields bad — the rest of the block
        keeps reporting, which is what an operator expects when one sensor is
        unplugged.
        """
        by_port: Dict[int, List[IfmField]] = {}
        for f in fields:
            if f.name and f.port:
                by_port.setdefault(int(f.port), []).append(f)
        if not by_port:
            return []

        adrs = [port_pdin_adr(p) for p in sorted(by_port)]
        values = self.get_many(adrs)

        out: List[Dict[str, Any]] = []
        for p in sorted(by_port):
            pdin, code = values.get(port_pdin_adr(p), (None, 0))
            for f in by_port[p]:
                if pdin is None or int(code or 0) >= 400:
                    out.append({"name": f.name, "value": None, "unit": f.unit,
                                "quality": 0, "raw": "",
                                "error": f"port {p} returned diagnostic code {code or 'no data'}"})
                    continue
                try:
                    val = decode_field(str(pdin), f)
                except DecodeError as exc:
                    out.append({"name": f.name, "value": None, "unit": f.unit,
                                "quality": 0, "raw": str(pdin), "error": str(exc)})
                    continue
                out.append({"name": f.name,
                            "value": float(val) if not isinstance(val, bool) else float(bool(val)),
                            "unit": f.unit, "quality": 192, "raw": str(pdin),
                            "error": "", "is_bool": isinstance(val, bool)})
        return out


# ---------------------------------------------------------------------------
def _as_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _code_from_exception(exc: Exception) -> int:
    if isinstance(exc, urllib.error.HTTPError):
        return int(exc.code)
    return 503


def fields_from_config(ifm_ports: List[Dict[str, Any]]) -> List[IfmField]:
    """Turn the saved `ifm_ports` configuration into decodable fields.

    Accepts both shapes the UI can produce: a port carrying an explicit `fields`
    list, or a port naming a built-in `profile`.
    """
    out: List[IfmField] = []
    for entry in ifm_ports or []:
        if not isinstance(entry, dict):
            continue
        if not bool(entry.get("enabled", True)):
            continue
        port = int(entry.get("port") or 0)
        if port <= 0:
            continue
        prefix = str(entry.get("prefix") or "")
        explicit = entry.get("fields")
        if isinstance(explicit, list) and explicit:
            for raw in explicit:
                if not isinstance(raw, dict):
                    continue
                spec = dict(raw)
                spec["port"] = port
                name = str(spec.get("name") or "").strip()
                if not name:
                    continue
                spec["name"] = f"{prefix}{name}" if prefix else name
                out.append(IfmField.from_dict(spec))
            continue
        profile_id = str(entry.get("profile") or "").strip()
        if profile_id:
            out.extend(fields_from_profile(profile_id, port, prefix))
    return out
