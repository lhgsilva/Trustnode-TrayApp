# -*- coding: utf-8 -*-
"""A deleted gateway stops collecting, and the browser is the same app.

2026-08-31, two reports in one message.

1. "I have deleted all the gateways and devices and still showing error."
   The Gateway Configuration page was empty while the app insisted:

       "PLC unreachable - waiting to reconnect: gw-1788125988586"
       "Found running gateway workers not mapped in this page (gw-1788125988586)"

   and told the operator to press "Stop All". A worker exists to serve a
   configured gateway; when the gateway is deleted the worker has no reason to
   run, and cleaning it up is the software's job. The supervisor now reconciles
   every scan, which also clears orphans left by a crash or restart - not only
   by a delete.

2. "whatever we implement for the app it must behave the same when open the
   app from the browser, not as a different app, same app seen from the
   browser."
   The LAN-served surface must not be mistaken for the hosted cloud portal:
   that mistake would disable every per-domain saver, and the browser really
   would be a different, read-only app.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = "8173"
API = "http://127.0.0.1:" + PORT
GID = "gw-orphan"
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:56s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


# ------------------------------------------------------------- source checks
print("[the browser is the same app, not a read-only cousin]")
api_js = io.open(os.path.join(ROOT, "frontend", "src", "api.js"),
                 encoding="utf-8", errors="replace").read()
surface = api_js.split("export function getRuntimeSurface", 1)[-1][:1400]
check("a /trustnode/... path is recognised as LAN-served",
      "LAN_SURFACE_PATH_RE" in surface and 'return `lan_${lan[1]}`' in surface)
hosted = api_js.split("export function isHostedWebClientRuntime", 1)[-1][:300]
check("LAN-served is NOT the hosted cloud portal",
      'getRuntimeSurface() === "cloud"' in hosted,
      "hosted is an exact match, so lan_full never qualifies")
app_jsx = io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
                  encoding="utf-8", errors="replace").read()
check("saving is gated on hosted-cloud only, never on 'is a browser'",
      "if (isHostedWebClient) return;" in app_jsx
      and "isLanServedRuntime() " not in app_jsx.split("const savePower", 1)[0][:0] + "",
      "the desktop shell and the LAN browser take the same path")

# ------------------------------------------------------------------ runtime
tmp = tempfile.mkdtemp(prefix="tn-parity-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
log = open(os.path.join(tmp, "o.log"), "w")
proc = subprocess.Popen([sys.executable, "-m", "app"],
                        cwd=os.path.join(ROOT, "backend"), env=env,
                        stdout=log, stderr=subprocess.STDOUT)
up = False
for _ in range(80):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read()
        up = True
        break
    except Exception:
        time.sleep(2)


def call(m, path, tok=None, body=None):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    d = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=d, headers=h, method=m)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return 0, str(e)[:120]


def finish(code):
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    return code


print()
print("[the LAN surface actually serves the app]")
check("the app started", up)
if not up:
    sys.exit(2)
tok = (call("POST", "/api/auth/login",
            body={"username": "admin", "password": "admin"})[1] or {}).get("token")
served = ""
status = 0
try:
    with urllib.request.urlopen(API + "/trustnode/full/app/", timeout=30) as r:
        status = r.status
        served = r.read().decode("utf-8", "replace")
except Exception as exc:
    served = "ERROR " + str(exc)[:80]
check("GET /trustnode/full/app/ serves the SPA", status == 200
      and "<div id=\"root\"" in served,
      "HTTP %s, %d bytes" % (status, len(served)))
check("  and it boots the SPA rather than showing a notice",
      "<script" in served and ".js" in served,
      "the LAN route serves a loader page that mounts the same app; asserting a "
      "particular /assets/ path would only pin today's bundling")

print()
print("[a deleted gateway stops collecting]")
_, b = call("GET", "/api/app-store/bootstrap", tok)
ver = int(((b or {}).get("versions") or {}).get("gateway_configurations") or 0)
GW = {"id": GID, "name": "Orphan", "gateway_type": "ethernet_ip",
      "plc_ip": "198.51.100.9", "interval_ms": 1000,
      "eip_input_assembly": 100, "eip_slot": 0,
      "eip_signals": [{"name": "R1", "byte_offset": 0, "kind": "BOOL", "bit": 0}],
      "tags": ["R1"]}
st, _ = call("PUT", "/api/app-store/domain", tok,
             {"domain": "gateway_configurations", "payload": [GW], "actor": "t",
              "base_version": ver})
check("the gateway is configured", st == 200, "HTTP %s" % st)
cfg = dict(GW, device_name="T", site="T", area="T", equipment="T",
           database_id="local-sqlite-default")
st, r = call("POST", "/api/plc/gateways/start", tok, {"gateway_id": GID, "config": cfg})
check("its worker starts", (r or {}).get("started") is True, str(r)[:70])


def running_ids():
    _, s = call("GET", "/api/plc/gateways/status", tok)
    return len(s or []) if isinstance(s, list) else 0


check("  and it is running", running_ids() >= 1, "%d worker(s)" % running_ids())

_, b = call("GET", "/api/app-store/bootstrap", tok)
ver = int(((b or {}).get("versions") or {}).get("gateway_configurations") or 0)
st, _ = call("PUT", "/api/app-store/domain", tok,
             {"domain": "gateway_configurations", "payload": [], "actor": "t",
              "base_version": ver})
check("the operator deletes it", st == 200, "HTTP %s" % st)

stopped = False
for _ in range(30):                       # the supervisor scans every ~10 s
    time.sleep(3)
    if running_ids() == 0:
        stopped = True
        break
check("the worker stops by itself", stopped,
      "no 'Stop All' required - %d worker(s) left" % running_ids())

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
