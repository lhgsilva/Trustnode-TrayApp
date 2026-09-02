# -*- coding: utf-8 -*-
"""OEE dashboard aggregates and the planning calendar.

2026-08-29, phase 1 of `docs/OEE_DASHBOARD_PLAN_2026-08-29.md`.

The pages and the 17 reusable widgets all read these endpoints, and none of
them may re-implement an OEE formula: two implementations will disagree, and
the one on screen is the one nobody can trace back to a number. So the
contract each endpoint returns is what this pins down.

Runs against its own backend on a throwaway workspace - never the live install.
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
PORT = "8131"
API = "http://127.0.0.1:" + PORT
FAILS: list[str] = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:150]) if detail else ""
    print("  {0:56s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


tmp = tempfile.mkdtemp(prefix="tn-oeedash-")
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
        try:
            return e.code, json.loads(e.read().decode() or "null")
        except Exception:
            return e.code, None
    except Exception as e:
        return 0, str(e)[:180]


def finish(code):
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    return code


print("[the module is up]")
check("the app started", up)
if not up:
    print(open(os.path.join(tmp, "o.log")).read()[-1800:])
    sys.exit(finish(2))

st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
tok = (b or {}).get("token")
if not tok:
    check("login", False, st)
    sys.exit(finish(2))

st, meta = call("GET", "/api/oee/meta", tok)
check("the OEE module answers", st == 200, st)

# A machine to aggregate over.
st, m = call("POST", "/api/oee/config/machines", tok, {
    "name": "Line 1 Filler", "line": "Line 1", "area": "Packing", "enabled": 1})
machine_id = ((m or {}).get("item") or {}).get("id") or ""
check("a machine can be configured", st == 200 and bool(machine_id), st)

# ------------------------------------------------------ dashboard aggregates
print()
print("[dashboard aggregates]")
REQUIRED_META = ("window", "timezone", "generated_utc")

st, tl = call("GET", "/api/oee/dashboard/timeline?hours=24", tok)
check("status timeline responds", st == 200 and (tl or {}).get("ok") is True, st)
check("  it returns one lane per machine",
      isinstance((tl or {}).get("lanes"), list) and len(tl["lanes"]) >= 1,
      len(((tl or {}).get("lanes")) or []))
check("  a lane names its machine",
      bool((((tl or {}).get("lanes") or [{}])[0]).get("machine_name")),
      ((tl or {}).get("lanes") or [{}])[0])
# A silently shortened timeline reads as "nothing happened after lunch".
check("  truncation is reported, never silent",
      "truncated" in (tl or {}) and "max_blocks" in (tl or {}),
      sorted((tl or {}).keys()))

st, par = call("GET", "/api/oee/dashboard/downtime-pareto?hours=24", tok)
check("downtime pareto responds", st == 200 and (par or {}).get("ok") is True, st)
check("  rows carry share and cumulative share",
      all(("share" in r and "cumulative" in r) for r in ((par or {}).get("rows") or [])),
      len(((par or {}).get("rows")) or []))

st, en = call("GET", "/api/oee/dashboard/energy?hours=24", tok)
check("energy summary responds", st == 200 and (en or {}).get("ok") is True, st)
check("  it totals used and wasted separately",
      "total_kwh" in (en or {}) and "wasted_kwh" in (en or {}), sorted((en or {}).keys()))

st, sh = call("GET", "/api/oee/dashboard/shifts?hours=24", tok)
check("shift performance responds", st == 200 and (sh or {}).get("ok") is True, st)

# Every dashboard response must say what window it covers.
print()
print("[every response states its own window]")
for label, payload in (("timeline", tl), ("pareto", par), ("energy", en), ("shifts", sh)):
    meta = (payload or {}).get("meta") or {}
    missing = [k for k in REQUIRED_META if k not in meta]
    check("  %s carries window/timezone metadata" % label, not missing, missing)

# ------------------------------------------------------- planning calendar
print()
print("[planning calendar]")
st, ev = call("POST", "/api/oee/config/planned_events", tok, {
    "name": "Weekly maintenance",
    "event_type": "planned_maintenance",
    "machine_id": machine_id,
    "start_utc": "2026-08-29 22:00:00.000",
    "end_utc": "2026-08-30 02:00:00.000",
    "exclude_from_oee": 1,
    "counts_as_planned_stop": 1,
    "notes": "monthly clean-down",
    "enabled": 1,
})
event_id = ((ev or {}).get("item") or {}).get("id") or ""
check("a planned event can be created", st == 200 and bool(event_id), st)

st, lst = call("GET", "/api/oee/config/planned_events", tok)
items = (lst or {}).get("items") or []
check("  and read back", st == 200 and len(items) == 1, len(items))
if items:
    got = items[0]
    check("  keeping its type and window",
          got.get("event_type") == "planned_maintenance"
          and str(got.get("start_utc")).startswith("2026-08-29 22:00"), got.get("event_type"))
    # exclude_from_oee is the one field that changes a NUMBER rather than a
    # picture, so it must survive the round trip exactly.
    check("  and its exclude_from_oee flag exactly",
          int(got.get("exclude_from_oee") or 0) == 1, got.get("exclude_from_oee"))

st, win = call("GET", "/api/oee/planning?from_utc=2026-08-29%2000:00:00.000"
                      "&to_utc=2026-08-31%2000:00:00.000", tok)
check("the calendar returns events overlapping a window",
      st == 200 and len((win or {}).get("events") or []) == 1,
      len(((win or {}).get("events")) or []))

st, out = call("GET", "/api/oee/planning?from_utc=2026-09-10%2000:00:00.000"
                      "&to_utc=2026-09-11%2000:00:00.000", tok)
check("  and excludes events outside it",
      st == 200 and len((out or {}).get("events") or []) == 0,
      len(((out or {}).get("events")) or []))

# ------------------------------------------------- the no-frontend-maths rule
print()
print("[the frontend must not compute OEE]")
oee_dir = os.path.join(ROOT, "frontend", "src", "components", "OEE")
offenders = []
for name in sorted(os.listdir(oee_dir)):
    if not name.endswith(".jsx"):
        continue
    src = io.open(os.path.join(oee_dir, name), encoding="utf-8", errors="replace").read()
    # An availability/performance/quality PRODUCT computed in the browser is
    # the specific thing that must not appear.
    if "availability * performance" in src.replace("\n", " "):
        offenders.append(name)
check("no OEE product is calculated in the OEE pages", not offenders, offenders)

wsrc = io.open(os.path.join(ROOT, "frontend", "src", "components", "Dashboard",
                            "OeeWidgets.jsx"), encoding="utf-8", errors="replace").read()
flat = wsrc.replace("\n", " ")
check("  nor in the dashboard widgets",
      "availability * performance" not in flat and "* quality" not in flat,
      "a widget that computes its own OEE will disagree with the module")

# ---------------------------------------------------- the widget registry
print()
print("[reusable widgets]")
reg = io.open(os.path.join(ROOT, "frontend", "src", "components", "Dashboard",
                           "widgetRegistry.js"), encoding="utf-8", errors="replace").read()
import re as _re
oee_widgets = _re.findall(r'\{ key: "(oee_[a-z_]+)"', reg)
check("all 17 OEE widgets are registered", len(oee_widgets) == 17,
      "%d registered" % len(oee_widgets))
gated = _re.findall(r'key: "(oee_[a-z_]+)"[^}]*licenseModule: "oee"', reg)
check("  every one is licence-gated like the Batch widgets",
      len(gated) == len(oee_widgets), "%d of %d gated" % (len(gated), len(oee_widgets)))

dw = io.open(os.path.join(ROOT, "frontend", "src", "components", "Dashboard",
                          "DashboardWidgets.jsx"), encoding="utf-8", errors="replace").read()
undispatched = [k for k in oee_widgets if 'case "%s"' % k not in dw]
check("  and every one is dispatched by the renderer",
      not undispatched, undispatched or "all 17")

# Loading / empty / error are the three states a dashboard tile must never
# conflate; one shared shell is what guarantees all 17 have them.
check("  widgets render loading, empty and error states",
      "Loading…" in wsrc and "widget-empty" in wsrc and "function Shell" in wsrc)

# ------------------------------------------- selection carried, not reset
print()
print("[the period survives navigation]")
app = io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
              encoding="utf-8", errors="replace").read()
check("Overview and Machine Detail share ONE selection object",
      "const [oeeSelection, setOeeSelection]" in app
      and app.count("selection={oeeSelection}") >= 2,
      "picking a shift then a machine must not reset the period")
check("  and Machine Detail is reached by clicking a machine",
      'onOpenMachine={(card) => { setOeeMachine(card); handleNavClick("oee_machine_detail"); }}' in app)
print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
