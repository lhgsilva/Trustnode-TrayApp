# -*- coding: utf-8 -*-
"""One endpoint, one row shape - whichever source answers.

2026-10-20, reported: "lan lite is not loading the data correctly". The LAN
Lite view showed "no samples" and "Gateways: 0/3" while the full app on the
same machine, reading the same historian, charted live values.

`/api/lite-local/live` has two branches and they disagreed:

    customer_sql   SELECT ... tag_name, ts_utc ...     <- canonical
    local_sqlite   app_store.get_live_rows()  -> ts, tag

The Lite UI reads `ts_utc` / `tag_name`. Measured that day: 118 of 118 rows
came back with `ts_utc = None`, so

  * every chart had no plottable sample,
  * the realtime handler dropped every row (`row.tag_name !== s.tag_name`),
  * `gatewayLastSeen` stayed empty, giving "Gateways: 0/3", and
  * `Date.parse(r.ts_utc || 0)` parsed the NUMBER 0 as the STRING "0" =
    1 Jan 2000, so the header read "OFFLINE - 845835661.7 s", i.e. 26.8
    years, on an edge that was collecting normally.

A site WITH a customer database never saw any of it, which is why this
survived: the cloud Lite worked and only the local one was broken.

get_live_rows() is deliberately NOT changed - eight other callers (the full
app, cloud_live, power, plc, the report scheduler, state) read ts/tag and
work. The endpoint normalises instead, and emits BOTH spellings so a reader
of either keeps working.

Drives a real backend on a scratch workspace. Never opens the live store.
"""
from __future__ import annotations

import datetime as dt
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
PORT = os.environ.get("LITE_PORT", "8193")
API = "http://127.0.0.1:" + PORT
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:190]) if detail else ""))
    if not ok:
        FAILS.append(name)
    return bool(ok)


TMP = tempfile.mkdtemp(prefix="tn-litec-")
ENV = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=TMP,
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
ENV.pop("TRUSTNODE_APP_STORE_PATH", None)

try:
    urllib.request.urlopen(API + "/api/health", timeout=3).read()
    print("REFUSING: something already serves on port %s." % PORT)
    raise SystemExit(2)
except SystemExit:
    raise
except Exception:
    pass

LOG = open(os.path.join(TMP, "o.log"), "w")
PROC = subprocess.Popen([sys.executable, "-m", "app"],
                        cwd=os.path.join(ROOT, "backend"), env=ENV,
                        stdout=LOG, stderr=subprocess.STDOUT)


def call(method, path, body=None, token=None, timeout=90):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        API + path, data=json.dumps(body).encode() if body is not None else None,
        headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:250]
    except Exception as e:  # noqa: BLE001
        return 0, str(e)[:200]


def finish(code):
    try:
        PROC.terminate(); PROC.wait(timeout=25)
    except Exception:
        PROC.kill()
    sys.exit(code)


print("TrustNode - Lite live rows carry the field names Lite reads")
print()

up = False
for _ in range(75):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read()
        up = True
        break
    except Exception:
        time.sleep(2)
if not check("the app came up", up):
    print(io.open(os.path.join(TMP, "o.log"), encoding="utf-8",
                  errors="replace").read()[-1200:])
    finish(2)

