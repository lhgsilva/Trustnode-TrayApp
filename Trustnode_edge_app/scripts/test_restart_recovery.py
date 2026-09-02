# -*- coding: utf-8 -*-
"""Restarting the app must lose nothing, and deleting must be allowed.

2026-08-31, from the plant: "we should get the restarting of the app full
proved for the recovery, no data lost or configuration... I have deleted all
the gateways and devices and still showing error, this also should not happen.
Make the logic simpler, reliable and full recovery."

Both halves are tested here, because they are the same design decision.

Three guards used to GUESS whether a write was intended - refuse an empty
list, refuse dropping 2+ items, refuse a list sharing no ids. Every one is a
heuristic, and heuristics have false positives: deleting all your devices is
an ordinary action and the app answered "Refused to clear 1 saved item(s) in
'devices'".

They are replaced by one fact. A save carries `base_version`, the version the
client last read. Match it and ANY change is honoured - including deleting
everything. Miss it and nothing is written, because the writer did not know
what it was replacing. That is the same protection the guards were reaching
for, without the guessing.

The restart half is deliberately an ABRUPT kill, not a graceful shutdown: a
recovery guarantee that only holds when the process exits politely is not a
recovery guarantee. Power gets pulled.
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
PORT = "8172"
API = "http://127.0.0.1:" + PORT
FAILS: list[str] = []
tmp = tempfile.mkdtemp(prefix="tn-restart-")


def check(name, ok, detail=""):
    print("  {0:56s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


def boot():
    env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
               TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
               TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
    log = open(os.path.join(tmp, "o.log"), "a")
    p = subprocess.Popen([sys.executable, "-m", "app"],
                         cwd=os.path.join(ROOT, "backend"), env=env,
                         stdout=log, stderr=subprocess.STDOUT)
    for _ in range(80):
        try:
            urllib.request.urlopen(API + "/api/health", timeout=3).read()
            return p
        except Exception:
            time.sleep(2)
    return p


def kill(p):
    """Abrupt. Recovery must not depend on a clean exit."""
    try:
        p.kill()
        p.wait(timeout=20)
    except Exception:
        pass
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


def login():
    return (call("POST", "/api/auth/login",
                 body={"username": "admin", "password": "admin"})[1] or {}).get("token")


def snapshot(tok):
    _, b = call("GET", "/api/app-store/bootstrap", tok)
    return (b or {}).get("data") or {}, (b or {}).get("versions") or {}


GATEWAYS = [
    {"id": "gw-plc", "name": "PLC", "gateway_type": "allen_bradley",
     "plc_ip": "192.168.10.240", "interval_ms": 1000,
     "tags": ["Tag%d" % i for i in range(49)]},
    {"id": "gw-pio", "name": "Rack", "gateway_type": "point_io",
     "plc_ip": "192.168.10.105", "interval_ms": 1000, "tags": ["Slot3_Pt1"],
     "point_io_modules": [{"slot": 3, "name": "1734-IE4C", "points": 4}],
     "point_io_points": [{"address": "Slot3_Pt1", "name": "Slot3_Pt1",
                          "channel": "AI", "enabled": True, "scale": 0.00122,
                          "unit": "mA"}]},
]
DEVICES = [{"id": "dev-1", "name": "1734-AENTR", "plc_ip": "192.168.10.105"},
           {"id": "dev-2", "name": "EM1", "plc_ip": "192.168.10.200"}]
DBS = [{"id": "local-sqlite-default", "name": "Local SQLite", "engine": "sqlite"}]

print("[a full configuration survives an abrupt restart]")
proc = boot()
tok = login()
check("the app started", bool(tok))
if not tok:
    sys.exit(2)

_, versions = snapshot(tok)
for dom, payload in (("gateway_configurations", GATEWAYS),
                     ("devices", DEVICES),
                     ("database_configurations", DBS)):
    st, res = call("PUT", "/api/app-store/domain", tok,
                   {"domain": dom, "payload": payload, "actor": "test",
                    "base_version": int(versions.get(dom) or 0)})
    check("saved %s" % dom, st == 200, "HTTP %s" % st)

before, vbefore = snapshot(tok)
check("everything is stored", len(before.get("gateway_configurations") or []) == 2
      and len(before.get("devices") or []) == 2,
      "%d gateway(s), %d device(s)" % (len(before.get("gateway_configurations") or []),
                                       len(before.get("devices") or [])))
check("bootstrap publishes a version per domain",
      all(str(d) in vbefore for d in ("gateway_configurations", "devices")),
      json.dumps({k: vbefore.get(k) for k in
                  ("gateway_configurations", "devices", "database_configurations")}))

kill(proc)
proc = boot()
tok = login()
after, vafter = snapshot(tok)
for dom in ("gateway_configurations", "devices", "database_configurations"):
    same = json.dumps(before.get(dom), sort_keys=True) == json.dumps(after.get(dom), sort_keys=True)
    check("%s is identical after restart" % dom, same,
          "" if same else "before=%s after=%s"
          % (json.dumps(before.get(dom))[:60], json.dumps(after.get(dom))[:60]))
check("  the POINT I/O rack mapping survived too",
      len(((after.get("gateway_configurations") or [{}])[1] or {}).get("point_io_points") or []) == 1,
      "modules and per-point scaling are part of the configuration")
check("  and the versions did not move", vafter == vbefore,
      "a restart is not an edit")

print()
print("[deleting is allowed - it is an instruction, not an accident]")
st, _ = call("PUT", "/api/app-store/domain", tok,
             {"domain": "devices", "payload": [], "actor": "test",
              "base_version": int(vafter.get("devices") or 0)})
now, vnow = snapshot(tok)
check("deleting EVERY device succeeds", st == 200 and (now.get("devices") or []) == [],
      "HTTP %s - this is what 'Refused to clear 1 saved item(s)' blocked" % st)

st, _ = call("PUT", "/api/app-store/domain", tok,
             {"domain": "gateway_configurations", "payload": [], "actor": "test",
              "base_version": int(vnow.get("gateway_configurations") or 0)})
now, vnow = snapshot(tok)
check("deleting EVERY gateway succeeds",
      st == 200 and (now.get("gateway_configurations") or []) == [], "HTTP %s" % st)

print()
print("[a write that did not read current state is refused]")
st, _ = call("PUT", "/api/app-store/domain", tok,
             {"domain": "gateway_configurations",
              "payload": [{"id": "gw-blind", "name": "Blind"}], "actor": "test",
              "base_version": 0})          # a client that never read
now2, _ = snapshot(tok)
check("a stale base_version is REFUSED", st == 409, "HTTP %s" % st)
check("  and nothing was changed",
      json.dumps(now2.get("gateway_configurations")) == json.dumps(now.get("gateway_configurations")))

print()
print("[the deletion itself survives a restart]")
kill(proc)
proc = boot()
tok = login()
final, _ = snapshot(tok)
check("gateways are still deleted", (final.get("gateway_configurations") or []) == [])
check("devices are still deleted", (final.get("devices") or []) == [])
check("  but the database connection is untouched",
      len(final.get("database_configurations") or []) == 1,
      "deleting one domain must not disturb another")

kill(proc)
print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
