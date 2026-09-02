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
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

# How many addresses go into one getdatamulti. Firmware differs in what it will
# accept, and a request that is too large fails as a whole — so we chunk rather
# than discover the limit the hard way on a customer's block.
MULTI_CHUNK = 32

# How long one block's gettree stays cached on a client. Long enough that a
# single discovery asks once, short enough that re-cabling a port and pressing
# "Scan block" again shows the new device.
TREE_CACHE_S = 30.0

# The longest a single "Scan block" may take. Discovery makes many requests;
# without a whole-scan ceiling a slow or wrong-protocol endpoint hangs the
# dialog indefinitely (observed: >200 s against a CIP port).
DISCOVERY_BUDGET_S = 45.0
# Parallel GETs when a block has no usable getdatamulti. Sequential reads of ~28
# datapoints could not finish inside one collection cycle, which is what made a
# RUNNING gateway collect nothing at all (2026-08-26).
#
# 2026-08-27, measured against a real AL1326: eight at once makes the block
# answer 503 Service Unavailable on a DIFFERENT subset every cycle - its web
# server simply cannot service that many. Sequentially all ten addresses read
# fine. Four is the compromise: still ~4x faster than serial, and quiet enough
# that the block keeps up.
FALLBACK_WORKERS = 4
# A 503 from an embedded server means "busy", not "no such value". Retrying the
# stragglers gently is the difference between a tag that trends and a tag that
# is randomly BAD.
BUSY_RETRY_CODES = {429, 500, 502, 503, 504}

# ifm's own vendor id, used to recognise their sensors when offering profiles.
# Ports that are definitely NOT an ifm IoT Core, with what they actually are.
# Pointing the IoT (HTTP/JSON) driver at one of these is the single most common
# ifm misconfiguration: the TCP connect SUCCEEDS, so it looks reachable, then
# the peer never speaks HTTP and the request hangs instead of failing.
NON_IOT_PORTS = {
    44818: ("EtherNet/IP (CIP explicit messaging)",
            "ethernet_ip"),
    2222: ("EtherNet/IP implicit I/O (UDP)", "ethernet_ip"),
    502: ("Modbus TCP", "modbus"),
    102: ("Siemens S7 / ISO-TSAP", "siemens_snap7"),
    4840: ("OPC UA", "siemens_opcua"),
}


class IfmTransportError(RuntimeError):
    """The endpoint is reachable but is not an ifm IoT Core.

    Carries an operator-facing sentence, not a stack trace: the fix is always a
    configuration change (wrong port, or wrong protocol for this block).
    """


def probe_iot_core(host: str, port: int, timeout_s: float = 3.0) -> str:
    """Is there an ifm IoT Core (an HTTP server) here? '' when yes, else why not.

    Done with a RAW socket rather than urllib on purpose. Pointed at a CIP port
    the block's TCP connect succeeds and the peer then never returns an HTTP
    response; urllib's per-request timeout is not a bound in that state (it
    keeps waiting on a socket that dribbles or stalls), and a "Scan block" that
    should take 2 seconds ran for over 200. One connect, one write, one read,
    one verdict.
    """
    port = int(port or 80)
    # An ordinary HTTP port needs no probe: the port number tells us nothing
    # against it, and opening an extra connection costs a busy block real
    # capacity (this bench's DL EIP already refuses requests under load). The
    # whole-scan deadline is what bounds a wrong host here.
    if port in (80, 8080, 443, 8443):
        return ""
    known = NON_IOT_PORTS.get(port)
    if known:
        what, better = known
        return (f"Port {port} is the {what} port, not the ifm IoT Core port. "
                f"The IoT Core serves JSON over HTTP - set the IoT port to 80, "
                f"or change this gateway's protocol to "
                f"\"EtherNet/IP device (EDS)\" to read the block over fieldbus.")
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=max(0.5, timeout_s))
        sock.settimeout(max(0.5, timeout_s))
        sock.sendall(b"GET /deviceinfo/productcode/getdata HTTP/1.1\r\n"
                     b"Host: " + str(host).encode() + b"\r\n"
                     b"Connection: close\r\n\r\n")
        head = sock.recv(16)
    except socket.timeout:
        # INCONCLUSIVE, not a failure. A real ifm IoT Core under load takes its
        # time (this bench's DL EIP refuses ~3 of 4 requests at 1 Hz), and a
        # preflight that rejects a WORKING block is worse than the hang it was
        # added to prevent. Let discovery proceed - it now runs under a
        # whole-scan deadline, so a genuinely wrong port fails in seconds with
        # a generic message instead of hanging for ever.
        return ""
    except OSError as exc:
        return f"Could not reach {host}:{port} ({exc})."
    finally:
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass
    if not head:
        return ""      # inconclusive - see the timeout note above
    if not head.startswith(b"HTTP/"):
        return (f"{host}:{port} answered with something that is not HTTP, so it "
                f"is not an ifm IoT Core. If this block is on fieldbus, set the "
                f"gateway protocol to \"EtherNet/IP device (EDS)\".")
    return ""


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


def port_mode_adr(port: int) -> str:
    """0=deactivated, 1=DI, 2=DO, 3=IO-Link. Decides how pin 4 reads."""
    return f"/iolinkmaster/port[{int(port)}]/mode/getdata"


def port_pin4in_adr(port: int) -> str:
    """Pin 4 is the IO-Link communication line, so it only reads as a
    digital input while the port is in DI mode - probe before offering."""
    return f"/iolinkmaster/port[{int(port)}]/pin4in/getdata"


def _multi_adr(adr: str) -> str:
    """The form getdatamulti wants: the node itself, without its getter."""
    text = str(adr or "")
    return text[: -len("/getdata")] if text.endswith("/getdata") else text


def port_pdout_adr(port: int) -> str:
    """What the master is DRIVING on an output port (DO / IO-Link out)."""
    return f"/iolinkmaster/port[{int(port)}]/iolinkdevice/pdout/getdata"


def port_status_adr(port: int) -> str:
    """IO-Link port status: 0=no device, 1=deactivated, 2=port diagnostic,
    3=pre-operate, 4=operate ... Trendable as a health signal."""
    return f"/iolinkmaster/port[{int(port)}]/iolinkdevice/status/getdata"