STORE = os.path.join(TMP, "trustnode_app_store.db")
NOW = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
con = sqlite3.connect(STORE, timeout=30)
for tag in ("SimREAL[3]", "SimREAL[4]"):
    con.execute(
        "INSERT INTO historian_readings (ts_utc, created_utc, gateway_id, "
        "gateway_name, tag_name, value, quality, quality_label, source, tenant_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (NOW, NOW, "gw-test-1", "GW", tag, 12.5, 192, "GOOD", "plc", "default"))
con.commit(); con.close()

st, r = call("POST", "/api/auth/login", {"username": "admin", "password": "admin"})
TOKEN = (r or {}).get("token") if isinstance(r, dict) else None
check("an admin session was obtained", bool(TOKEN), "HTTP %s" % st)

print()
print("[1. the row shape the Lite UI actually reads]")
st, r = call("GET", "/api/lite-local/live", None, TOKEN)
rows = (r or {}).get("rows") if isinstance(r, dict) else None
if not check("live returned rows", st == 200 and isinstance(rows, list) and rows,
             "HTTP %s source=%s rows=%s"
             % (st, (r or {}).get("source"), len(rows or []))):
    finish(2)
print("      source=%s rows=%d" % ((r or {}).get("source"), len(rows)))

with_ts = [x for x in rows if x.get("ts_utc")]
with_tag = [x for x in rows if x.get("tag_name")]
check("every row carries ts_utc", len(with_ts) == len(rows),
      "%d of %d - the UI plots nothing without it, and shows OFFLINE for "
      "26.8 years because Date.parse(0) is the year 2000"
      % (len(with_ts), len(rows)))
check("  every row carries tag_name", len(with_tag) == len(rows),
      "%d of %d - the realtime handler compares row.tag_name and drops "
      "everything that lacks it" % (len(with_tag), len(rows)))

print()
print("[2. the older spellings still work, so nothing else breaks]")
check("ts is still present", all(x.get("ts") for x in rows),
      "eight other callers read ts/tag; this endpoint must not take that away")
check("  tag is still present", all(x.get("tag") for x in rows))
check("  and the two agree",
      all(x.get("ts") == x.get("ts_utc") and x.get("tag") == x.get("tag_name")
          for x in rows),
      "two spellings that disagree would be worse than one")

print()
print("[3. a gateway can be derived from what Lite receives]")
# This is what drives the "Gateways: n/m" badge.
seen = {x.get("gateway_id") for x in rows if x.get("ts_utc") and x.get("gateway_id")}
check("at least one gateway resolves a last-seen time", bool(seen), sorted(seen))

print()
print("[4. the UI no longer turns a missing timestamp into the year 2000]")
LITE = io.open(os.path.join(ROOT, "backend", "static", "lite_view", "index.html"),
               encoding="utf-8").read()
check("no Date.parse(... || 0) remains",
      "Date.parse(r.ts_utc || 0)" not in LITE,
      "the number 0 coerces to the string \"0\", which parses as "
      "2000-01-01 and reported 845835661.7 s offline")
check("  it falls back to ts, then to NaN",
      "tsMs(r.ts_utc || r.ts)" in LITE,
      "NaN is ignored by the Number.isFinite guards, which is correct")

print()
print("[4b. a stored UTC timestamp is not read as local time]")
# 2026-10-20, second report: "lite lan app still not working". The rows were
# arriving correctly by then - collection was writing 1,870 every 15 s - but
# the header read "OFFLINE - 3608.5 s" and the chart said "no samples".
# 3608.5 s is one hour: this machine is UTC+1, and the backend serialises
# ts_utc as "2026-10-20 20:21:28.491" - a SPACE and no zone. Date.parse()
# reads that shape as LOCAL, so every sample looked exactly one timezone
# offset old and fell outside the chart window.
check("there is ONE timestamp parser",
      "const tsMs = (v) =>" in LITE,
      "eleven call sites each doing their own Date.parse is how half of "
      "them end up wrong")
check("  it appends Z to a zone-less time",
      'str += "Z"' in LITE,
      "the column is named ts_utc and the value IS utc; only the reader "
      "was wrong")
check("  it leaves an already-zoned value alone",
      "const zoned =" in LITE,
      "appending Z to '...+00:00' would produce an unparseable string")
_code = chr(10).join(
    ln for ln in LITE.splitlines() if not ln.strip().startswith("//"))
check("  no raw Date.parse survives outside it",
      _code.count("Date.parse(") == 1,
      "found %d in CODE (the helper itself is the one) - every stored "
      "timestamp must go through tsMs()" % _code.count("Date.parse("))
check("  and no local variable shadows it",
      "const tsMs = Date.parse" not in LITE,
      "a local called tsMs would silently restore the bug inside that scope")

print()
print("[5. the endpoint says why, for whoever edits it next]")
SRC = io.open(os.path.join(ROOT, "backend", "app", "routers", "lite_local.py"),
              encoding="utf-8").read()
check("the normalisation is in the endpoint, not get_live_rows",
      "normalised" in SRC and "get_live_rows" in SRC)
check("  and the reason is recorded",
      "DIFFERENT ROW SHAPES" in SRC or "ts_utc` / `tag_name`" in SRC)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
