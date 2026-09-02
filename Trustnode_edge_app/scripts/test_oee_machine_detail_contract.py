# -*- coding: utf-8 -*-
"""What the OEE Machine Detail page needs from the service - and only from it.

2026-08-31, implementing OEE > Overview > Machine Detail. The brief was again
explicit that the page reads central OEE outputs and grows no calculations of
its own, so what the design needs had to exist server-side first:

  * A PREVIOUS PERIOD for one machine, so the KPI cards can show "vs previous
    shift" as a difference between two figures the service produced.
  * A PRODUCTION TARGET per bucket, so the Production Count chart has
    something to draw the dashed line at. It comes from the configured ideal
    cycle time - the same number Performance is measured against - so the
    chart and the KPI cannot disagree. It is null when no cycle time is
    configured, because a target of zero reads as "make nothing".
  * PARTIAL EDITS of a downtime event. This one is a bug fix, not a feature:
    the endpoint used to write all four columns on every call, so the brief's
    "add a comment" and "mark planned" actions would each have silently erased
    the reason and category somebody walked to the machine to establish.

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
PORT = "8188"
API = "http://127.0.0.1:" + PORT
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:56s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


tmp = tempfile.mkdtemp(prefix="tn-oeemd-")
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
             {"name": "Detail Filler", "line": "Line 1", "area": "Packing",
              "enabled": 1, "manual_enabled": 1, "ideal_cycle_time_s": 12.0})
mid = ((m or {}).get("item") or {}).get("id") or ""
check("a machine can be configured", st == 200 and bool(mid), st)

print()
print("[the KPI cards can show a comparison for ONE machine]")
st, res = call("GET", "/api/oee/machines/%s/result?hours=8" % mid, tok)
check("machine result responds", st == 200 and (res or {}).get("ok") is True, st)
check("  and does NOT compute a previous period unless asked",
      "previous" not in (res or {}),
      "the Overview polls this too; a caller that does not need it should "
      "not pay for a second full calculation")

st, res2 = call("GET", "/api/oee/machines/%s/result?hours=8&compare=1" % mid, tok)
prev = (res2 or {}).get("previous") or {}
check("compare=1 returns the previous window", st == 200 and bool(prev),
      json.dumps(prev)[:110])
check("  it is the equal-length window immediately before",
      str(prev.get("to_utc") or "") == str(((res2 or {}).get("result") or {}).get("from_utc") or ""),
      "prev_to=%s current_from=%s"
      % (prev.get("to_utc"), ((res2 or {}).get("result") or {}).get("from_utc")))
check("  and carries a full result, not just a number",
      isinstance(prev.get("result"), dict) and "oee" in (prev.get("result") or {}),
      "every KPI card on the page needs its own previous value")

print()
print("[the machine says how complete its own figure is]")
r = (res2 or {}).get("result") or {}
check("the result carries a maturity stage", bool(r.get("stage")), r.get("stage"))
check("  and names the factors it could not measure",
      isinstance(r.get("missing_factors"), list), r.get("missing_factors"))
check("  a configured cycle time is reported back",
      float(r.get("ideal_cycle_time_s") or 0) == 12.0,
      "the Performance card says 'no cycle time configured' off this field")

print()
print("[the production chart has a target to draw]")
st, tr = call("GET", "/api/oee/trend?hours=8&buckets=4&machine_ids=%s" % mid, tok)
buckets = (tr or {}).get("buckets") or (tr or {}).get("rows") or []
check("trend responds for one machine", st == 200 and bool(buckets), st)
first = buckets[0] if buckets else {}
check("  buckets carry the counts the chart plots",
      all(k in first for k in ("total", "good", "reject")), sorted(first)[:6])
check("  and a target from the configured ideal cycle time",
      first.get("target_count") is not None,
      "target=%s (planned seconds / 12 s per piece)" % first.get("target_count"))

st, m2 = call("POST", "/api/oee/config/machines", tok,
              {"name": "No Cycle Time", "line": "Line 1", "enabled": 1})
mid2 = ((m2 or {}).get("item") or {}).get("id") or ""
st, tr2 = call("GET", "/api/oee/trend?hours=8&buckets=2&machine_ids=%s" % mid2, tok)
b2 = ((tr2 or {}).get("buckets") or [{}])[0]
check("  and NO target when no cycle time is configured",
      b2.get("target_count") is None,
      "a dashed line at zero would read as 'target: make nothing'")

print()
print("[editing one downtime event does not erase the rest of it]")
st, reason = call("POST", "/api/oee/config/downtime_reasons", tok,
                  {"reason": "Waiting for material", "category": "Waiting",
                   "enabled": 1})
rid = ((reason or {}).get("item") or {}).get("id") or ""
check("a downtime reason can be configured", st == 200 and bool(rid), st)

call("POST", "/api/oee/operator/state", tok,
     {"machine_id": mid, "state": "stopped", "comment": "test stop"})
st, evs = call("GET", "/api/oee/machines/%s/events?hours=8" % mid, tok)
events = (evs or {}).get("events") or []
eid = str((events[0] or {}).get("id") or "") if events else ""
check("a stop was recorded", bool(eid), "%d event(s)" % len(events))

if eid:
    st, one = call("POST", "/api/oee/operator/downtime", tok,
                   {"event_id": eid, "downtime_reason_id": rid,
                    "downtime_category": "Waiting", "comment": "waiting for pallet"})
    ev = (one or {}).get("event") or {}
    check("the stop can be classified",
          st == 200 and str(ev.get("downtime_reason_id")) == rid, st)

    # THE regression this test exists for.
    st, two = call("POST", "/api/oee/operator/downtime", tok,
                   {"event_id": eid, "comment": "second comment only"})
    ev2 = (two or {}).get("event") or {}
    check("  adding only a comment KEEPS the reason",
          str(ev2.get("downtime_reason_id") or "") == rid,
          "reason after a comment-only edit: %s - it used to write all four "
          "columns every time, silently discarding the classification"
          % ev2.get("downtime_reason_id"))
    check("  and keeps the category",
          str(ev2.get("downtime_category") or "") == "Waiting",
          ev2.get("downtime_category"))
    check("  and the comment did change",
          str(ev2.get("operator_comment") or "") == "second comment only",
          ev2.get("operator_comment"))

    st, three = call("POST", "/api/oee/operator/downtime", tok,
                     {"event_id": eid, "is_planned": True})
    ev3 = (three or {}).get("event") or {}
    check("  marking it planned KEEPS the reason and the comment",
          int(ev3.get("is_planned") or 0) == 1
          and str(ev3.get("downtime_reason_id") or "") == rid
          and str(ev3.get("operator_comment") or "") == "second comment only",
          "planned=%s reason=%s comment=%s"
          % (ev3.get("is_planned"), ev3.get("downtime_reason_id"),
             ev3.get("operator_comment")))
    check("  and records who confirmed it",
          bool(ev3.get("confirmed_by")) and bool(ev3.get("confirmed_utc")),
          "who looked at it and when IS the confirmation")

    st, four = call("POST", "/api/oee/operator/downtime", tok,
                    {"event_id": eid, "downtime_reason_id": ""})
    ev4 = (four or {}).get("event") or {}
    check("  an explicit empty string still CLEARS the reason",
          not ev4.get("downtime_reason_id"),
          "absent means leave alone; empty means clear - the page needs both")

print()
print("[the page reads the service; it does not recompute OEE]")
import io as _io  # noqa: E402


def src(name):
    return _io.open(os.path.join(ROOT, "frontend", "src", "components", "OEE", name),
                    encoding="utf-8", errors="replace").read()


page = src("OeeMachineDetail.jsx")
parts = src("OeeMachineDetailParts.jsx")
check("the page asks the service for the comparison",
      "compare: 1" in page, "rather than fetching two windows and subtracting")
check("  and for the machine's own Pareto grouping",
      "group_by: paretoGroup" in page and "metric: paretoMetric" in page)
# Multiplying a factor by 100 is a unit conversion; multiplying two factors
# TOGETHER is the OEE formula, and that belongs in calc.py once.
FACTORS = ("availability", "performance", "quality")


def combines_factors(text):
    """Lines where two different factors sit on either side of a `*`."""
    bad = []
    for line in text.splitlines():
        low = line.lower()
        if "*" not in low:
            continue
        left, _, right = low.partition("*")
        if any(f in left for f in FACTORS) and any(f in right for f in FACTORS):
            bad.append(line.strip())
    return bad


_hits = combines_factors(page)
check("no OEE formula lives in the page", not _hits,
      _hits[0] if _hits
      else "Availability x Performance x Quality is calculated once, server-side")
check("the shift handed over is resolved as a shift",
      'oeeList("shifts")' in page,
      "without the shift rows resolveWindow widens to the whole day, and the "
      "page would claim to show Shift 2 while showing 24 hours")
check("quick actions are gated on permission",
      "canEdit" in parts and "Read-only access" in parts,
      "and say why they are absent rather than showing dead buttons")
check("manual actions also respect the machine's configuration",
      "manual_enabled" in parts,
      "manual count on a machine with manual input switched off is a 400 "
      "waiting to happen")
check("each edit sends only what it changes",
      "is_planned: !planned" in parts and "downtime_reason_id: reasonId" in parts,
      "so the tri-state endpoint above is actually used as one")
check("unclassified stops are marked, not just listed",
      "Needs a reason" in parts and "oee-dt-unknown" in parts)
check("a missing count shows a setup state, not a zero",
      "No production counted in this period" in parts,
      "0 pieces and 'no counter configured' are different facts")
check("the target line is drawn only when configured",
      "hasTarget" in parts,
      "a dashed line at zero would read as 'target: make nothing'")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