# Per-port current draw. ifm does not put this in the same place on every
# family - the AL13xx has no per-port node at all, while masters with load
# monitoring expose one of the shapes below. Every candidate is PROBED and only
# the one that answers is offered, so this stays correct on a block we have
# never seen. See CHANNEL_DI / CHANNEL_CURRENT below for how it is labelled.
PORT_CURRENT_ADR_CANDIDATES: Tuple[str, ...] = (
    "/iolinkmaster/port[{n}]/current/getdata",
    "/iolinkmaster/port[{n}]/portcurrent/getdata",
    "/iolinkmaster/port[{n}]/powersupply/current/getdata",
    "/processdatamaster/port[{n}]/current/getdata",
    "/iolinkmaster/port[{n}]/iolinkdevice/current/getdata",
)

PORT_VOLTAGE_ADR_CANDIDATES: Tuple[str, ...] = (
    "/iolinkmaster/port[{n}]/voltage/getdata",
    "/iolinkmaster/port[{n}]/powersupply/voltage/getdata",
)


def port_current_adrs(port: int) -> List[str]:
    return [t.format(n=int(port)) for t in PORT_CURRENT_ADR_CANDIDATES]


def port_voltage_adrs(port: int) -> List[str]:
    return [t.format(n=int(port)) for t in PORT_VOLTAGE_ADR_CANDIDATES]


# How a value is labelled for the operator. The user asked for the pin data to
# arrive "as part of the tags of the device, if is a DI, DO or iolink type" —
# this is that label, carried on the datapoint and shown in the picker.
CHANNEL_DI = "DI"
CHANNEL_DO = "DO"
CHANNEL_IOLINK = "IO-Link"
CHANNEL_CURRENT = "Current"
CHANNEL_DIAGNOSTIC = "Diagnostic"

# Port mode, as the block reports it.
PORT_MODE_DEACTIVATED = 0
PORT_MODE_DI = 1
PORT_MODE_DO = 2
PORT_MODE_IOLINK = 3
PORT_MODE_LABELS = {
    PORT_MODE_DEACTIVATED: "deactivated",
    PORT_MODE_DI: "digital input",
    PORT_MODE_DO: "digital output",
    PORT_MODE_IOLINK: "IO-Link",
}


MASTER_TEMPERATURE_ADR = "/processdatamaster/temperature/getdata"
MASTER_CURRENT_ADR = "/processdatamaster/current/getdata"
MASTER_VOLTAGE_ADR = "/processdatamaster/voltage/getdata"
MASTER_SUPERVISION_ADR = "/processdatamaster/supervisionstatus/getdata"


def unit_adr_for(value_adr: str) -> str:
    """The block declares the UNIT of its own diagnostics next to the value:
    .../current/getdata -> .../current/unit/getdata. Reading it means a tag
    says "mA" because the hardware said so, not because we guessed."""
    if value_adr.endswith("/getdata"):
        return value_adr[: -len("/getdata")] + "/unit/getdata"
    return value_adr + "/unit/getdata"
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
# Device variants
# ---------------------------------------------------------------------------
# An ifm block's address layout depends on what KIND of device it is. Rather
# than a new gateway type per part number, a variant is data: how to find the
# readable values, and how to turn one into a number.
#
#   iolink_master : AL13xx/AL14xx. Values live inside a port's `pdin` HEX string
#                   and need bit extraction against the sensor's IODD.
#   io_module     : AL40xx. Each input is its OWN datapoint holding a ready
#                   integer (0/1) -- no decoding at all.
#
# Adding the next device is a new entry here plus, if needed, a decoder.
VARIANT_IOLINK_MASTER = "iolink_master"
VARIANT_IO_MODULE = "io_module"
VARIANT_AUTO = "auto"

VARIANTS: List[Dict[str, Any]] = [
    {"id": VARIANT_AUTO, "label": "Detect automatically",
     "description": "Ask the block what it is (gettree), then use the right layout."},
    {"id": VARIANT_IOLINK_MASTER, "label": "IO-Link master (AL13xx / AL14xx)",
     "description": "Sensors on IO-Link ports; values are decoded from each port's process data."},
    {"id": VARIANT_IO_MODULE, "label": "I/O module (AL40xx, e.g. AL4022)",
     "description": "Digital inputs and counters, each already a ready-to-use value."},
]


@dataclass
class IfmDatapoint:
    """One value to collect, addressed directly in the block's own tree.

    This is the UNIFIED model across every ifm device. `adr` is whatever the
    block calls the value. When `bit_length` is set the datapoint is a slice of
    a HEX process-data string (an IO-Link port's pdin); otherwise the value the
    block returns IS the value, which is the case for every AL40xx input.
    """
    name: str
    adr: str
    kind: str = "direct"          # direct | uint | int | bool | float32
    bit_offset: int = 0
    bit_length: int = 0           # 0 = take the value as-is
    scale: float = 1.0
    offset: float = 0.0
    unit: str = ""

    @staticmethod
    def from_dict(raw: Dict[str, Any]) -> "IfmDatapoint":
        return IfmDatapoint(
            name=str(raw.get("name") or "").strip(),
            adr=str(raw.get("adr") or "").strip(),
            kind=str(raw.get("kind") or "direct").strip().lower(),
            bit_offset=max(0, int(raw.get("bit_offset") or 0)),
            bit_length=max(0, int(raw.get("bit_length") or 0)),
            scale=float(raw.get("scale") if raw.get("scale") is not None else 1.0),
            offset=float(raw.get("offset") or 0.0),
            unit=str(raw.get("unit") or ""),
        )


def datapoints_from_config(raw_points: List[Dict[str, Any]]) -> List[IfmDatapoint]:
    """Saved datapoints -> readable list, duplicates dropped (first wins)."""
    out: List[IfmDatapoint] = []
    seen: set = set()
    for item in raw_points or []:
        if not isinstance(item, dict):
            continue
        if not bool(item.get("enabled", True)):
            continue
        dp = IfmDatapoint.from_dict(item)
        if not dp.name or not dp.adr:
            continue
        key = dp.name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(dp)
    return out


