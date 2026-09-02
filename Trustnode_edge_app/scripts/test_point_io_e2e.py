# -*- coding: utf-8 -*-
"""POINT I/O behind a 1734-AENTR, read without a PLC, end to end.

    python scripts/test_point_io_e2e.py --host 192.168.10.105

2026-08-30. The adapter's own assemblies read 0 bytes because POINT I/O is
normally consumed over a Class 1 implicit connection - which is why TrustNode
could not see this rack at all. Each MODULE, however, is a CIP node reachable
by routing an explicit request through the backplane, so the rack can be polled
with no implicit connection and no PLC owning it.

This walks the whole path an operator does: scan the rack, save a gateway from
what was found, start it, and check the values reach the historian with GOOD
quality. It SKIPS when no adapter answers, so it is safe in the release gate on
a machine that has none.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = "8155"
API = "http://127.0.0.1:" + PORT
GID = "gw-pointio-e2e"
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:56s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


ap = argparse.ArgumentParser()
ap.add_argument("--host", default="192.168.10.105")
args = ap.parse_args()

print("TrustNode - POINT I/O without a PLC")
print("  adapter: %s" % args.host)
try:
    with socket.create_connection((args.host, 44818), timeout=2):
        pass
except Exception as exc:
    print("SKIP: nothing answers at %s:44818 (%s)" % (args.host, exc))
    sys.exit(0)

# --- the driver alone, before any app ------------------------------------
sys.path.insert(0, os.path.join(ROOT, "backend"))
from app.drivers.point_io import PointIoClient, datapoints_from_scan  # noqa: E402

print()
print("[the rack, straight from the adapter]")
cli = PointIoClient(args.host, timeout_s=3.0)
t0 = time.perf_counter()
modules = cli.scan(max_slots=8)
scan_ms = (time.perf_counter() - t0) * 1000
cli.close()
for m in modules:
    print("     slot %d  %-32s %-9s %d point(s)%s"
          % (m["slot"], str(m["name"])[:32], m.get("mode"), m.get("points"),
             "  [" + m["note"][:60] + "]" if m.get("note") else ""))
check("the backplane scan finds modules", len(modules) >= 1,
      "%d module(s) in %.0f ms" % (len(modules), scan_ms))
points = datapoints_from_scan(modules)
check("  every point becomes a datapoint", len(points) >= 1, "%d point(s)" % len(points))
check("  named by where they are on the panel",
      all(str(p["name"]).startswith("Slot") for p in points),
      points[0]["name"] if points else "")

# A cycle must be cheap enough to poll. The whole rack, timed.
cli = PointIoClient(args.host, timeout_s=3.0)
t0 = time.perf_counter()
for m in modules:
    cli.read_module(m)
cycle_ms = (time.perf_counter() - t0) * 1000
cli.close()
check("  a whole-rack read fits a 1 s cycle", cycle_ms < 500,
      "%.0f ms for %d module(s)" % (cycle_ms, len(modules)))

# --- the app, the way the operator drives it -----------------------------
tmp = tempfile.mkdtemp(prefix="tn-pio-")
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


def call(method, path, tok=None, body=None, timeout=90):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    d = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=d, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return 0, str(e)[:140]


def finish(code):
    call("POST", "/api/plc/gateways/stop", tok, {"gateway_id": GID})
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    return code


print()
print("[the app: scan, configure, collect]")
tok = None
check("the app started", up)
if not up:
    sys.exit(2)
st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
tok = (b or {}).get("token")

st, r = call("POST", "/api/plc/pointio/scan", tok, {"ip": args.host})
check("the scan endpoint reports the rack", (r or {}).get("ok") is True,
      (r or {}).get("message"))
api_mods = (r or {}).get("modules") or []
api_pts = (r or {}).get("datapoints") or []
check("  and hands back modules and datapoints",
      len(api_mods) == len(modules) and len(api_pts) == len(points),
      "%d module(s), %d point(s)" % (len(api_mods), len(api_pts)))

# A gateway with no modules must be refused, not started green.
st, r = call("POST", "/api/plc/gateways/start", tok, {
    "gateway_id": "gw-pio-empty",
    "config": {"gateway_type": "point_io", "name": "empty", "device_name": "AENTR",
               "plc_ip": args.host, "point_io_modules": [], "tags": ["Slot1_Pt1"],
               "interval_ms": 1000, "site": "T", "area": "T", "equipment": "T"}})
check("a rack-less POINT I/O gateway is refused", (r or {}).get("started") is False,
      str((r or {}).get("message"))[:90])

cfg = {"gateway_type": "point_io", "name": "POINT IO", "device_name": "1734-AENTR",
       "plc_ip": args.host, "point_io_modules": api_mods,
       "tags": [p["name"] for p in api_pts], "interval_ms": 1000,
       "site": "Plant", "area": "Cell", "equipment": "Rack"}
st, r = call("POST", "/api/plc/gateways/start", tok, {"gateway_id": GID, "config": cfg})
check("the gateway starts", (r or {}).get("started") is True, str(r)[:100])
if not (r or {}).get("started"):
    sys.exit(finish(2))

time.sleep(14)
st, live = call("GET", "/api/app-store/live?limit=2000", tok)
rows = [x for x in ((live or {}).get("rows") or []) if str(x.get("gateway_id")) == GID]
good = [x for x in rows if int(x.get("quality") or 0) >= 192]
check("values reach the live cache", len(rows) >= 1, "%d tag(s)" % len(rows))
check("  with GOOD quality", len(good) == len(rows) and bool(rows),
      "%d of %d GOOD" % (len(good), len(rows)))
# Discrete points must be 0/1; an analog channel is a count and may be any
# number - asserting 0/1 across the board failed the moment a 1734-IE4C was
# put in the rack, against a driver that was reading it correctly.
# Analog is identified by CHANNEL (AI/AO), not by the declared type string -
# the type changed from INT to REAL when scaling was added, and keying on it
# silently reclassified every analog channel as discrete.
_analog = {p["name"] for p in api_pts if str(p.get("channel", "")).startswith("A")}
_disc = [x for x in good if str(x.get("tag") or x.get("tag_name")) not in _analog]
check("  discrete points are 0 or 1",
      all(x.get("value") in (0.0, 1.0, 0, 1) for x in _disc),
      sorted({str(x.get("value")) for x in _disc}) or "none present")
check("  analog channels carry a number",
      all(isinstance(x.get("value"), (int, float)) for x in good
          if str(x.get("tag") or x.get("tag_name")) in _analog),
      "%d analog channel(s)" % len(_analog))

st, hist = call("GET", "/api/app-store/historian?limit=800", tok)
hrows = [x for x in ((hist or {}).get("rows") or []) if str(x.get("gateway_id")) == GID]
check("and the historian is being written", len(hrows) >= len(api_pts),
      "%d row(s) - this is what the charts read" % len(hrows))

st, statuses = call("GET", "/api/plc/gateways/status", tok)
row = next((g for g in (statuses if isinstance(statuses, list) else [])
            if isinstance(g, dict) and str(g.get("gateway_id")) == GID), None)
check("the gateway reports itself running", bool(row) and row.get("running") is True)
check("  with no error", not str((row or {}).get("last_error") or ""),
      str((row or {}).get("last_error"))[:90])

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
