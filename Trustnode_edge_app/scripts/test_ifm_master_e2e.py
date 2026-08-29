# -*- coding: utf-8 -*-
"""An IO-Link MASTER (AL13xx/AL14xx) must collect into the historian too.

The AL4022 test covers the I/O-module variant, where each pin is its own
address. A master is the harder shape: the sensor value is packed inside a
per-port `pdin` HEX string, and only an IODD profile says which bits mean what.
The operator reported "no values" on BOTH kinds, so both need pipeline cover -
a driver-level decode test would not have caught the write-path defect that
actually broke them (see test_historian_commit_path.py).

The temperature case is pinned to the ifm worked example: pdin 03C9, 14-bit
value at bit offset 2, 0.1 degC per count -> 24.2 degC. If the bit convention
is ever flipped back to MSB-first this reads 96.9 and fails here.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
PORT = "8097"
API = "http://127.0.0.1:" + PORT
FAILS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:100]) if detail else ""
    print("  {0:58s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


# --- the fake master -------------------------------------------------------
# port 1: ifm temperature sensor (vendor 310, device 446) -> profile match
# port 2: ifm vibration sensor  (vendor 310, device 416) -> multi-field profile
# port 3: nothing plugged in                             -> must be skipped
# port 4: an unknown device                              -> raw 16-bit fallback
PORTS = {
    1: {"name": "TA2105", "vendor": 310, "device": 446, "pdin": "03C9"},
    2: {"name": "VVB001", "vendor": 310, "device": 416, "pdin": "01F4008C05"},
    3: None,
    4: {"name": "MysterySensor", "vendor": 999, "device": 1, "pdin": "0064"},
}


def _tree():
    return {
        "identifier": "al1352",
        "type": "device",
        "subs": {
            "getdatamulti": {"type": "service"},
            "iolinkmaster": dict(
                ("port[{0}]".format(p), {"type": "structure"}) for p in PORTS
            ),
        },
    }


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
        if not adr.startswith("/iolinkmaster/port["):
            return None, 404
        try:
            p = int(adr.split("[")[1].split("]")[0])
        except Exception:
            return None, 404
        info = PORTS.get(p)
        if info is None:
            # an empty port answers, but with a "no device" code
            return None, 404
        if "productname" in adr:
            return info["name"], 200
        if "vendorid" in adr:
            return info["vendor"], 200
        if "deviceid" in adr:
            return info["device"], 200
        if "pdin" in adr:
            return info["pdin"], 200
        return None, 404

    def do_GET(self):
        value, code = self._value_for(self.path)
        self._send({"cid": -1, "data": {"value": value}, "adr": self.path, "code": code})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode() or "{}")
        adr = body.get("adr")
        if adr == "gettree":
            self._send({"cid": -1, "code": 200, "data": _tree()})
            return
        if adr == "/getdatamulti":
            data = {}
            for a in (body.get("data") or {}).get("datatosend") or []:
                # The REAL block addresses the NODE here: an address that
                # still ends in /getdata is not recognised and is simply
                # OMITTED from the reply (verified on an AL1326,
                # 2026-08-27). Mirror that, or this fake hides the bug.
                if a.endswith("/getdata"):
                    continue
                value, code = self._value_for(a + "/getdata")
                data[a] = {"data": value, "code": code}
            self._send({"cid": 1, "data": data, "code": 200})
            return
        self._send({"cid": 1, "code": 400}, status=400)


block = HTTPServer(("127.0.0.1", 0), FakeMaster)
threading.Thread(target=block.serve_forever, daemon=True).start()
HOST, HPORT = block.server_address
print("  fake IO-Link master on {0}:{1}".format(HOST, HPORT))

# --- driver level ----------------------------------------------------------
from app.drivers.ifm_iolink import (  # noqa: E402
    IfmMasterClient, VARIANT_IOLINK_MASTER, datapoints_from_config,
)

print("\n[DRIVER]")
cli = IfmMasterClient(host=HOST, port=HPORT, timeout_s=5.0)
check("a master is detected as a master",
      cli.detect_variant() == VARIANT_IOLINK_MASTER, cli.detect_variant())

ports = cli.scan_ports(port_count=8)
check("only the ports the block declares are probed", len(ports) == 4, len(ports))
connected = [p for p in ports if p.get("connected")]
check("the three plugged-in ports are seen", len(connected) == 3,
      [p["port"] for p in connected])
check("the empty port is reported as empty",
      not next(p for p in ports if p["port"] == 3)["connected"])
p1 = next(p for p in ports if p["port"] == 1)
check("a known sensor gets its IODD profile",
      p1.get("suggested_profile") == "ifm-temperature-0.1c", p1.get("suggested_profile"))
p4 = next(p for p in ports if p["port"] == 4)
check("an unknown sensor still offers a raw value",
      p4.get("connected") is True and not p4.get("suggested_profile"),
      p4.get("suggested_profile"))

disc = cli.discover_datapoints(variant=VARIANT_IOLINK_MASTER, port_count=8)
names = [d["name"] for d in disc.get("datapoints") or []]
check("discovery names the profile fields", any("Temperature" in n for n in names), names)
check("  including every field of a multi-value sensor",
      sum(1 for n in names
          if "Acceleration" in n or "Velocity" in n or "Diagnosis" in n) == 3, names)
check("  and a raw value for the unknown port", any("Port4" in n for n in names), names)
check("nothing is offered for the empty port", not any("Port3" in n for n in names), names)

# the saved config is plain dicts; the driver reads typed datapoints, which
# is exactly the conversion the gateway does each cycle
vals = cli.read_datapoints(datapoints_from_config(disc.get("datapoints") or []))
temp = next((v for v in vals if "Temperature" in v["name"]), {})
check("temperature decodes to the ifm worked example (24.2 degC)",
      abs(float(temp.get("value") or 0) - 24.2) < 0.01, temp.get("value"))
check("  with its unit", temp.get("unit") == "degC", temp.get("unit"))
check("all readings are GOOD", all(not v.get("error") for v in vals),
      [v for v in vals if v.get("error")][:2])

# --- end to end ------------------------------------------------------------
print("\n[END TO END through the real pipeline]")
tmp = tempfile.mkdtemp(prefix="tn-ifmmaster-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=os.path.join(ROOT, "backend"),
                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(60):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read()
        break
    except Exception:
        time.sleep(2)


def call(method, path, token=None, body=None, timeout=90):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:400]
    except Exception as e:
        return 0, str(e)[:300]


def finish(code):
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    block.shutdown()
    sys.exit(code)


st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
admin = (b or {}).get("token") if isinstance(b, dict) else None
check("admin login", st == 200 and bool(admin))
if not admin:
    finish(2)

st, scan = call("POST", "/api/plc/ifm/scan-ports", admin,
                {"plc_ip": HOST, "http_port": HPORT, "variant": "auto"})
check("the dialog scan detects a master",
      st == 200 and (scan or {}).get("variant") == "iolink_master",
      (scan or {}).get("variant"))
scanned = (scan or {}).get("datapoints") or []
check("scan returns tickable datapoints", len(scanned) >= 5, len(scanned))

chosen = [d for d in scanned if "Temperature" in d["name"] or "Velocity" in d["name"]]
check("the temperature and velocity fields are offerable", len(chosen) == 2,
      [d["name"] for d in chosen])

st, r = call("POST", "/api/plc/gateways/start", admin, {
    "gateway_id": "gw-ifm-master",
    "config": {"gateway_type": "ifm_iolink", "name": "IO-Link Master",
               "device_name": "AL1352", "plc_ip": HOST,
               "ifm_http_port": HPORT, "ifm_variant": "iolink_master",
               "ifm_datapoints": chosen,
               "tags": [d["name"] for d in chosen],
               "interval_ms": 1000, "site": "Limerick", "area": "LineA",
               "equipment": "Master"}})
check("a master gateway starts", st == 200, "status={0} {1}".format(st, str(r)[:140]))

time.sleep(6)
st, hist = call("GET", "/api/app-store/historian?limit=100", admin)
rows = [x for x in (((hist or {}).get("rows")) or [])
        if str(x.get("gateway_id")) == "gw-ifm-master"]
check("readings reach the historian", len(rows) > 0, len(rows))
got = set(str(x.get("tag")) for x in rows)
check("both chosen fields are collected", len(got) == 2, sorted(got))
one = next((x for x in rows if "Temperature" in str(x.get("tag"))), {})
check("the decoded temperature is stored (24.2)",
      abs(float(one.get("value") or 0) - 24.2) < 0.01, one.get("value"))
check("quality GOOD", str(one.get("quality_label")) == "GOOD", one.get("quality_label"))

time.sleep(3)
st, hist2 = call("GET", "/api/app-store/historian?limit=300", admin)
again = [x for x in ((hist2 or {}).get("rows") or [])
         if "Temperature" in str(x.get("tag"))]
check("it keeps sampling on the interval (trendable)", len(again) >= 2, len(again))

# a master configured with only tag NAMES must resolve them, as the dialog saves
st, r = call("POST", "/api/plc/gateways/start", admin, {
    "gateway_id": "gw-ifm-master-names",
    "config": {"gateway_type": "ifm_iolink", "name": "Master by name",
               "device_name": "AL1352", "plc_ip": HOST, "ifm_http_port": HPORT,
               "tags": ["Port1_Temperature"], "interval_ms": 1000,
               "site": "Limerick", "area": "LineA", "equipment": "Master"}})
check("a master with only tag NAMES starts", st == 200,
      "status={0} {1}".format(st, str(r)[:120]))
time.sleep(6)
st, hist3 = call("GET", "/api/app-store/historian?limit=300", admin)
nrows = [x for x in ((hist3 or {}).get("rows") or [])
         if str(x.get("gateway_id")) == "gw-ifm-master-names"]
check("  it resolves the name and COLLECTS", len(nrows) > 0, len(nrows))

call("POST", "/api/plc/gateways/stop", admin, {"gateway_id": "gw-ifm-master"})
call("POST", "/api/plc/gateways/stop", admin, {"gateway_id": "gw-ifm-master-names"})

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
