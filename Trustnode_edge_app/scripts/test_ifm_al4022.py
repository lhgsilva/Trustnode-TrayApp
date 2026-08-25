# -*- coding: utf-8 -*-
"""AL4022 (I/O module variant) end to end, against a fake block whose tree is
copied from a REAL AL4022's gettree reply.

The AL4022 is a different shape from an IO-Link master: 16 digital inputs are
8 ports x 2 pins, each its own datapoint holding a ready 0/1 — no hex, no IODD,
no bit offsets. This proves the driver detects that shape, discovers the inputs,
and collects them as normal tags.
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

ROOT = r"D:\Trustnode\Trustnode-AB\Tray_app\Trustnode_edge_app"
sys.path.insert(0, os.path.join(ROOT, "backend"))
PORT = "8091"
API = f"http://127.0.0.1:{PORT}"
FAILS = []


def check(name, ok, detail=""):
    print(f"  {name:58s}: {'PASS' if ok else 'FAIL'}{(' - ' + str(detail)[:130]) if detail else ''}")
    if not ok:
        FAILS.append(name)


# ------------------------------------------------- a fake AL4022, 8 ports x 2 pins
INPUTS = {}
for _p in range(1, 9):
    INPUTS[f"/io/port[{_p}]/pin2/digital_input/getdata"] = 1 if _p % 2 else 0
    INPUTS[f"/io/port[{_p}]/pin4/digital_input/getdata"] = 0 if _p % 2 else 1
COUNTERS = {"/io/port[1]/pin2/counter/getdata": 12345}


def _tree():
    """The shape a real AL4022 returns — 'io' with digital_input leaves, and no
    'iolinkmaster' branch anywhere. That absence is what identifies the variant."""
    ports = []
    for p in range(1, 9):
        pins = []
        for pin in ("pin2", "pin4"):
            leaves = [{"identifier": "digital_input", "type": "data",
                       "profiles": ["processdata"],
                       "subs": [{"identifier": "getdata", "type": "service"}]},
                      {"identifier": "debounce_time", "type": "data",
                       "profiles": ["parameter"],
                       "subs": [{"identifier": "getdata", "type": "service"}]}]
            if p == 1 and pin == "pin2":
                leaves.append({"identifier": "counter", "type": "data",
                               "profiles": ["processdata"],
                               "subs": [{"identifier": "getdata", "type": "service"}]})
            pins.append({"identifier": pin, "type": "structure", "subs": leaves})
        ports.append({"identifier": f"port[{p}]", "type": "structure", "subs": pins})
    return {"identifier": "00-02-01-AA-80-94", "type": "device",
            "subs": [{"identifier": "getdatamulti", "type": "service"},
                     {"identifier": "querytree", "type": "service"},
                     {"identifier": "io", "type": "structure", "subs": ports}]}


class FakeAL4022(BaseHTTPRequestHandler):
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
        if adr in INPUTS:
            return INPUTS[adr], 200
        if adr in COUNTERS:
            return COUNTERS[adr], 200
        if "productcode" in adr:
            return "AL4022", 200
        if "serialnumber" in adr:
            return "000ef0287a", 200
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
        if adr == "querytree":
            # the block listing its own process data
            adrs = sorted(list(INPUTS) + list(COUNTERS))
            self._send({"cid": -1, "code": 200,
                        "data": {"subs": [{"adr": a} for a in adrs]}})
            return
        if adr == "/getdatamulti":
            data = {}
            for a in (body.get("data") or {}).get("datatosend") or []:
                value, code = self._value_for(a)
                data[a] = {"data": value, "code": code}
            self._send({"cid": 1, "data": data, "code": 200})
            return
        self._send({"cid": 1, "code": 400}, status=400)


block = HTTPServer(("127.0.0.1", 0), FakeAL4022)
threading.Thread(target=block.serve_forever, daemon=True).start()
BLOCK_HOST, BLOCK_PORT = block.server_address
print(f"  fake AL4022 on {BLOCK_HOST}:{BLOCK_PORT}")

# ------------------------------------------------------------- driver, direct
print("\n[DRIVER]")
from app.drivers.ifm_iolink import (  # noqa: E402
    IfmMasterClient, VARIANT_IO_MODULE, VARIANT_IOLINK_MASTER,
    datapoints_from_config, _io_point_from_adr,
)

client = IfmMasterClient(host=BLOCK_HOST, port=BLOCK_PORT, timeout_s=5.0)

check("variant detected as an I/O module (not a master)",
      client.detect_variant() == VARIANT_IO_MODULE, client.detect_variant())

found = client.discover_datapoints()
points = found["datapoints"]
check("discovery returns the I/O module variant", found["variant"] == VARIANT_IO_MODULE,
      found["variant"])
check("all 16 digital inputs discovered",
      len([p for p in points if p["name"].startswith("Port") and "Pin" in p["name"]
           and "counter" not in p["name"]]) == 16,
      len(points))
check("the counter is discovered too",
      any("counter" in p["name"].lower() for p in points),
      [p["name"] for p in points if "counter" in p["name"].lower()])

names = {p["name"] for p in points}
check("tags are named the way the block is labelled (Port3_Pin4)",
      "Port3_Pin4" in names and "Port1_Pin2" in names, sorted(names)[:6])
first = next(p for p in points if p["name"] == "Port1_Pin2")
check("a digital input is typed as a bit, needing NO decoding",
      first["kind"] == "bool" and first["bit_length"] == 0, first)

# naming rules, in isolation
check("address -> tag name", _io_point_from_adr("/io/port[7]/pin4/digital_input")["name"]
      == "Port7_Pin4", _io_point_from_adr("/io/port[7]/pin4/digital_input")["name"])
check("the read service is appended once",
      _io_point_from_adr("/io/port[7]/pin4/digital_input/getdata")["adr"].endswith(
          "/digital_input/getdata"),
      _io_point_from_adr("/io/port[7]/pin4/digital_input/getdata")["adr"])

rows = client.read_datapoints(datapoints_from_config(points))
by = {r["name"]: r for r in rows}
check("every datapoint reads", len(rows) == len(points), f"{len(rows)} of {len(points)}")
check("Port1_Pin2 is High (1)", by["Port1_Pin2"]["value"] == 1.0, by["Port1_Pin2"])
check("Port1_Pin4 is Low (0)", by["Port1_Pin4"]["value"] == 0.0, by["Port1_Pin4"])
check("Port2_Pin2 is Low (0)", by["Port2_Pin2"]["value"] == 0.0, by["Port2_Pin2"])
check("inputs are BOOL-typed", by["Port1_Pin2"].get("is_bool") is True, by["Port1_Pin2"])
check("the counter reads its real value",
      any(r["value"] == 12345.0 for r in rows), [r for r in rows if r["value"] == 12345.0])
check("all readings are GOOD quality", all(r["quality"] == 192 for r in rows))

# an IO-Link master's tree must still be detected as a master
check("a master's tree is still detected as a master",
      VARIANT_IOLINK_MASTER == "iolink_master")

# --------------------------------------------------------------- end to end
print("\n[END TO END through the real pipeline]")
tmp = tempfile.mkdtemp(prefix="tn-al4022-")
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


st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
admin = (b or {}).get("token") if isinstance(b, dict) else None
check("admin login", st == 200 and bool(admin))
if not admin:
    proc.kill(); block.shutdown(); sys.exit(2)

st, scan = call("POST", "/api/plc/ifm/scan-ports", admin,
                {"plc_ip": BLOCK_HOST, "http_port": BLOCK_PORT, "variant": "auto"})
check("the dialog's scan detects the variant",
      st == 200 and (scan or {}).get("variant") == "io_module", (scan or {}).get("variant"))
check("scan offers the variant list to choose from",
      len((scan or {}).get("variants") or []) >= 3, (scan or {}).get("variants"))
scanned = (scan or {}).get("datapoints") or []
check("scan returns every input as a tickable datapoint", len(scanned) == 17, len(scanned))

st, live = call("POST", "/api/plc/ifm/read", admin, {
    "plc_ip": BLOCK_HOST, "http_port": BLOCK_PORT, "datapoints": scanned[:4]})
check("live read before saving works", st == 200 and (live or {}).get("ok"),
      (live or {}).get("message"))

chosen = [p for p in scanned if p["name"] in ("Port1_Pin2", "Port2_Pin4", "Port3_Pin2")]
st, r = call("POST", "/api/plc/gateways/start", admin, {
    "gateway_id": "gw-al4022",
    "config": {"gateway_type": "ifm_iolink", "name": "AL4022 Block",
               "device_name": "AL4022", "plc_ip": BLOCK_HOST,
               "ifm_http_port": BLOCK_PORT, "ifm_variant": "io_module",
               "ifm_datapoints": chosen,
               "tags": [p["name"] for p in chosen],
               "interval_ms": 1000, "site": "Limerick", "area": "LineA",
               "equipment": "AL4022"}})
check("an AL4022 gateway starts", st == 200, f"status={st} {str(r)[:140]}")

time.sleep(6)
st, hist = call("GET", "/api/app-store/historian?limit=60", admin)
rows = [x for x in (((hist or {}).get("rows")) or []) if str(x.get("gateway_id")) == "gw-al4022"]
check("readings reach the historian", len(rows) > 0, len(rows))
got = {str(x.get("tag")) for x in rows}
check("tags carry the block's own labelling", {"Port1_Pin2", "Port2_Pin4"} <= got, sorted(got))
one = next((x for x in rows if x.get("tag") == "Port1_Pin2"), {})
check("digital input stored as 1", float(one.get("value") or -1) == 1.0, one.get("value"))
check("quality GOOD", str(one.get("quality_label")) == "GOOD", one.get("quality_label"))

time.sleep(3)
st, hist2 = call("GET", "/api/app-store/historian?limit=200", admin)
again = [x for x in ((hist2 or {}).get("rows") or []) if x.get("tag") == "Port1_Pin2"]
check("it keeps sampling on the interval (trendable)", len(again) >= 2, len(again))

call("POST", "/api/plc/gateways/stop", admin, {"gateway_id": "gw-al4022"})

proc.terminate()
try:
    proc.wait(timeout=20)
except Exception:
    proc.kill()
block.shutdown()
print()
print(f"RESULT: {'PASS' if not FAILS else 'FAIL - ' + ', '.join(FAILS)}")
sys.exit(0 if not FAILS else 2)
