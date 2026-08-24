# -*- coding: utf-8 -*-
"""IFM IO-Link driver: decoding and transport, against a FAKE master.

No hardware and no product wiring — the driver is deliberately standalone, so
this proves it in isolation. The fake master serves the response shapes taken
from ifm's documentation and a working open-source client (see
docs/ifm-iolink-gateway-integration-plan-2026-08-24.md).
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from app.drivers.ifm_iolink import (  # noqa: E402
    DecodeError, IfmField, IfmMasterClient, decode_field, fields_from_config,
    fields_from_profile, port_pdin_adr, profile_for,
)

FAILS = []


def check(name, ok, detail=""):
    print(f"  {name:58s}: {'PASS' if ok else 'FAIL'}{(' - ' + str(detail)[:120]) if detail else ''}")
    if not ok:
        FAILS.append(name)


# --------------------------------------------------------------------- decode
print("[DECODE]")

# ifm's own worked example: bits 2..15 carry the temperature, 0.1 degC per
# count, and 242 counts must read as 24.2 degC. This pins the bit convention:
# IODD counts bit 0 from the LSB, so 0x03C9 >> 2 = 242. Reading the same field
# MSB-first gives 96.9 — plausible enough to ship, and wrong.
temp = IfmField(name="T", port=1, bit_offset=2, bit_length=14, kind="int",
                scale=0.1, unit="degC")
value = decode_field("03C9", temp)
check("ifm worked example: 03C9 -> 24.2 degC", abs(value - 24.2) < 1e-9, value)

check("uint field", decode_field("00FF", IfmField("v", 1, 0, 16, "uint")) == 255.0)
check("scaled uint", decode_field("0064", IfmField("v", 1, 0, 16, "uint", scale=0.01)) == 1.0)
check("offset applied",
      decode_field("0000", IfmField("v", 1, 0, 16, "uint", scale=1.0, offset=-40.0)) == -40.0)

# two's complement over the field's OWN width, not 16 bits
check("signed negative", decode_field("FFFF", IfmField("v", 1, 0, 16, "int")) == -1.0)
check("signed negative, narrow field",
      decode_field("F000", IfmField("v", 1, 12, 4, "int")) == -1.0,
      decode_field("F000", IfmField("v", 1, 12, 4, "int")))

check("bool set (bit 15 is the MSB of a 16-bit word)",
      decode_field("8000", IfmField("b", 1, 15, 1, "bool")) is True)
check("bool clear", decode_field("8000", IfmField("b", 1, 14, 1, "bool")) is False)

# a field in the middle of the word
check("mid-word slice", decode_field("0F00", IfmField("v", 1, 8, 4, "uint")) == 15.0,
      decode_field("0F00", IfmField("v", 1, 8, 4, "uint")))

check("float32", abs(decode_field("3F800000", IfmField("f", 1, 0, 32, "float32")) - 1.0) < 1e-6)

# hygiene
check("0x prefix tolerated", decode_field("0x00FF", IfmField("v", 1, 0, 16, "uint")) == 255.0)
try:
    decode_field("00FF", IfmField("v", 1, 0, 32, "uint"))
    check("a field past the end of the data is refused", False, "no error raised")
except DecodeError as exc:
    check("a field past the end of the data is refused", "only 16 bits" in str(exc), str(exc)[:70])
try:
    decode_field("ZZZZ", IfmField("v", 1, 0, 16, "uint"))
    check("non-hex data is refused", False, "no error raised")
except DecodeError:
    check("non-hex data is refused", True)

# -------------------------------------------------------------------- profiles
print("\n[PROFILES]")
prof = profile_for(310, 446)
check("ifm temperature sensor matches a profile", bool(prof), (prof or {}).get("id"))
check("an unknown sensor matches nothing", profile_for(999, 1) is None)
flds = fields_from_profile("ifm-vibration-vvb", 3)
check("profile expands to fields for the port", len(flds) == 3 and all(f.port == 3 for f in flds),
      [f.name for f in flds])
check("profile field names are port-qualified", flds[0].name == "Port3_Acceleration", flds[0].name)

cfg = [
    {"port": 1, "enabled": True, "profile": "ifm-temperature-0.1c"},
    {"port": 2, "enabled": False, "profile": "ifm-temperature-0.1c"},
    {"port": 4, "enabled": True, "prefix": "Tank_",
     "fields": [{"name": "Level", "bit_offset": 0, "bit_length": 16, "kind": "uint", "scale": 0.1}]},
]
built = fields_from_config(cfg)
names = [f.name for f in built]
check("config expands enabled ports only", len(built) == 2, names)
check("explicit fields honour the prefix", "Tank_Level" in names, names)
check("a disabled port contributes nothing", not any(f.port == 2 for f in built), names)


# ------------------------------------------------------------------- transport
print("\n[TRANSPORT against a fake master]")

STATE = {"multi_enabled": True, "multi_calls": 0, "get_calls": 0}
PDIN = {1: "03C9", 3: "04D2", 5: "00FF"}


class FakeMaster(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _value_for(self, adr):
        for p, hexval in PDIN.items():
            if adr == port_pdin_adr(p):
                return hexval, 200
        if "/productname/" in adr:
            port = int(adr.split("port[")[1].split("]")[0])
            return ("TA2105", 200) if port in PDIN else (None, 404)
        if "/vendorid/" in adr:
            port = int(adr.split("port[")[1].split("]")[0])
            return (310, 200) if port in PDIN else (None, 404)
        if "/deviceid/" in adr:
            port = int(adr.split("port[")[1].split("]")[0])
            return (446, 200) if port in PDIN else (None, 404)
        if adr.startswith("/iolinkmaster/port["):
            return None, 404          # an empty port
        if "productcode" in adr:
            return "AL1326", 200
        if "serialnumber" in adr:
            return "000123456789", 200
        return None, 404

    def do_GET(self):
        STATE["get_calls"] += 1
        value, code = self._value_for(self.path)
        self._send({"cid": 1, "data": {"value": value}, "code": code})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode() or "{}")
        if body.get("adr") == "/getdatamulti":
            if not STATE["multi_enabled"]:
                self._send({"cid": 1, "code": 400}, status=400)
                return
            STATE["multi_calls"] += 1
            data = {}
            for adr in (body.get("data") or {}).get("datatosend") or []:
                value, code = self._value_for(adr)
                data[adr] = {"data": value, "code": code}
            self._send({"cid": 1, "data": data, "code": 200})
            return
        self._send({"cid": 1, "code": 400}, status=400)


srv = HTTPServer(("127.0.0.1", 0), FakeMaster)
threading.Thread(target=srv.serve_forever, daemon=True).start()
host, port = srv.server_address

client = IfmMasterClient(host=host, port=port, timeout_s=5.0)

info = client.identify()
check("block identity read", info.get("product_code") == "AL1326", info)

ports = client.scan_ports(port_count=8)
check("scan reports all 8 ports", len(ports) == 8, len(ports))
connected = [p for p in ports if p["connected"]]
check("scan finds the connected sensors", sorted(p["port"] for p in connected) == [1, 3, 5],
      [p["port"] for p in connected])
check("scan suggests a profile for a known sensor",
      connected[0]["suggested_profile"] == "ifm-temperature-0.1c",
      connected[0]["suggested_profile"])
check("scan shows the live raw value", connected[0]["pdin"] == "03C9", connected[0]["pdin"])

# one request per cycle regardless of port count -- the reason getdatamulti exists
STATE["multi_calls"] = 0
fields = [
    IfmField("Temp1", 1, 2, 14, "int", 0.1, unit="degC"),
    IfmField("Temp3", 3, 2, 14, "int", 0.1, unit="degC"),
    IfmField("Raw5", 5, 0, 16, "uint", 1.0),
]
rows = client.read_fields(fields)
check("read_fields returns one row per field", len(rows) == 3, len(rows))
by_name = {r["name"]: r for r in rows}
check("Temp1 decodes to 24.2", abs(by_name["Temp1"]["value"] - 24.2) < 1e-9, by_name["Temp1"])
check("Temp3 decodes to 30.8", abs(by_name["Temp3"]["value"] - 30.8) < 1e-9, by_name["Temp3"])
check("Raw5 decodes to 255", by_name["Raw5"]["value"] == 255.0, by_name["Raw5"])
check("all three ports cost ONE request", STATE["multi_calls"] == 1, STATE["multi_calls"])
check("good readings carry GOOD quality", all(r["quality"] == 192 for r in rows))

# an unplugged port must not poison the rest of the block
rows = client.read_fields(fields + [IfmField("Ghost", 7, 0, 16, "uint")])
ghost = next(r for r in rows if r["name"] == "Ghost")
check("an empty port reads BAD, with a reason", ghost["quality"] == 0 and bool(ghost["error"]),
      ghost.get("error"))
check("  and the healthy ports still read GOOD",
      all(r["quality"] == 192 for r in rows if r["name"] != "Ghost"))

# a mapping that does not fit is reported per field, not as a dead cycle
rows = client.read_fields([IfmField("TooWide", 1, 0, 32, "uint")])
check("a mapping that overruns the data reports itself",
      rows[0]["quality"] == 0 and "only 16 bits" in rows[0]["error"], rows[0]["error"])

# firmware without getdatamulti still works
STATE["multi_enabled"] = False
client2 = IfmMasterClient(host=host, port=port, timeout_s=5.0)
STATE["get_calls"] = 0
rows = client2.read_fields(fields)
check("falls back to per-port getdata", len(rows) == 3 and all(r["quality"] == 192 for r in rows),
      [r.get("error") for r in rows])
check("  and the fallback is remembered (no retry storm)", client2._multi_supported is False)
before = STATE["get_calls"]
client2.read_fields(fields)
check("  second cycle costs 3 GETs, not 3 + a failed multi",
      STATE["get_calls"] - before == 3, STATE["get_calls"] - before)

# an unreachable master must fail fast, not hang the collection cycle
dead = IfmMasterClient(host="127.0.0.1", port=9, timeout_s=1.0)
rows = dead.read_fields([IfmField("X", 1, 0, 16, "uint")])
check("an unreachable master returns BAD quality quickly",
      len(rows) == 1 and rows[0]["quality"] == 0, rows)

srv.shutdown()
print()
print(f"RESULT: {'PASS' if not FAILS else 'FAIL - ' + ', '.join(FAILS)}")
sys.exit(0 if not FAILS else 2)
