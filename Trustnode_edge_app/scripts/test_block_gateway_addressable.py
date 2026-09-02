# -*- coding: utf-8 -*-
"""A gateway that cannot address its values must not report RUNNING.

2026-08-30, from the plant: an ifm gateway was saved with four tag NAMES and
zero datapoints. It started, the row went green, `W:1436 P:0` ticked along
from an earlier run, and no sample ever arrived - "I was expecting plug and
play, and gateways is showing as running even when it was not".

Tag names are labels. The ADDRESS of each value comes from discovery -
`ifm_datapoints` over IoT, `eip_signals` or an input assembly over fieldbus.
Without it the driver has nothing to fetch. Starting anyway produces the worst
possible state: a green row asserting something the app cannot back up.

The refusal lives on the start endpoint, beside the licence checks, so it
covers the UI, the API, and anything else that activates collection. It needs
no hardware - the point is that the config is unusable before a packet is sent.
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
PORT = "8153"
API = "http://127.0.0.1:" + PORT
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:140]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("[the rule sits where collection is activated]")
src = io.open(os.path.join(ROOT, "backend", "app", "routers", "plc.py"),
              encoding="utf-8", errors="replace").read()
head = src.split("async def start_gateway_runtime", 1)[-1][:4000]
check("the start endpoint checks it can address values",
      "ifm_datapoints" in head and "eip_signals" in head,
      "one gate for the UI, the API and anything else that starts a gateway")

tmp = tempfile.mkdtemp(prefix="tn-addr-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
log = open(os.path.join(tmp, "o.log"), "w")
proc = subprocess.Popen([sys.executable, "-m", "app"],
                        cwd=os.path.join(ROOT, "backend"), env=env,
                        stdout=log, stderr=subprocess.STDOUT)
up = False
for _ in range(70):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read()
        up = True
        break
    except Exception:
        time.sleep(2)


def call(method, path, tok=None, body=None):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    d = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=d, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
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
print("[a block gateway with names but no addresses]")
check("the app started", up)
if not up:
    sys.exit(2)
st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
tok = (b or {}).get("token")

# Exactly the shape that shipped from the plant: tags, no datapoints.
st, r = call("POST", "/api/plc/gateways/start", tok, {
    "gateway_id": "gw-addr-iot",
    "config": {
        "gateway_type": "ifm_iolink", "name": "IFM", "device_name": "IFM1",
        "plc_ip": "192.168.10.250", "ifm_http_port": 80, "interval_ms": 1000,
        "tags": ["Port7_Pin2", "Port8_Pin2", "Port7_Pin4", "Port8_Pin4"],
        "ifm_datapoints": [],
        "equipment": "T", "site": "T", "area": "T",
    }})
started = (r or {}).get("started")
msg = str((r or {}).get("message") or "")
check("the ifm gateway is REFUSED, not started", started is False,
      "started=%s" % started)
check("  and the message says what is missing", "datapoint" in msg.lower(), msg[:110])
check("  and how to fix it", "search available tags" in msg.lower(), msg[:110])

st, r = call("POST", "/api/plc/gateways/start", tok, {
    "gateway_id": "gw-addr-eip",
    "config": {
        "gateway_type": "ethernet_ip", "name": "IFM fieldbus", "device_name": "IFM1",
        "plc_ip": "192.168.10.251", "eip_input_assembly": 0, "eip_signals": [],
        "interval_ms": 1000, "tags": ["Port7_Pin4"],
        "equipment": "T", "site": "T", "area": "T",
    }})
check("the fieldbus gateway with no assembly is REFUSED",
      (r or {}).get("started") is False, str((r or {}).get("started")))
check("  and names the assembly as the missing piece",
      "assembly" in str((r or {}).get("message") or "").lower(),
      str((r or {}).get("message") or "")[:110])

# The refusal must be narrow: a properly discovered config still starts.
st, r = call("POST", "/api/plc/gateways/start", tok, {
    "gateway_id": "gw-addr-ok",
    "config": {
        "gateway_type": "ethernet_ip", "name": "OK", "device_name": "IFM1",
        # TEST-NET-2: unreachable on purpose. The gateway should still be
        # ALLOWED to start - it is addressable, it just cannot connect, and
        # that is a runtime fault the status reports, not a config error.
        "plc_ip": "198.51.100.9", "eip_input_assembly": 100, "eip_slot": 0,
        "eip_signals": [{"name": "P7", "byte_offset": 0, "kind": "BOOL", "bit": 6}],
        "interval_ms": 1000, "tags": ["P7"],
        "equipment": "T", "site": "T", "area": "T",
    }})
check("an addressable gateway still starts", (r or {}).get("started") is True,
      "the guard must reject unusable config, not unreachable devices")
call("POST", "/api/plc/gateways/stop", tok, {"gateway_id": "gw-addr-ok"})

# --- a worker that has not read yet is STARTING, not RUNNING -------------
print()
print("[a gateway on its way up is not yet running]")
st, r = call("POST", "/api/plc/gateways/start", tok, {
    "gateway_id": "gw-starting",
    "config": {
        "gateway_type": "ethernet_ip", "name": "Slow", "device_name": "X",
        # Unreachable, so it never completes a cycle and stays in the
        # transitional state for as long as we care to look.
        "plc_ip": "198.51.100.9", "eip_input_assembly": 100, "eip_slot": 0,
        "eip_signals": [{"name": "P7", "byte_offset": 0, "kind": "BOOL", "bit": 6}],
        "interval_ms": 1000, "tags": ["P7"],
        "equipment": "T", "site": "T", "area": "T",
    }})
check("it starts", (r or {}).get("started") is True, str(r)[:80])
time.sleep(4)
st, statuses = call("GET", "/api/plc/gateways/status", tok)
row = next((g for g in (statuses if isinstance(statuses, list) else [])
            if isinstance(g, dict) and str(g.get("gateway_id")) == "gw-starting"), None)
check("the status carries a `starting` flag", row is not None and "starting" in row,
      "RUNNING and STOPPED are both wrong before the first read")
check("  and it is TRUE while nothing has been read",
      bool((row or {}).get("starting")) is True,
      "starting=%s running=%s" % ((row or {}).get("starting"), (row or {}).get("running")))
call("POST", "/api/plc/gateways/stop", tok, {"gateway_id": "gw-starting"})

# The UI must actually use it, and must not assert the outcome of a press.
app = io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
              encoding="utf-8", errors="replace").read()
check("the pill shows STARTING", "STARTING" in app and "STOPPING" in app)
check("  a press is recorded as pending, not as the outcome",
      "__pending" in app and "`running` is deliberately NOT set here" in app,
      "flipping running on click is what made a start read RUNNING instantly")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
