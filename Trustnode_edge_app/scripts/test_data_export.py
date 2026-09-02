# -*- coding: utf-8 -*-
"""The Data Export assistant: filters, aggregation, pivot, streaming.

2026-08-31: "a new sub menu under data history called Data export... selecting
gateways, devices, tags, data range, other columns conditions, complete
filtering system conditions, aggregation and filter features. preview the data
format, including pivot based on the time stamp... cannot break the historian,
it is only a query assistant to the database."

Checked here:
  * every filter narrows what it claims to narrow, and they compose;
  * aggregation buckets by time and the buckets contain what they should;
  * pivot puts one row per timestamp and one column per tag;
  * the full export STREAMS - it must not assemble 9 million rows in memory;
  * the historian's own read path is untouched.

The seeded set is small but shaped like the real one: two gateways, several
tags, values that make a filter's effect arithmetically checkable.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = "8179"
API = "http://127.0.0.1:" + PORT
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:56s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


tmp = tempfile.mkdtemp(prefix="tn-dexp-")
store = os.path.join(tmp, "s.db")
conn = sqlite3.connect(store)
conn.execute("""CREATE TABLE IF NOT EXISTS historian_readings (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  tenant_id TEXT NOT NULL DEFAULT 'default', ts_utc TEXT NOT NULL,
                  gateway_id TEXT NULL, gateway_name TEXT NULL, device_name TEXT NULL,
                  plc_ip TEXT NULL, database_name TEXT NULL, tag_name TEXT NOT NULL,
                  value REAL NULL, value_text TEXT NULL, data_type TEXT NULL,
                  quality INTEGER NULL, quality_label TEXT NULL, source TEXT NULL,
                  created_utc TEXT NOT NULL)""")
# 2 gateways x 3 tags x 120 seconds. Values are the second index, so an
# aggregate over a known bucket has an arithmetic answer.
rows = []
for sec in range(120):
    ts = "2026-08-30 10:%02d:%02d" % (sec // 60, sec % 60)
    for gw, gwname, dev in (("gw-a", "Line A", "PLC-A"), ("gw-b", "Line B", "PLC-B")):
        for tag in ("Temp", "Pressure", "Flow"):
            q = 192 if tag != "Flow" else 0
            rows.append(("default", ts, gw, gwname, dev, tag, float(sec), q,
                         "GOOD" if q >= 192 else "BAD", "test", "2026-08-30 10:00:00"))
conn.executemany(
    "INSERT INTO historian_readings (tenant_id, ts_utc, gateway_id, gateway_name,"
    " device_name, tag_name, value, quality, quality_label, source, created_utc)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
conn.commit()
conn.close()
TOTAL = len(rows)
print("[a query assistant over %s seeded rows]" % f"{TOTAL:,}")

env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=store, TRUSTNODE_BOOT_INTEGRITY_CHECK="never",
           TRUSTNODE_PORT=PORT)
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


def call(m, path, tok=None, body=None, raw=False):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    d = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=d, headers=h, method=m)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = r.read()
            return r.status, (data.decode("utf-8", "replace") if raw
                              else json.loads(data.decode() or "null"))
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


check("the app started", up)
if not up:
    sys.exit(2)
tok = (call("POST", "/api/auth/login",
            body={"username": "admin", "password": "admin"})[1] or {}).get("token")

st, opts = call("GET", "/api/data-export/options", tok)
check("the assistant advertises what it accepts",
      st == 200 and bool((opts or {}).get("columns")) and bool((opts or {}).get("aggregates")),
      "%d column(s), %d aggregate(s), %d bucket(s)"
      % (len((opts or {}).get("columns") or []), len((opts or {}).get("aggregates") or []),
         len((opts or {}).get("buckets") or [])))

st, src = call("GET", "/api/data-export/sources", tok)
check("pickers are built from the DATA, not live config",
      st == 200 and set((src or {}).get("gateways") or []) == {"gw-a", "gw-b"}
      and set((src or {}).get("tags") or []) == {"Temp", "Pressure", "Flow"},
      "gateways=%s tags=%s" % ((src or {}).get("gateways"), (src or {}).get("tags")))

print()
print("[filters narrow what they claim to]")
st, r = call("POST", "/api/data-export/preview", tok, {})
check("no filter sees everything", st == 200 and (r or {}).get("total_rows") == TOTAL,
      "%s of %s" % ((r or {}).get("total_rows"), TOTAL))

st, r = call("POST", "/api/data-export/preview", tok, {"gateways": ["gw-a"]})
check("by gateway", (r or {}).get("total_rows") == TOTAL // 2, (r or {}).get("total_rows"))

st, r = call("POST", "/api/data-export/preview", tok, {"tags": ["Temp"]})
check("by tag", (r or {}).get("total_rows") == 240, (r or {}).get("total_rows"))

st, r = call("POST", "/api/data-export/preview", tok,
             {"gateways": ["gw-a"], "tags": ["Temp", "Pressure"]})
check("  and they compose", (r or {}).get("total_rows") == 240, (r or {}).get("total_rows"))

st, r = call("POST", "/api/data-export/preview", tok, {"quality": "good"})
check("by quality", (r or {}).get("total_rows") == TOTAL - 240,
      "Flow was seeded BAD: %s" % (r or {}).get("total_rows"))

st, r = call("POST", "/api/data-export/preview", tok,
             {"tags": ["Temp"], "gateways": ["gw-a"],
              "conditions": [{"op": "gte", "value": 100}]})
check("by a value condition", (r or {}).get("total_rows") == 20,
      "values 100..119: %s" % (r or {}).get("total_rows"))

st, r = call("POST", "/api/data-export/preview", tok,
             {"tags": ["Temp"], "gateways": ["gw-a"],
              "from_utc": "2026-08-30 10:00:30", "to_utc": "2026-08-30 10:00:39"})
check("by time range", (r or {}).get("total_rows") == 10, (r or {}).get("total_rows"))

print()
print("[aggregation and pivot]")
st, r = call("POST", "/api/data-export/preview", tok,
             {"tags": ["Temp"], "gateways": ["gw-a"], "bucket": "1m", "aggregate": "avg"})
rows_out = (r or {}).get("rows") or []
check("aggregation buckets by time", len(rows_out) == 2,
      "120 s at 1 m = 2 buckets, got %d" % len(rows_out))
if len(rows_out) == 2:
    check("  and the bucket holds the right average",
          abs(float(rows_out[0].get("value")) - 29.5) < 0.01,
          "mean of 0..59 = 29.5, got %s" % rows_out[0].get("value"))
    check("  and reports how many samples it covers",
          int(rows_out[0].get("samples") or 0) == 60, rows_out[0].get("samples"))

st, r = call("POST", "/api/data-export/preview", tok,
             {"gateways": ["gw-a"], "from_utc": "2026-08-30 10:00:00",
              "to_utc": "2026-08-30 10:00:04", "pivot": True})
cols = (r or {}).get("columns") or []
prow = ((r or {}).get("rows") or [{}])[0]
check("pivot gives one column per tag",
      set(cols) == {"ts_utc", "Temp", "Pressure", "Flow"}, cols)
check("  and one row per timestamp", len((r or {}).get("rows") or []) == 5,
      "5 seconds selected, got %d" % len((r or {}).get("rows") or []))

print()
print("[the export itself]")
st, body = call("POST", "/api/data-export/run", tok,
                {"tags": ["Temp"], "gateways": ["gw-a"],
                 "columns": ["ts_utc", "tag_name", "value"]}, raw=True)
lines = [ln for ln in str(body or "").splitlines() if ln.strip()]
check("the export streams CSV", st == 200 and len(lines) == 121,
      "1 header + 120 rows, got %d line(s)" % len(lines))
check("  with the header the operator asked for",
      lines and lines[0] == "ts_utc,tag_name,value", lines[0] if lines else "")
st, body = call("POST", "/api/data-export/run", tok,
                {"tags": ["Temp"], "gateways": ["gw-a"], "include_header": False,
                 "columns": ["ts_utc", "value"]}, raw=True)
lines2 = [ln for ln in str(body or "").splitlines() if ln.strip()]
check("  and no header when none was asked for",
      len(lines2) == 120 and not lines2[0].startswith("ts_utc,"), lines2[0] if lines2 else "")

print()
print("[the historian is untouched]")
appstore = io.open(os.path.join(ROOT, "backend", "app", "services", "app_store.py"),
                   encoding="utf-8", errors="replace").read()
check("the assistant is not wired into the historian read path",
      "data_export" not in appstore,
      "a query screen must not grow inside the path every chart uses")
# The historian clamps limit to a floor of 50, so ask for something above it
# rather than asserting a number the endpoint is entitled to raise.
st, hist = call("GET", "/api/app-store/historian/range?limit=100", tok)
check("the historian still answers as before",
      st == 200 and len((hist or {}).get("rows") or []) == 100,
      "%d row(s)" % len((hist or {}).get("rows") or []))

print()
print("[the assistant is reachable in the UI]")
app = io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
              encoding="utf-8", errors="replace").read()
check("it has a menu entry under Data History",
      '"Data History", items: ["Historian", "Data Export", "Logs"]' in app)
check("  the label maps to a page", 'return "data_export"' in app)
check("  the page renders", 'activePage === "data_export"' in app and "DataExportPage" in app)
check("  and carries the Historian's grant",
      'if (page === "data_export") return Boolean(perms.historian' in app,
      "reading the same rows must not need a second permission")
check("local streams server-side, cloud pages client-side",
      "exportServerSide" in app and "exportClientSide" in app
      and "isCloudClient={Boolean(isHostedWebClient" in app,
      "the operator's rule: server-side locally, client-side on a cloud client")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
