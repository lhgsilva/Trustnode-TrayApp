# -*- coding: utf-8 -*-
"""What the OEE Overview page needs from the service - and only from it.

2026-08-31, implementing OEE > Overview to spec. The brief was explicit that
the page must read central OEE outputs and must not grow calculations of its
own, so three things the design needs had to exist server-side first:

  * NUMBER OF STOPS, so "toggle between Duration and Number of Stops" has
    something to toggle to. Fourteen two-minute stops and one 28-minute stop
    are the same bar by duration and completely different problems.
  * GROUPING by reason, category, machine and line - and NOT by product, order
    or shift, because oee_machine_events carries no product or order column. A
    menu entry for a grouping the data cannot support is a promise the page
    cannot keep, so the endpoint reports which groupings are real.
  * A PREVIOUS PERIOD, computed through the same code path over the
    equal-length window immediately before, so "vs previous shift" compares
    like with like.

Runs against its own backend on a throwaway workspace - never the live install.
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
PORT = "8184"
API = "http://127.0.0.1:" + PORT
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:56s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


tmp = tempfile.mkdtemp(prefix="tn-oeeov-")
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

st, m = call("POST", "/api/oee/config/machines", tok,
             {"name": "Line 1 Filler", "line": "Line 1", "area": "Packing", "enabled": 1})
machine_id = ((m or {}).get("item") or {}).get("id") or ""
check("a machine can be configured", st == 200 and bool(machine_id), st)

print()
print("[the KPI cards can show a comparison]")
st, ov = call("GET", "/api/oee/overview?hours=8", tok)
check("overview responds", st == 200 and (ov or {}).get("ok") is True, st)
check("  and does NOT compute a previous period unless asked",
      "previous" not in (ov or {}),
      "it doubles the work; a caller that does not need it should not pay")

st, ov2 = call("GET", "/api/oee/overview?hours=8&compare=1", tok)
prev = (ov2 or {}).get("previous") or {}
check("compare=1 returns the previous window", st == 200 and bool(prev),
      json.dumps(prev)[:110])
check("  it is the equal-length window immediately before",
      bool(prev.get("from_utc")) and bool(prev.get("to_utc"))
      and str(prev.get("to_utc")) == str((ov2 or {}).get("from_utc")),
      "prev_to=%s current_from=%s" % (prev.get("to_utc"), (ov2 or {}).get("from_utc")))
check("  and carries the same totals shape",
      isinstance(prev.get("totals"), dict)
      and "oee" in (prev.get("totals") or {}),
      "a comparison against a differently-shaped number is not a comparison")

print()
print("[the Pareto can be ranked and grouped]")
st, pa = call("GET", "/api/oee/dashboard/downtime-pareto?hours=8", tok)
check("pareto responds", st == 200 and (pa or {}).get("ok") is True, st)
check("  it says which groupings the data supports",
      set((pa or {}).get("groups_supported") or []) == {"reason", "category", "machine", "line"},
      (pa or {}).get("groups_supported"))
check("  product/order/shift are NOT offered",
      not ({"product", "order", "shift"} & set((pa or {}).get("groups_supported") or [])),
      "an event has no product or order column; offering it would be a lie")
check("  rows carry BOTH duration and stops",
      all(("seconds" in r and "stops" in r) for r in ((pa or {}).get("rows") or [])) or
      not ((pa or {}).get("rows") or []),
      "so the UI toggles without another round trip")

for group in ("reason", "category", "machine", "line"):
    st, g = call("GET", "/api/oee/dashboard/downtime-pareto?hours=8&group_by=%s" % group, tok)
    check("  group_by=%s is honoured" % group,
          st == 200 and str((g or {}).get("group_by")) == group,
          (g or {}).get("group_by"))

st, g = call("GET", "/api/oee/dashboard/downtime-pareto?hours=8&group_by=nonsense", tok)
check("  an unknown grouping falls back to reason, not an error",
      st == 200 and str((g or {}).get("group_by")) == "reason",
      "a bad menu value must not break the page")

st, ms = call("GET", "/api/oee/dashboard/downtime-pareto?hours=8&metric=stops", tok)
check("  metric=stops is honoured",
      st == 200 and str((ms or {}).get("metric")) == "stops", (ms or {}).get("metric"))

print()
print("[the page's other inputs still answer]")
for label, path in (("trend", "/api/oee/trend?hours=8&buckets=8"),
                    ("timeline", "/api/oee/dashboard/timeline?hours=8"),
                    ("machines live", "/api/oee/machines/live"),
                    ("meta", "/api/oee/meta")):
    st, b = call("GET", path, tok)
    check("%s responds" % label, st == 200 and (b or {}).get("ok") is not False, st)

print()
print("[maturity comes from the service, not from a guess in the page]")
st, ovm = call("GET", "/api/oee/overview?hours=8", tok)
tot = (ovm or {}).get("totals") or {}
check("totals say how complete the figure is",
      bool(tot.get("stage")),
      "the UI has carried maturity labels since day one with nothing to "
      "fill them in; every badge rendered as nothing")
check("  and how many machines actually produced one",
      "machines_counted" in tot and "machines_total" in tot,
      "%s of %s" % (tot.get("machines_counted"), tot.get("machines_total")))
res = ((ovm or {}).get("results") or [{}])[0]
check("  each machine result carries its own stage",
      "stage" in res and "missing_factors" in res,
      "%s missing=%s" % (res.get("stage"), res.get("missing_factors")))
check("  a machine with no cycle time is NOT reported as full",
      str(res.get("stage")) != "full"
      or not (res.get("missing_factors") or []),
      "stage=%s missing=%s - a stage of 'full' with a missing factor would "
      "be the exact over-claim the brief forbids"
      % (res.get("stage"), res.get("missing_factors")))
st, tr = call("GET", "/api/oee/trend?hours=8&buckets=4", tok)
brow = ((tr or {}).get("buckets") or (tr or {}).get("rows") or [{}])[0]
check("  trend buckets carry it too",
      "stage" in brow and "machines_total" in brow,
      "so a dip caused by a machine dropping out reads differently from a "
      "dip caused by the plant slowing down")

print()
print("[the page reads the service; it does not recompute OEE]")
import io as _io  # noqa: E402
parts = _io.open(os.path.join(ROOT, "frontend", "src", "components", "OEE",
                              "OeeOverviewParts.jsx"), encoding="utf-8",
                 errors="replace").read()
page = _io.open(os.path.join(ROOT, "frontend", "src", "components", "OEE",
                             "OeeOverview.jsx"), encoding="utf-8",
                errors="replace").read()
check("the KPI delta is a difference, not an OEE formula",
      "availability * performance" not in parts.lower()
      and "def " not in parts and "a - b) / Math.abs(b)" in parts,
      "the only arithmetic allowed here is comparing two figures the "
      "service returned")
check("the page asks the service for the comparison",
      "compare: 1" in page,
      "rather than computing the previous window itself")
check("the Pareto offers only supported groupings",
      "paretoData?.groups_supported" in page,
      "the menu is built from what the data can answer")
check("KPI cards explain how each number is made",
      "KPI_HINTS" in parts and "Availability × Performance × Quality" in parts)
check("missing data is never rendered as zero",
      "Not enough data" in parts,
      "0% OEE and an unconfigured plant must not look the same")
check("trend lines can be switched off",
      "TREND_SERIES" in parts and "onToggle" in parts)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
