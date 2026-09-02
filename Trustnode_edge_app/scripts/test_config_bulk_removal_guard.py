# -*- coding: utf-8 -*-
"""An uninformed write cannot replace a configuration. An informed one can.

History of this file. It began on 2026-08-30, after a save from a half-loaded
page destroyed a PLC gateway with 49 tags, and it asserted a heuristic: refuse
a write that drops two or more items. A sibling guard refused writes whose ids
were wholly disjoint from what was stored.

Both were replaced on 2026-08-31, because the operator hit their false
positives immediately - deleting every device is an ordinary action, and the
app answered "Refused to clear 1 saved item(s) in 'devices'". Guards that
guess at intent block real work while still missing real damage: the original
incident removed exactly ONE gateway and passed the count test.

The replacement asks a question with an exact answer. Every save carries
`base_version`, the version the client last read:

    match  -> the client is editing current state; ANY change is honoured,
              including emptying the collection
    miss   -> the client never read this state, or has been overtaken;
              nothing is written and it is told to reload

This file now tests that rule, and specifically that it still protects the
case it was born from.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = "8163"
API = "http://127.0.0.1:" + PORT
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:140]) if detail else ""))
    if not ok:
        FAILS.append(name)


tmp = tempfile.mkdtemp(prefix="tn-guard-")
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
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return 0, str(e)[:140]


def finish(code):
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    return code


def state():
    _, b = call("GET", "/api/app-store/bootstrap", tok)
    data = (b or {}).get("data") or {}
    vers = (b or {}).get("versions") or {}
    return (data.get("gateway_configurations") or [],
            int(vers.get("gateway_configurations") or 0))


print("[a page that never read the configuration cannot replace it]")
check("the app started", up)
if not up:
    sys.exit(2)
tok = (call("POST", "/api/auth/login",
            body={"username": "admin", "password": "admin"})[1] or {}).get("token")

THREE = [
    {"id": "gw-plc", "name": "PLC", "gateway_type": "allen_bradley",
     "plc_ip": "10.0.0.1", "tags": ["a"] * 49},
    {"id": "gw-ifm", "name": "IFM", "gateway_type": "ifm_iolink", "plc_ip": "10.0.0.2"},
    {"id": "gw-mtr", "name": "Meter", "gateway_type": "modbus_tcp", "plc_ip": "10.0.0.3"},
]
_, v0 = state()
st, _ = call("PUT", "/api/app-store/domain", tok,
             {"domain": "gateway_configurations", "payload": THREE, "actor": "test",
              "base_version": v0})
gws, v1 = state()
check("three gateways are saved", st == 200 and len(gws) == 3, len(gws))

# The 2026-08-30 incident: a page holding nothing saves one gateway over three.
st, _ = call("PUT", "/api/app-store/domain", tok,
             {"domain": "gateway_configurations",
              "payload": [{"id": "gw-new", "name": "IFM", "gateway_type": "ifm_iolink",
                           "plc_ip": "10.0.0.9"}], "actor": "test",
              "base_version": 0})            # a client that never read
after, v2 = state()
check("a write from an unread version is REFUSED", st == 409,
      "HTTP %s - 409, not 500: a deliberate refusal the UI can show" % st)
check("  and nothing was changed", len(after) == 3,
      "%d gateway(s) still stored" % len(after))
check("  the PLC and its 49 tags survive",
      any(g.get("id") == "gw-plc" and len(g.get("tags") or []) == 49 for g in after))

# An informed client may do exactly the same thing.
st, _ = call("PUT", "/api/app-store/domain", tok,
             {"domain": "gateway_configurations",
              "payload": [{"id": "gw-new", "name": "IFM", "gateway_type": "ifm_iolink",
                           "plc_ip": "10.0.0.9"}], "actor": "test",
              "base_version": v2})
after, v3 = state()
check("the SAME write from the current version is allowed",
      st == 200 and len(after) == 1 and after[0].get("id") == "gw-new",
      "HTTP %s, %d stored - intent is proven by the version, not the shape" % (st, len(after)))

# Deleting everything is an instruction, not an accident.
st, _ = call("PUT", "/api/app-store/domain", tok,
             {"domain": "gateway_configurations", "payload": [], "actor": "test",
              "base_version": v3})
after, _ = state()
check("deleting every gateway is allowed", st == 200 and after == [],
      "HTTP %s - the old guard answered 'Refused to clear 1 saved item(s)'" % st)

# Devices behave identically.
_, b = call("GET", "/api/app-store/bootstrap", tok)
dv = int(((b or {}).get("versions") or {}).get("devices") or 0)
DEVS = [{"id": "d1", "name": "A"}, {"id": "d2", "name": "B"}, {"id": "d3", "name": "C"}]
call("PUT", "/api/app-store/domain", tok,
     {"domain": "devices", "payload": DEVS, "actor": "test", "base_version": dv})
st, _ = call("PUT", "/api/app-store/domain", tok,
             {"domain": "devices", "payload": [{"id": "d9", "name": "Z"}],
              "actor": "test", "base_version": 0})
_, b = call("GET", "/api/app-store/bootstrap", tok)
devs = ((b or {}).get("data") or {}).get("devices") or []
check("devices are protected the same way", st == 409 and len(devs) == 3,
      "HTTP %s, %d device(s)" % (st, len(devs)))

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
