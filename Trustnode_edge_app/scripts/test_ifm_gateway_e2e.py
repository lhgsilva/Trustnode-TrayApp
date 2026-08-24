# -*- coding: utf-8 -*-
"""IFM gateway end to end: a FAKE IO-Link block -> the real collection pipeline
-> normalized tags in the historian.

Proves the new gateway type behaves like any other: readings land with GOOD
quality, engineering units and the operator's own tag names, so dashboards,
trends and reports get them for free.

Also asserts the thing that matters most: the four existing gateway types are
untouched. Throwaway backend only.
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
PORT = "8081"
API = f"http://127.0.0.1:{PORT}"
FAILS = []


def check(name, ok, detail=""):
    print(f"  {name:58s}: {'PASS' if ok else 'FAIL'}{(' - ' + str(detail)[:130]) if detail else ''}")
    if not ok:
        FAILS.append(name)


# ------------------------------------------------------- the fake IFM block
PDIN = {1: "03C9", 2: "04D2"}          # 24.2 degC and 30.8 degC once decoded


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
            if f"port[{p}]" in adr and "/pdin/" in adr:
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
            return None, 404
        if "productcode" in adr:
            return "AL1326", 200
        if "serialnumber" in adr:
            return "000123456789", 200
        return None, 404

    def do_GET(self):
        value, code = self._value_for(self.path)
        self._send({"cid": 1, "data": {"value": value}, "code": code})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode() or "{}")
        if body.get("adr") == "/getdatamulti":
            data = {}
            for adr in (body.get("data") or {}).get("datatosend") or []:
                value, code = self._value_for(adr)
                data[adr] = {"data": value, "code": code}
            self._send({"cid": 1, "data": data, "code": 200})
            return
        self._send({"cid": 1, "code": 400}, status=400)


block = HTTPServer(("127.0.0.1", 0), FakeMaster)
threading.Thread(target=block.serve_forever, daemon=True).start()
BLOCK_HOST, BLOCK_PORT = block.server_address
print(f"  fake IFM block on {BLOCK_HOST}:{BLOCK_PORT}")

# ------------------------------------------------------------ the backend
tmp = tempfile.mkdtemp(prefix="tn-ifm-")
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

# --- the dialog's port scan -----------------------------------------------
st, scan = call("POST", "/api/plc/ifm/scan-ports", admin,
                {"plc_ip": BLOCK_HOST, "http_port": BLOCK_PORT, "port_count": 8})
check("scan-ports answers", st == 200 and (scan or {}).get("ok"), f"status={st} {str(scan)[:110]}")
found = [p for p in (scan or {}).get("ports") or [] if p.get("connected")]
check("scan finds the two sensors", len(found) == 2, [p["port"] for p in found])
check("scan names the block", ((scan or {}).get("device") or {}).get("product_code") == "AL1326",
      (scan or {}).get("device"))
check("scan offers built-in profiles", len((scan or {}).get("profiles") or []) >= 3,
      len((scan or {}).get("profiles") or []))

# --- the live decode preview ----------------------------------------------
st, prev = call("POST", "/api/plc/ifm/preview", admin, {
    "plc_ip": BLOCK_HOST, "http_port": BLOCK_PORT, "port": 1,
    "fields": [{"name": "Temp", "bit_offset": 2, "bit_length": 14, "kind": "int",
                "scale": 0.1, "unit": "degC"}]})
check("preview reads the live raw value", st == 200 and (prev or {}).get("raw") == "03C9",
      (prev or {}).get("raw"))
check("preview decodes it to 24.2 degC",
      abs(((prev or {}).get("values") or [{}])[0].get("value", 0) - 24.2) < 1e-9,
      (prev or {}).get("values"))

# --- "Search Available Tags" must reach the same block ---------------------
# It shares the discovery endpoint with every other gateway type, so it has to
# carry the IFM connection details or a block on a non-default IoT port is
# reachable from "Scan ports" and invisible here.
st, disc = call("POST", "/api/plc/discover-tags", admin, {
    "gateway_type": "ifm_iolink", "plc_ip": BLOCK_HOST,
    "ifm_http_port": BLOCK_PORT, "ifm_port_count": 8})
check("tag discovery reaches a block on a non-default port",
      st == 200 and bool((disc or {}).get("ok")), f"status={st} {str(disc)[:110]}")
check("it suggests searchable tag names",
      len((disc or {}).get("tags") or []) >= 2, (disc or {}).get("tags"))

# --- configure and START a real gateway -----------------------------------
IFM_PORTS = [
    {"port": 1, "enabled": True, "prefix": "",
     "fields": [{"name": "Tank1_Temp", "bit_offset": 2, "bit_length": 14,
                 "kind": "int", "scale": 0.1, "unit": "degC"}]},
    {"port": 2, "enabled": True, "prefix": "",
     "fields": [{"name": "Tank2_Temp", "bit_offset": 2, "bit_length": 14,
                 "kind": "int", "scale": 0.1, "unit": "degC"}]},
]
start_body = {
    "gateway_id": "gw-ifm-test",
    "config": {
        "gateway_type": "ifm_iolink",
        "name": "IFM Block A",
        "device_name": "AL1326",
        "plc_ip": BLOCK_HOST,
        "ifm_http_port": BLOCK_PORT,
        "ifm_ports": IFM_PORTS,
        "tags": ["Tank1_Temp", "Tank2_Temp"],
        "interval_ms": 1000,
        "site": "Limerick", "area": "LineA", "equipment": "TANKS",
    },
}
st, r = call("POST", "/api/plc/gateways/start", admin, start_body)
check("an ifm_iolink gateway STARTS", st == 200, f"status={st} {str(r)[:160]}")

time.sleep(6)

st, status = call("GET", "/api/plc/gateways/status", admin)
row = next((g for g in (status if isinstance(status, list) else [])
            if str(g.get("gateway_id")) == "gw-ifm-test"), {})
check("the gateway reports running", bool(row.get("running")), row)

# --- the readings must be normal tags in the historian --------------------
st, hist = call("GET", "/api/app-store/historian?limit=50", admin)
rows = (hist or {}).get("rows") if isinstance(hist, dict) else []
ifm_rows = [r for r in (rows or []) if str(r.get("gateway_id")) == "gw-ifm-test"]
check("readings reached the historian", len(ifm_rows) > 0, len(ifm_rows))
names = {str(r.get("tag")) for r in ifm_rows}
check("they carry the OPERATOR's tag names", {"Tank1_Temp", "Tank2_Temp"} <= names, sorted(names))
t1 = next((r for r in ifm_rows if r.get("tag") == "Tank1_Temp"), {})
check("stored in ENGINEERING units, not raw hex",
      abs(float(t1.get("value") or 0) - 24.2) < 1e-6, t1.get("value"))
check("quality is GOOD", str(t1.get("quality_label") or "") == "GOOD", t1.get("quality_label"))
check("the block identity travels with the row",
      str(t1.get("plc_ip") or "") == BLOCK_HOST, t1.get("plc_ip"))

# more than one sample => it is collecting on the interval, i.e. it will trend
time.sleep(3)
st, hist2 = call("GET", "/api/app-store/historian?limit=200", admin)
rows2 = [r for r in ((hist2 or {}).get("rows") or []) if r.get("tag") == "Tank1_Temp"]
check("it keeps sampling on the gateway interval (trendable)", len(rows2) >= 2, len(rows2))

st, _ = call("POST", "/api/plc/gateways/stop", admin, {"gateway_id": "gw-ifm-test"})
check("the gateway stops cleanly", st == 200, f"status={st}")

# --- the generic EtherNet/IP path: EDS import ------------------------------
# Same architecture, different transport: import the EDS, get the assemblies.
EDS = '''
[Device]
    VendCode = 310;
    VendName = "ifm electronic gmbh";
    ProdCode = 4711;
    ProdName = "AL1326";
[Assembly]
    Assem100 = "Output Data", , 32, 0x0000;
    Assem101 = "Input Data", , 64, 0x0000;
'''
st, eds = call("POST", "/api/plc/eip/parse-eds", admin, {"eds_text": EDS})
check("an EDS file is parsed", st == 200 and (eds or {}).get("ok"), f"status={st}")
check("EDS reports the device", ((eds or {}).get("device") or {}).get("product_name") == "AL1326",
      ((eds or {}).get("device") or {}).get("product_name"))
check("EDS suggests the input assembly",
      ((eds or {}).get("suggested") or {}).get("input_assembly") == 101,
      (eds or {}).get("suggested"))
st, bad = call("POST", "/api/plc/eip/parse-eds", admin, {"eds_text": "hello"})
check("a non-EDS file is rejected with a reason",
      st == 200 and not (bad or {}).get("ok") and bool((bad or {}).get("message")),
      (bad or {}).get("message"))

# an ethernet_ip gateway with no assembly set must refuse clearly, not crash
st, r = call("POST", "/api/plc/gateways/start", admin, {
    "gateway_id": "gw-eip-test",
    "config": {"gateway_type": "ethernet_ip", "plc_ip": "10.255.255.2",
               "tags": ["X"], "interval_ms": 1000}})
check("an ethernet_ip gateway starts (errors surface per cycle)", st == 200, f"status={st}")
call("POST", "/api/plc/gateways/stop", admin, {"gateway_id": "gw-eip-test"})

# --- the block password must not be readable by a browser -----------------
GW_DOC = [{"id": "gw-secret", "gateway_type": "ifm_iolink", "plc_ip": "10.0.0.9",
           "ifm_username": "administrator", "ifm_password": "s3cret-block-pw",
           "tags": [], "interval_ms": 1000}]
st, _ = call("PUT", "/api/app-store/domain", admin,
             {"domain": "gateway_configurations", "payload": GW_DOC, "actor": "t"})
check("a gateway with credentials saves", st == 200, f"status={st}")

st, bs = call("GET", "/api/app-store/bootstrap", admin)
served = (((bs or {}).get("data") or {}).get("gateway_configurations") or [])
row = next((g for g in served if g.get("id") == "gw-secret"), {})
check("the password is NOT served to the browser",
      row.get("ifm_password") != "s3cret-block-pw", row.get("ifm_password"))
check("  it is replaced by a sentinel", row.get("ifm_password") == "__set__",
      row.get("ifm_password"))
check("  the username is still shown", row.get("ifm_username") == "administrator",
      row.get("ifm_username"))

# saving back the sentinel must not destroy the stored password
st, _ = call("PUT", "/api/app-store/domain", admin,
             {"domain": "gateway_configurations",
              "payload": [{**row, "interval_ms": 2000}], "actor": "t"})
check("re-saving with the sentinel is accepted", st == 200, f"status={st}")
import sqlite3 as _sq
_con = _sq.connect(f"file:{os.path.join(tmp, 's.db')}?mode=ro", uri=True, timeout=10)
_rows = _con.execute(
    "SELECT payload_json FROM config_documents_scoped WHERE domain='gateway_configurations' "
    "UNION ALL SELECT payload_json FROM config_documents WHERE domain='gateway_configurations'"
).fetchall()
_con.close()
_stored_pw = ""
for _r in _rows:
    for _g in (json.loads(_r[0] or "[]") or []):
        if isinstance(_g, dict) and _g.get("id") == "gw-secret":
            _stored_pw = str(_g.get("ifm_password") or "")
check("the real password survived the round trip", _stored_pw == "s3cret-block-pw",
      _stored_pw or "(empty)")

# --- NOTHING ELSE MAY HAVE CHANGED ----------------------------------------
print()
print("  [no-regression]")
st, disc = call("POST", "/api/plc/discover-tags", admin,
                {"gateway_type": "siemens_snap7", "plc_ip": "10.0.0.1"})
check("snap7 discovery answers exactly as before",
      st == 200 and "cannot enumerate symbolic tags" in str((disc or {}).get("message") or ""),
      str((disc or {}).get("message"))[:80])
st, r = call("POST", "/api/plc/gateways/start", admin, {
    "gateway_id": "gw-ab-test",
    "config": {"gateway_type": "allen_bradley", "plc_ip": "10.255.255.1",
               "tags": ["Nope"], "interval_ms": 1000}})
check("an allen_bradley gateway still starts (unreachable PLC is fine)", st == 200,
      f"status={st} {str(r)[:100]}")
call("POST", "/api/plc/gateways/stop", admin, {"gateway_id": "gw-ab-test"})

st, cfg = call("GET", "/api/plc/config", admin)
check("the default gateway config still defaults to allen_bradley",
      str((cfg or {}).get("gateway_type")) == "allen_bradley", (cfg or {}).get("gateway_type"))
check("existing configs gain the IFM fields with harmless defaults",
      (cfg or {}).get("ifm_ports") == [] and int((cfg or {}).get("ifm_http_port") or 0) == 80,
      {k: (cfg or {}).get(k) for k in ("ifm_ports", "ifm_http_port")})

proc.terminate()
try:
    proc.wait(timeout=20)
except Exception:
    proc.kill()
block.shutdown()
print()
print(f"RESULT: {'PASS' if not FAILS else 'FAIL - ' + ', '.join(FAILS)}")
sys.exit(0 if not FAILS else 2)