def value_of_datapoint(raw_value: Any, dp: IfmDatapoint) -> float | bool:
    """Turn one raw reply into an engineering value.

    Two shapes, one function: a hex process-data string that needs a bit slice,
    or a value the block already computed. The AL4022 is entirely the second
    kind, which is why it needs no IODD.
    """
    if dp.bit_length > 0:
        fld = IfmField(name=dp.name, port=0, bit_offset=dp.bit_offset,
                       bit_length=dp.bit_length, kind=(dp.kind if dp.kind != "direct" else "uint"),
                       scale=dp.scale, offset=dp.offset, unit=dp.unit)
        return decode_field(str(raw_value), fld)

    if raw_value is None:
        raise DecodeError("no value returned")
    if isinstance(raw_value, bool):
        return raw_value
    if dp.kind == "bool":
        return bool(int(raw_value))
    try:
        number = float(raw_value)
    except (TypeError, ValueError):
        # Some datapoints are strings (product names, tags). Numeric consumers
        # cannot use them, so say so rather than inventing a number.
        raise DecodeError(f"'{raw_value}' is not numeric") from None
    return number * dp.scale + dp.offset


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
    _opener_cache: Any = field(default=None, repr=False)
    # Set by the caller to bound a whole read. Monotonic; None = no bound.
    _deadline: Optional[float] = field(default=None, repr=False)
    # Filled in per read so the caller can explain a partial cycle.
    last_transport_note: str = field(default="", repr=False)

    # -- low level ---------------------------------------------------------
    def _base(self) -> str:
        scheme = "https" if self.use_https else "http"
        host = (self.host or "").strip()
        if not host:
            raise ValueError("IFM master address is empty")
        default_port = 443 if self.use_https else 80
        suffix = "" if int(self.port or default_port) == default_port else f":{int(self.port)}"
        return f"{scheme}://{host}{suffix}"

    # Set by get_many so a caller can describe the last cycle honestly.
    # Deliberately UNannotated: this is a @dataclass, and an annotated class
    # attribute would become a constructor field instead of plain state.
    last_read_total = 0
    last_read_busy = 0
    _busy_streak = 0

    def _budget(self) -> float:
        """Seconds this request may take: the smaller of the per-request timeout
        and whatever is left of the whole-read deadline. Returns 0 when the
        deadline has passed, which callers treat as "do not start"."""
        if self._deadline is None:
            return float(self.timeout_s)
        left = self._deadline - time.monotonic()
        if left <= 0:
            return 0.0
        return max(0.05, min(float(self.timeout_s), left))

    def _opener(self):
        # Built ONCE. It used to be rebuilt per request, so a 28-datapoint cycle
        # paid 28 TCP handshakes every second on top of everything else.
        if self._opener_cache is not None:
            return self._opener_cache
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
        self._opener_cache = urllib.request.build_opener(*handlers)
        return self._opener_cache

    def _post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        budget = self._budget()
        if budget <= 0:
            raise TimeoutError("read budget exhausted before the request started")
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self._base() + "/", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with self._opener().open(req, timeout=budget) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    def _get(self, adr: str) -> Dict[str, Any]:
        budget = self._budget()
        if budget <= 0:
            raise TimeoutError("read budget exhausted before the request started")
        url = self._base() + adr
        req = urllib.request.Request(url, method="GET")
        with self._opener().open(req, timeout=budget) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    def begin_read(self, budget_s: float) -> None:
        """Bound everything that follows to `budget_s` seconds in total.

        The collection loop enforces its own wall-clock cap on a read cycle and
        orphans the executor when it fires. A driver that can overrun that cap
        therefore never completes a cycle at all - so the driver, not the loop,
        has to be the thing that gives up in time."""
        try:
            budget = max(0.2, float(budget_s))
        except Exception:
            budget = float(self.timeout_s)
        self._deadline = time.monotonic() + budget

    def end_read(self) -> None:
        self._deadline = None

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
        self.last_transport_note = ""
        out: Dict[str, Tuple[Any, int]] = {}

        # 1. getdatamulti, in CHUNKS. A single 28-address request is rejected
        #    whole by some firmware (and by any block where one address in the
        #    set is unreadable), which used to condemn the entire block to the
        #    per-address path forever. Chunking contains that blast radius.
        # Counted per call so the caller can report "3 of 19 values read" and
        # decide whether the transport is worth staying on.
        self.last_read_total = len(wanted)
        self.last_read_busy = 0
        remaining = list(wanted)
        if self._multi_supported is not False:
            still: List[str] = []
            multi_ok = 0
            for i in range(0, len(remaining), MULTI_CHUNK):
                chunk = remaining[i:i + MULTI_CHUNK]
                if self._budget() <= 0:
                    still.extend(chunk)
                    continue
                try:
                    # getdatamulti addresses a NODE, not its getter: the block
                    # wants "/iolinkmaster/port[1]/pin2in", never
                    # ".../pin2in/getdata". Sent the long form it answers
                    # code 200 with an EMPTY payload - so every address looked
                    # unanswered, fell through to the per-address path, and
                    # _multi_supported latched False for the life of the
                    # client. That is why an 8-port block needed ~100 separate
                    # HTTP GETs per discovery (64s) and why concurrent reads
                    # provoked 503s. Verified against a real AL1326 on
                    # 2026-08-27: short form returns all values, long form
                    # returns none.
                    sendable = [_multi_adr(a) for a in chunk]
                    reply = self._post({
                        "code": "request", "cid": 1,
                        "adr": "/getdatamulti",
                        "data": {"datatosend": sendable},
                    })
                    items = (reply.get("data") or {})
                    if isinstance(items, dict) and items:
                        # Map the block's keys back to the caller's addresses,
                        # tolerating firmware that echoes either form.
                        back = {}
                        for a in chunk:
                            back[a] = a
                            back[_multi_adr(a)] = a
                        for adr, entry in items.items():
                            target = back.get(adr, adr)
                            if isinstance(entry, dict):
                                out[target] = (entry.get("data"),
                                               int(entry.get("code") or 200))
                            else:
                                out[target] = (entry, 200)
                        multi_ok += 1
                        # A per-address busy code inside a BATCHED reply counts
                        # too: this is how a block says "not now" without
                        # failing the request, and it is the signal that made
                        # an ifm DL EIP look like it was collecting when it was
                        # refusing three quarters of every cycle.
                        self.last_read_busy += sum(
                            1 for a in chunk
                            if int((out.get(a) or (None, 0))[1] or 0) in BUSY_RETRY_CODES)
                        # anything the block simply omitted still needs fetching
                        still.extend([a for a in chunk if a not in out])
                    else:
                        still.extend(chunk)
                except Exception:
                    still.extend(chunk)
            # Only give up on getdatamulti when NO chunk worked - one awkward
            # address must not cost the whole block its fast path.
            if multi_ok == 0 and wanted:
                self._multi_supported = False
            elif multi_ok:
                self._multi_supported = True
            remaining = still

        # 2. Per-address fallback, CONCURRENTLY and inside the deadline. The old
        #    sequential loop was the defect behind "RUNNING but nothing
        #    collected": 28 addresses x the per-request timeout could not fit in
        #    a 1 s cycle, the collection loop's own cap fired every time, and no
        #    cycle ever completed - so no rows, no error the operator could see.
        if remaining:
            timed_out: List[str] = []

            def _one(adr: str) -> Tuple[str, Tuple[Any, int]]:
                if self._budget() <= 0:
                    return adr, (None, 408)
                try:
                    payload = self._get(adr)
                    code = int(payload.get("code") or 200)
                    return adr, (((payload.get("data") or {}) or {}).get("value"), code)
                except Exception as exc:
                    return adr, (None, _code_from_exception(exc))

            # NOT adaptive on purpose. Dropping to one worker when a block
            # returns 503 was tried on 2026-08-27 and MEASURED WORSE: on an
            # "IO-Link Master DL EIP 8P IP67" it took 22% of values down to 3%,
            # because sequential reads overrun the cycle budget and every
            # address then fails with 408 instead of some succeeding. That
            # block's problem is capacity, not parallelism - the same 19 values
            # read 19/19 every time at a 5 s interval. The cure is cadence or
            # transport, and the gateway now says so (_ifm_transport_advice).
            workers = max(1, min(FALLBACK_WORKERS, len(remaining)))
            try:
                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="ifm-read") as pool:
                    for adr, res in pool.map(_one, remaining):
                        out[adr] = res
                        if res[1] == 408:
                            timed_out.append(adr)
            except Exception as exc:      # pool creation failure only
                for adr in remaining:
                    out.setdefault(adr, (None, _code_from_exception(exc)))
            # Second pass, SEQUENTIALLY, for anything the block was too busy to
            # answer. Concurrency is what caused these; retrying the same way
            # would just reproduce it.
            busy = [a for a in remaining
                    if int((out.get(a) or (None, 0))[1] or 0) in BUSY_RETRY_CODES]
            if busy:
                self._busy_streak = min(10, int(getattr(self, "_busy_streak", 0) or 0) + 1)
            else:
                self._busy_streak = 0
            for adr in busy:
                if self._budget() <= 0:
                    break
                try:
                    payload = self._get(adr)
                    code = int(payload.get("code") or 200)
                    if code < 400:
                        out[adr] = (((payload.get("data") or {}) or {}).get("value"), code)
                        if adr in timed_out:
                            timed_out.remove(adr)
                except Exception:
                    pass

            self.last_read_busy += len(busy)
            starved = len(timed_out)
            slow = sum(1 for a in remaining
                       if a not in timed_out
                       and int((out.get(a) or (None, 0))[1] or 0) >= 400)
            if starved or slow:
                # Say which of the two it is: "the block would not answer" and
                # "we ran out of time to ask" need different fixes, and an
                # operator staring at blank tags cannot tell them apart.
                bits = []
                if slow:
                    bits.append(f"{slow} timed out or were refused by the block")
                if starved:
                    bits.append(f"{starved} did not fit in the {self.timeout_s:.1f}s read budget")
                self.last_transport_note = (
                    f"{starved + slow} of {len(wanted)} value(s) failed: "
                    + "; ".join(bits)
                    + ". Raise the gateway interval or select fewer tags.")
        return out

    def preflight(self, timeout_s: float = 3.0) -> None:
        """Fail FAST and clearly when this is not an IoT Core endpoint.

        Called before any discovery. Without it a wrong port produces a hang
        and then a generic timeout, which tells the operator nothing about the
        actual mistake.
        """
        problem = probe_iot_core(self.host, self.port, timeout_s)
        if problem:
            raise IfmTransportError(problem)

    def get_tree(self) -> Dict[str, Any]:
        """The block's ENTIRE device description as JSON.

        ifm documents this as the first thing to do when talking to the IoT Core
        programmatically, and it is what makes discovery plug-and-work: rather
        than assuming port[1..8] exists and guessing what is on it, we ask the
        block to describe itself. This is the IoT-Core equivalent of installing
        a device's EDS in CODESYS and getting named I/O channels back.

            {"code": "request", "cid": -1, "adr": "gettree"}

        CACHED for a short while. One discovery asked for the tree three times
        - detect_variant, scan_ports and the address filter - and a real AL1326
        spends ~9s serialising its 930-node description, so that alone was ~28s
        of a 65s scan. The cache is short-lived so re-cabling a port and
        pressing Scan again still sees the change.
        """
        now = time.monotonic()
        cached = getattr(self, "_tree_cache", None)
        if cached is not None and now < getattr(self, "_tree_cache_until", 0.0):
            return cached
        reply = self._post({"code": "request", "cid": -1, "adr": "gettree"})
        data = reply.get("data")
        tree = data if isinstance(data, dict) else {}
        if tree:
            self._tree_cache = tree
            self._tree_cache_until = now + TREE_CACHE_S
        return tree

    def query_profile(self, profile: str = "processdata") -> List[str]:
        """Every datapoint the block tags with a profile, via `querytree`.

        This is the device telling us what is worth reading. For an AL4022 the
        "processdata" profile returns each digital input and each counter, which
        is exactly the list an operator wants to tick.
        """
        try:
            reply = self._post({"code": "request", "cid": -1, "adr": "querytree",
                                "data": {"profile": str(profile or "processdata")}})
        except Exception:
            return []
        data = reply.get("data")
        found: List[str] = []

        def _walk(node: Any) -> None:
            if isinstance(node, dict):
                adr = node.get("adr") or node.get("identifier")
                if isinstance(adr, str) and adr.startswith("/"):
                    found.append(adr)
                for value in node.values():
                    _walk(value)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(data)
        return sorted(set(found))

    def detect_variant(self) -> str:
        """Ask the block what shape it is, from its own tree."""
        try:
            tree = self.get_tree()
        except Exception:
            return VARIANT_IOLINK_MASTER
        text = json.dumps(tree)
        if '"iolinkmaster"' in text:
            return VARIANT_IOLINK_MASTER
        if '"io"' in text and "digital_input" in text:
            return VARIANT_IO_MODULE
        return VARIANT_IOLINK_MASTER

    def discover_datapoints(self, variant: str = VARIANT_AUTO,
                            port_count: int = DEFAULT_PORT_COUNT) -> Dict[str, Any]:
        """Everything this block offers to collect, whatever kind it is.

        Returns {variant, datapoints[], message}. Each datapoint is ready to be
        ticked and saved — name, address, and how to read it — so the dialog
        never asks an operator to type an address.
        """
        # One cheap check before anything else: is this even an IoT Core? A
        # wrong port used to spend minutes timing out address by address.
        self.preflight()

        # And a hard ceiling on the WHOLE discovery, so the dialog can never
        # hang: individual request timeouts do not bound a scan that makes a
        # hundred of them.
        started_deadline = self._deadline
        if started_deadline is None:
            self.begin_read(DISCOVERY_BUDGET_S)
        try:
            return self._discover_datapoints(variant, port_count)
        finally:
            if started_deadline is None:
                self.end_read()

    def _discover_datapoints(self, variant: str = VARIANT_AUTO,
                             port_count: int = DEFAULT_PORT_COUNT) -> Dict[str, Any]:
        resolved = variant if variant in (VARIANT_IOLINK_MASTER, VARIANT_IO_MODULE)             else self.detect_variant()

        if resolved == VARIANT_IO_MODULE:
            points = self._discover_io_module()
            # A non-master module reports on ITSELF too where the firmware has
            # the nodes - supply voltage, temperature, current draw. Probed
            # against the block's own tree, so a module without them pays
            # nothing and offers nothing.
            points.extend(self._probe_master_diagnostics())
            points = uniquify_points(points)
            inputs = sum(1 for pt in points
                         if pt.get("channel") in (CHANNEL_DI, CHANNEL_DO))
            return {"variant": resolved, "datapoints": points,
                    "message": f"{len(points)} value(s) on this I/O module"
                               f" ({inputs} input/output channel(s))."}

        ports = self.scan_ports(port_count=port_count)
        points = []
        for prt in ports:
            if not prt.get("connected"):
                continue
            port_no = int(prt["port"])
            profile_id = str(prt.get("suggested_profile") or "")
            adr = port_pdin_adr(port_no)
            if profile_id:
                for fld in fields_from_profile(profile_id, port_no):
                    points.append({
                        "name": fld.name, "adr": adr, "kind": fld.kind,
                        "bit_offset": fld.bit_offset, "bit_length": fld.bit_length,
                        "scale": fld.scale, "offset": fld.offset, "unit": fld.unit,
                        "source": prt.get("product_name") or f"port {port_no}",
                        "enabled": True,
                    })
            else:
                points.append({
                    "name": f"Port{port_no}_Value", "adr": adr, "kind": "uint",
                    "bit_offset": 0, "bit_length": 16, "scale": 1.0, "offset": 0.0,
                    "unit": "", "source": prt.get("product_name") or f"port {port_no}",
                    "enabled": True,
                })

        # Everything discovered so far is decoded IO-Link process data.
        for pt in points:
            pt.setdefault("channel", CHANNEL_IOLINK)

        # 2026-08-27: an IO-Link master with NOTHING plugged into its ports used
        # to offer nothing at all, so a gateway built against it collected zero
        # values and looked broken. It is not empty: a real AL1326 answers the
        # DIGITAL INPUT on every port's pin 2 whether or not an IO-Link device
        # is attached, plus its own temperature, current and voltage. Those are
        # exactly the "status of the inputs" an operator wants to trend.
        points.extend(self._probe_port_channels(ports))
        points.extend(self._probe_master_diagnostics())

        points = uniquify_points(points)
        connected = sum(1 for p in ports if p.get("connected"))
        if connected:
            msg = f"{len(points)} value(s): {connected} IO-Link port(s) plus the block's own inputs."
        else:
            msg = (f"{len(points)} value(s). No IO-Link device is plugged into this "
                   f"master, so these are its digital inputs and diagnostics.")
        # A scan cut short by the deadline returns FEWER channels than the block
        # has. Saying so beats an operator wondering why a port is missing.
        if self._deadline is not None and (self._deadline - time.monotonic()) <= 0.5:
            msg += (" The scan ran out of time, so this list may be incomplete - "
                    "the block was slow to answer. Scan again, or raise the "
                    "gateway interval and try once more.")
        return {"variant": resolved, "datapoints": points, "ports": ports,
                "message": msg}

    def _tree_adrs(self) -> Optional[set]:
        """Every address the block DECLARES, from one gettree.

        Discovery used to find the pin channels by probing ~108 candidate
        addresses over HTTP, most of which 404 on any given block. On a real
        AL1326 that took 64 seconds - long enough for the gateway to look dead
        on start and for "Scan block" to time out in the dialog.

        The block already publishes its own node list, so ask once and probe
        only what exists. Returns None when the tree cannot be read, and the
        caller then falls back to probing everything rather than offering
        nothing.
        """
        cached = getattr(self, "_tree_adr_cache", None)
        if cached is not None:
            return cached if cached else None
        try:
            tree = self.get_tree()
        except Exception:
            self._tree_adr_cache = set()
            return None

        found: set = set()

        def walk(node: Any, path: str) -> None:
            if isinstance(node, list):
                for item in node:
                    walk(item, path)
                return
            if not isinstance(node, dict):
                return
            ident = str(node.get("identifier") or "")
            here = path + "/" + ident if ident else path
            if here:
                found.add(here)
            subs = node.get("subs")
            if isinstance(subs, dict):
                subs = list(subs.values())
            for sub in subs or []:
                walk(sub, here)

        walk(tree, "")
        # Paths start with the device's own identifier (its MAC), which is not
        # part of the address an operator or the read path uses.
        root = str((tree or {}).get("identifier") or "")
        prefix = "/" + root if root else ""
        adrs = set()
        for f in found:
            if prefix and f.startswith(prefix):
                adrs.add(f[len(prefix):] or "/")
            else:
                adrs.add(f)
        self._tree_adr_cache = adrs
        return adrs or None

    def _probe_port_channels(self, ports: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Every pin on every port, labelled by what that pin actually IS.

        The user asked to trend "the data from the ports pins and current drawn
        of each pin ... if is a DI, DO or IO-Link type". A port is not one fixed
        thing: pin 4 is a digital input in DI mode, a digital output in DO mode
        and the IO-Link line in IO-Link mode, and the address that answers
        differs with it. So this asks the block for the port's MODE first, then
        offers only the channels that mode makes real.

        Everything is PROBED in one batch before it is offered. A port whose pin
        is unavailable answers 404, and offering an address that cannot be read
        is exactly how a tag ends up permanently BAD on the dashboard.

        Per-port current is offered only where the hardware has it. On an AL1326
        it does not exist (every candidate 404s) and the master total is offered
        instead; on a master with load monitoring the per-port node answers and
        each port gets its own Current tag.
        """
        if not ports:
            return []

        # Ask the block which of these nodes it actually has, so we spend HTTP
        # requests on addresses that can answer instead of on 404s.
        known = self._tree_adrs()

        # ifm's DOCUMENTED per-port addresses. These are always probed, whether
        # or not gettree mentions them.
        #
        # 2026-09-02, measured on a real AL1326 ("IO-Link Master DL EIP 8P"):
        # querytree(processdata) returned ZERO addresses and the block's
        # gettree is dominated by MQTT/configuration nodes - yet every one of
        # these answered 200 in 14-31 ms when asked by name. Gating the probe
        # on the tree therefore hid the port mode, the IO-Link device status
        # and the process data on a block that serves all three, which is
        # exactly the reported "it is not reading the IO-Link IO status".
        #
        # The tree is an OPTIMISATION - it keeps us from probing the dozen
        # speculative current/voltage spellings on a block that has none. It
        # is not an inventory, and it must not be treated as one. The block's
        # own 200/404/503 is the authority.
        _documented = set()
        for _prt in ports:
            _n = int(_prt["port"])
            _documented.update({
                port_mode_adr(_n), port_pin2in_adr(_n), port_pin4in_adr(_n),
                port_pdin_adr(_n), port_pdout_adr(_n), port_status_adr(_n),
            })

        def declared(adr: str) -> bool:
            if adr in _documented:
                return True
            return known is None or adr in known

        candidates: List[str] = []
        for prt in ports:
            n = int(prt["port"])
            for adr in (port_mode_adr(n), port_pin2in_adr(n), port_pin4in_adr(n),
                        port_pdin_adr(n), port_pdout_adr(n), port_status_adr(n)):
                if declared(adr):
                    candidates.append(adr)
            for adr in port_current_adrs(n) + port_voltage_adrs(n):
                if declared(adr):
                    candidates.append(adr)
        probed = self.get_many(candidates) if candidates else {}

        # 2026-08-27: a BUSY answer is not the same as "this pin does not
        # exist". A 404 means the block genuinely has no such node; a 503 means
        # it would not answer just then. Treating them alike meant one busy
        # moment during discovery permanently dropped a port from the gateway's
        # tag list - observed on an ifm DL EIP, where Port2_Pin4 vanished from
        # a 28-value block and nothing said why. Re-probe the busy ones,
        # sequentially and once, before concluding anything about them.
        busy = [adr for adr in candidates
                if int((probed.get(adr) or (None, 0))[1] or 0) in BUSY_RETRY_CODES]
        for adr in busy:
            if self._budget() <= 0:
                break
            try:
                payload = self._get(adr)
                code = int(payload.get("code") or 200)
                if code < 400:
                    probed[adr] = (((payload.get("data") or {}) or {}).get("value"), code)
            except Exception:
                pass

        def ok(adr: str) -> bool:
            value, code = probed.get(adr, (None, 0))
            return value is not None and int(code or 0) < 400

        out: List[Dict[str, Any]] = []
        for prt in ports:
            n = int(prt["port"])
            mode_raw, mode_code = probed.get(port_mode_adr(n), (None, 0))
            mode = _as_int(mode_raw) if int(mode_code or 0) < 400 else -1
            mode_label = PORT_MODE_LABELS.get(mode, "unknown")
            product = str(prt.get("product_name") or "").strip()

            # --- pin 2: a digital input on every port, in every mode ---------
            if ok(port_pin2in_adr(n)):
                out.append(_channel_point(
                    name="Port%d_Pin2" % n, adr=port_pin2in_adr(n),
                    channel=CHANNEL_DI, kind="bool",
                    source="port %d pin 2 - digital input" % n))

            # --- pin 4: whatever the port's mode makes it --------------------
            if ok(port_pin4in_adr(n)):
                # Firmware that exposes pin4in directly - the simplest case.
                out.append(_channel_point(
                    name="Port%d_Pin4" % n, adr=port_pin4in_adr(n),
                    channel=CHANNEL_DI, kind="bool",
                    source="port %d pin 4 - digital input" % n))
            elif mode == PORT_MODE_DI and ok(port_pdin_adr(n)):
                # An AL1326 has no pin4in node: in DI mode the pin 4 state is
                # bit 0 of that port's process data. Verified against the block
                # on 2026-08-27 - toggling the sensor flips this bit, nothing
                # else moves.
                out.append(_channel_point(
                    name="Port%d_Pin4" % n, adr=port_pdin_adr(n),
                    channel=CHANNEL_DI, kind="bool", bit_offset=0, bit_length=1,
                    source="port %d pin 4 - digital input" % n))
            elif mode == PORT_MODE_DO and ok(port_pdout_adr(n)):
                out.append(_channel_point(
                    name="Port%d_Pin4" % n, adr=port_pdout_adr(n),
                    channel=CHANNEL_DO, kind="bool", bit_offset=0, bit_length=1,
                    source="port %d pin 4 - digital output" % n))

            # --- an IO-Link port with no profile still has raw process data --
            # (a profiled device already contributed decoded fields above)
            if (mode == PORT_MODE_IOLINK and not prt.get("suggested_profile")
                    and ok(port_pdin_adr(n))):
                src = "port %d IO-Link process data" % n
                if product:
                    src += " - " + product
                out.append(_channel_point(
                    name="Port%d_PDIn" % n, adr=port_pdin_adr(n),
                    channel=CHANNEL_IOLINK, kind="uint", bit_offset=0,
                    bit_length=16, source=src))
                if ok(port_pdout_adr(n)):
                    out.append(_channel_point(
                        name="Port%d_PDOut" % n, adr=port_pdout_adr(n),
                        channel=CHANNEL_IOLINK, kind="uint", bit_offset=0,
                        bit_length=16,
                        source="port %d IO-Link output data" % n))

            # --- current drawn on this port, where the hardware has it -------
            for adr in port_current_adrs(n):
                if ok(adr):
                    out.append(_channel_point(
                        name="Port%d_Current" % n, adr=adr,
                        channel=CHANNEL_CURRENT, kind="direct",
                        unit=self._unit_of(adr, "mA"),
                        source="port %d current draw" % n))
                    break
            for adr in port_voltage_adrs(n):
                if ok(adr):
                    out.append(_channel_point(
                        name="Port%d_Voltage" % n, adr=adr,
                        channel=CHANNEL_CURRENT, kind="direct",
                        unit=self._unit_of(adr, "mV"),
                        source="port %d supply voltage" % n))
                    break

            # --- diagnostics: mode and port status, trendable like any tag ---
            if int(mode_code or 0) < 400 and mode_raw is not None:
                out.append(_channel_point(
                    name="Port%d_Mode" % n, adr=port_mode_adr(n),
                    channel=CHANNEL_DIAGNOSTIC, kind="direct", enabled=False,
                    source="port %d configured mode (%s)" % (n, mode_label)))
            if ok(port_status_adr(n)):
                out.append(_channel_point(
                    name="Port%d_Status" % n, adr=port_status_adr(n),
                    channel=CHANNEL_DIAGNOSTIC, kind="direct", enabled=False,
                    source="port %d IO-Link port status" % n))
        return out

    def _unit_of(self, value_adr: str, fallback: str = "") -> str:
        """The unit the BLOCK declares for one of its own values.

        An AL1326 answers .../current/unit/getdata with "mA" and
        .../temperature/unit/getdata with "degC" - so a tag carries the real
        engineering unit instead of one we assumed. Falls back quietly, because
        a missing unit must never cost us the datapoint itself.
        """
        unit_adr = unit_adr_for(value_adr)
        known = self._tree_adrs()
        if known is not None and unit_adr not in known:
            return fallback
        try:
            unit = self.get_value(unit_adr)
        except Exception:
            return fallback
        text = str(unit or "").strip()
        return text or fallback

    def _probe_master_diagnostics(self) -> List[Dict[str, Any]]:
        """The block's own health: temperature, current, voltage, supervision.

        On a master with no per-port current node this is where "how much is
        this block drawing" actually lives, so it is offered switched ON rather
        than hidden behind the diagnostics default.
        """
        wanted = (
            ("Master_Temperature", MASTER_TEMPERATURE_ADR, "degC",
             CHANNEL_DIAGNOSTIC, True),
            ("Master_Current", MASTER_CURRENT_ADR, "mA", CHANNEL_CURRENT, True),
            ("Master_Voltage", MASTER_VOLTAGE_ADR, "mV", CHANNEL_CURRENT, True),
            ("Master_SupervisionStatus", MASTER_SUPERVISION_ADR, "",
             CHANNEL_DIAGNOSTIC, False),
        )
        known = self._tree_adrs()
        adrs = [adr for _, adr, _, _, _ in wanted
                if known is None or adr in known]
        probed = self.get_many(adrs) if adrs else {}
        out: List[Dict[str, Any]] = []
        for label, adr, fallback_unit, channel, on in wanted:
            value, code = probed.get(adr, (None, 0))
            if value is None or int(code or 0) >= 400:
                continue
            out.append(_channel_point(
                name=label, adr=adr, channel=channel, kind="direct",
                unit=self._unit_of(adr, fallback_unit) if fallback_unit else "",
                enabled=on, source="block diagnostics"))
        return out

    def _discover_io_module(self) -> List[Dict[str, Any]]:
        """Datapoints of an AL40xx-style I/O module.

        Prefers `querytree` — the block listing its own process data. Falls back
        to walking the tree for `digital_input` leaves when the firmware has no
        querytree, so a module still configures itself either way.
        """
        points: List[Dict[str, Any]] = []
        for adr in self.query_profile("processdata"):
            points.append(_io_point_from_adr(adr))
        if points:
            return points

        try:
            tree = self.get_tree()
        except Exception:
            return []
        found: List[str] = []

        def _walk(node: Any, path: str) -> None:
            if isinstance(node, dict):
                ident = str(node.get("identifier") or "")
                here = f"{path}/{ident}" if ident else path
                if node.get("type") == "data" and "processdata" in (node.get("profiles") or []):
                    found.append(here)
                for sub in node.get("subs") or []:
                    _walk(sub, here)
            elif isinstance(node, list):
                for item in node:
                    _walk(item, path)

        # 2026-08-27: start BELOW the root. gettree's top node is identified by
        # the device's own MAC, so walking from it produced
        # "/00-02-01-AA-80-94/io/port[1]/pin2/digital_input" - an address the
        # block answers 404 to, because the MAC is not part of the path. Every
        # input a module discovered this way was therefore permanently BAD.
        # This is the fallback used when the firmware has no `querytree`, so it
        # only bit the older blocks, which is why it survived: the AL4022 test
        # exercises the querytree path.
        subs = tree.get("subs") if isinstance(tree, dict) else None
        if isinstance(subs, list):
            for sub in subs:
                _walk(sub, "")
        else:
            _walk(tree, "")
        for adr in sorted(set(found)):
            # the walker builds "/io/port[1]/pin2/digital_input"; the service is
            # appended when it is read.
            points.append(_io_point_from_adr(adr))
        return points

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
        hoping. `gettree` is asked first so the block declares its own ports —
        a 4-port master is not probed as if it had 8.
        """
        try:
            declared = ports_from_tree(self.get_tree())
        except Exception:
            declared = []
        port_list = declared or list(range(1, max(1, int(port_count)) + 1))

        adrs: List[str] = []
        for p in port_list:
            adrs += [port_productname_adr(p), port_vendorid_adr(p),
                     port_deviceid_adr(p), port_pdin_adr(p)]
        values = self.get_many(adrs)

        ports: List[Dict[str, Any]] = []
        for p in port_list:
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

    def read_datapoints(self, points: List[IfmDatapoint]) -> List[Dict[str, Any]]:
        """Read every configured datapoint in ONE cycle.

        Distinct addresses are fetched once via getdatamulti even when several
        datapoints slice the same one (two bit fields inside one IO-Link port's
        process data cost one read, not two). A datapoint that fails marks only
        itself bad, so one dead input never blanks the block.
        """
        if not points:
            return []
        adrs = sorted({dp.adr for dp in points if dp.adr})
        values = self.get_many(adrs)

        out: List[Dict[str, Any]] = []
        for dp in points:
            raw_value, code = values.get(dp.adr, (None, 0))
            if raw_value is None or int(code or 0) >= 400:
                out.append({"name": dp.name, "value": None, "unit": dp.unit,
                            "quality": 0, "raw": "",
                            "error": f"{dp.adr} returned diagnostic code {code or 'no data'}"})
                continue
            try:
                value = value_of_datapoint(raw_value, dp)
            except DecodeError as exc:
                out.append({"name": dp.name, "value": None, "unit": dp.unit,
                            "quality": 0, "raw": str(raw_value), "error": str(exc)})
                continue
            out.append({"name": dp.name,
                        "value": float(value) if not isinstance(value, bool) else float(value),
                        "unit": dp.unit, "quality": 192, "raw": str(raw_value),
                        "error": "", "is_bool": isinstance(value, bool)})
        return out

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

    Duplicate names are dropped, keeping the first. Two ports both producing
    "Temperature" would otherwise write two different values under one tag name
    every cycle — the historian would accept both and the trend would be a saw
    tooth between two sensors, which is far worse than one missing tag.
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

    deduped: List[IfmField] = []
    seen: set = set()
    for f in out:
        key = f.name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(f)
    return deduped


def ports_from_tree(tree: Dict[str, Any]) -> List[int]:
    """Port numbers the block itself declares, from a `gettree` reply.

    Falls back to an empty list when the shape is unfamiliar; the caller then
    uses the configured port count. Firmware differs in how deeply it nests
    `iolinkmaster`, so this walks rather than assuming a fixed path.
    """
    found: set = set()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key.startswith("port["):
                    try:
                        found.add(int(key.split("[")[1].split("]")[0]))
                    except Exception:
                        pass
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(tree)
    return sorted(found)


def _channel_point(name: str, adr: str, channel: str, kind: str = "direct",
                   bit_offset: int = 0, bit_length: int = 0, unit: str = "",
                   source: str = "", enabled: bool = True) -> Dict[str, Any]:
    """One discovered value, carrying the KIND of channel it came from.

    `channel` is what the operator sees next to the tag - DI, DO, IO-Link,
    Current or Diagnostic - so a list of Port3_Pin4 style names says which are
    inputs, which are outputs and which are the block reporting on itself.
    It is metadata only: the read path uses name/adr/kind exactly as before, so
    an older saved gateway with no channel on its datapoints keeps working.
    """
    return {
        "name": name, "adr": adr, "kind": kind,
        "bit_offset": int(bit_offset), "bit_length": int(bit_length),
        "scale": 1.0, "offset": 0.0, "unit": unit,
        "channel": channel, "source": source or channel,
        "enabled": bool(enabled),
    }


def channel_for_leaf(leaf: str) -> str:
    """Best guess at a channel type from an I/O module's own leaf name."""
    low = str(leaf or "").lower()
    if "digital_output" in low or low.endswith("_out") or low == "out":
        return CHANNEL_DO
    if "digital_input" in low or low.endswith("_in") or low == "in":
        return CHANNEL_DI
    if "current" in low or "voltage" in low:
        return CHANNEL_CURRENT
    if "iolink" in low or low in ("pdin", "pdout"):
        return CHANNEL_IOLINK
    return CHANNEL_DIAGNOSTIC


def uniquify_points(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Make every discovered name unique, WITHOUT losing a datapoint.

    Addresses on different branches can flatten to the same leaf - a block that
    reports both /deviceinfo/temperature and /processdatamaster/temperature
    yields "temperature" twice. datapoints_from_config drops duplicates by name
    (first wins), so the second value silently disappeared: an operator ticked a
    tag that could never produce data. Disambiguate with the parent segment
    instead, and only fall back to a numeric suffix if that still collides.
    """
    seen: Dict[str, int] = {}
    out: List[Dict[str, Any]] = []
    for p in points or []:
        name = str((p or {}).get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key not in seen:
            seen[key] = 1
            out.append(p)
            continue
        parts = [x for x in str(p.get("adr") or "").split("/") if x]
        parent = ""
        if len(parts) >= 3:
            parent = parts[-3] if parts[-1] == "getdata" else parts[-2]
        parent = "".join(ch for ch in parent if ch.isalnum())
        candidate = f"{parent}_{name}" if parent else name
        while candidate.lower() in seen:
            seen[key] += 1
            candidate = f"{parent}_{name}_{seen[key]}" if parent else f"{name}_{seen[key]}"
        seen[candidate.lower()] = 1
        q = dict(p)
        q["name"] = candidate
        out.append(q)
    return out


def _io_point_from_adr(adr: str) -> Dict[str, Any]:
    """A readable datapoint from an I/O module address.

    "/io/port[3]/pin4/digital_input" becomes the tag "Port3_Pin4", which is what
    an electrician reading the block's own labelling would expect to see on a
    chart. Counters and anything else keep their own leaf name.
    """
    clean = str(adr or "").strip()
    if clean.endswith("/getdata"):
        clean = clean[: -len("/getdata")]
    parts = [p for p in clean.split("/") if p]
    leaf = parts[-1] if parts else "value"

    port = ""
    pin = ""
    for part in parts:
        if part.startswith("port[") and part.endswith("]"):
            port = part[5:-1]
        elif part.startswith("pin"):
            pin = part[3:]

    if leaf == "digital_input" and port and pin:
        name = f"Port{port}_Pin{pin}"
        kind = "bool"
    elif port and pin:
        name = f"Port{port}_Pin{pin}_{leaf}"
        kind = "direct"
    elif port:
        name = f"Port{port}_{leaf}"
        kind = "direct"
    else:
        name = leaf.replace(".", "_")
        kind = "direct"

    # A non-master I/O module gets the SAME channel labelling as a master, so
    # both kinds of block produce tags an operator can tell apart at a glance.
    return {"name": name, "adr": clean + "/getdata", "kind": kind,
            "bit_offset": 0, "bit_length": 0, "scale": 1.0, "offset": 0.0,
            "unit": "", "channel": channel_for_leaf(leaf), "source": leaf,
            "enabled": True}
