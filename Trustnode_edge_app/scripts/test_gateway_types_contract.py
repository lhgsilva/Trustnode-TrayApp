# -*- coding: utf-8 -*-
"""Every gateway type must be accepted by EVERY gateway endpoint.

Two real defects motivated this (2026-08-25):

  * Several request models repeated the gateway-type union by hand, so adding a
    device type made Test Connection and Network Discovery answer 422 while the
    gateway itself worked. The operator saw "[object Object]".
  * Saving a gateway kept its protocol fields, but REOPENING it did not restore
    them, so the next save wrote them back empty — the ticked tags "did not
    stay".

This asserts the contract rather than any one device: whenever a new gateway
type is added to models.GatewayType, it must pass here without edits.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = r"D:\Trustnode\Trustnode-AB\Tray_app\Trustnode_edge_app"
sys.path.insert(0, os.path.join(ROOT, "backend"))
PORT = "8095"
API = f"http://127.0.0.1:{PORT}"
FAILS = []


def check(name, ok, detail=""):
    print(f"  {name:60s}: {'PASS' if ok else 'FAIL'}{(' - ' + str(detail)[:110]) if detail else ''}")
    if not ok:
        FAILS.append(name)


# the single source of truth for what a gateway type can be
from app.models import GatewayConfig  # noqa: E402
import typing  # noqa: E402

GATEWAY_TYPES = list(typing.get_args(GatewayConfig.model_fields["gateway_type"].annotation))
print(f"  gateway types declared: {GATEWAY_TYPES}")

tmp = tempfile.mkdtemp(prefix="tn-gwtypes-")
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


def call(method, path, token=None, body=None, timeout=60):
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
    proc.kill(); sys.exit(2)

# --- 1. no endpoint may 422 on a declared gateway type --------------------
print("\n[every type is accepted by every endpoint]")
for gt in GATEWAY_TYPES:
    st, r = call("POST", "/api/plc/test-connection", admin,
                 {"gateway_type": gt, "plc_ip": "127.0.0.1", "timeout_ms": 300})
    check(f"test-connection accepts '{gt}'", st != 422, f"status={st} {str(r)[:70]}")

    st, r = call("POST", "/api/plc/discover-network", admin,
                 {"gateway_type": gt, "scan_range": "127.0.0.1", "timeout_ms": 300,
                  "include_tcp_probe": False})
    check(f"discover-network accepts '{gt}'", st != 422, f"status={st} {str(r)[:70]}")

    st, r = call("POST", "/api/plc/discover-tags", admin,
                 {"gateway_type": gt, "plc_ip": "127.0.0.1", "timeout_ms": 300})
    check(f"discover-tags accepts '{gt}'", st != 422, f"status={st} {str(r)[:70]}")

# --- 2. the connectivity test must know which port to probe ---------------
print("\n[the test names a real port, never 'n/a']")
EXPECTED_PORT = {"allen_bradley": 44818, "siemens_snap7": 102, "siemens_opcua": 4840,
                 "boston": 502, "ifm_iolink": 80, "ethernet_ip": 44818}
for gt, want in EXPECTED_PORT.items():
    if gt not in GATEWAY_TYPES:
        continue
    st, r = call("POST", "/api/plc/test-connection", admin,
                 {"gateway_type": gt, "plc_ip": "127.0.0.1", "timeout_ms": 300})
    got = (r or {}).get("port") if isinstance(r, dict) else None
    check(f"'{gt}' probes port {want}", got == want, f"got {got}")

st, r = call("POST", "/api/plc/test-connection", admin,
             {"gateway_type": "ifm_iolink", "plc_ip": "127.0.0.1",
              "ifm_http_port": 8080, "timeout_ms": 300})
check("an ifm block on a custom IoT port is probed there",
      (r or {}).get("port") == 8080, (r or {}).get("port"))

# --- 3. a 422 must read as a sentence, not [object Object] ----------------
st, r = call("POST", "/api/plc/test-connection", admin,
             {"gateway_type": "definitely-not-a-type", "plc_ip": "127.0.0.1"})
body = r.decode() if isinstance(r, bytes) else json.dumps(r)
check("an unknown type still 422s (the union is enforced)", st == 422, f"status={st}")
check("  and the body names the offending field",
      "gateway_type" in body, body[:90])

# --- 4. every protocol field survives a save/reload round trip ------------
print("\n[a saved gateway keeps its protocol fields]")
GW = {
    "id": "gw-roundtrip", "name": "IFM", "gateway_type": "ifm_iolink",
    "plc_ip": "192.168.1.250", "interval_ms": 1000,
    "ifm_http_port": 80, "ifm_variant": "io_module",
    "ifm_datapoints": [
        {"name": "Port1_Pin2", "adr": "/io/port[1]/pin2/digital_input/getdata",
         "kind": "bool", "bit_length": 0, "scale": 1.0, "unit": "", "enabled": True},
        {"name": "Port1_Pin4", "adr": "/io/port[1]/pin4/digital_input/getdata",
         "kind": "bool", "bit_length": 0, "scale": 1.0, "unit": "", "enabled": True},
    ],
    "tags": ["Port1_Pin2", "Port1_Pin4"],
}
st, _ = call("PUT", "/api/app-store/domain", admin,
             {"domain": "gateway_configurations", "payload": [GW], "actor": "t"})
check("gateway saves", st == 200, f"status={st}")

st, bs = call("GET", "/api/app-store/bootstrap", admin)
stored = (((bs or {}).get("data") or {}).get("gateway_configurations") or [])
row = next((g for g in stored if g.get("id") == "gw-roundtrip"), {})
check("ifm_datapoints survive the round trip", len(row.get("ifm_datapoints") or []) == 2,
      len(row.get("ifm_datapoints") or []))
check("ifm_variant survives", row.get("ifm_variant") == "io_module", row.get("ifm_variant"))
check("tags survive", len(row.get("tags") or []) == 2, row.get("tags"))

# and the collector must receive them
st, r = call("POST", "/api/plc/gateways/start", admin,
             {"gateway_id": "gw-roundtrip",
              "config": {k: v for k, v in GW.items() if k != "id"}})
check("a gateway carrying datapoints starts", st == 200, f"status={st} {str(r)[:90]}")
call("POST", "/api/plc/gateways/stop", admin, {"gateway_id": "gw-roundtrip"})

proc.terminate()
try:
    proc.wait(timeout=20)
except Exception:
    proc.kill()
print()
print(f"RESULT: {'PASS' if not FAILS else 'FAIL - ' + ', '.join(FAILS)}")
sys.exit(0 if not FAILS else 2)
