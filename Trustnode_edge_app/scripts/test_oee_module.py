# -*- coding: utf-8 -*-
"""The OEE module, end to end through the real app.

Boots the whole backend on a throwaway workspace and exercises the module the
way the UI does: create a machine, map an existing gateway tag, add power
rules, record states and counts, then check the OEE arithmetic that comes back.

Also asserts the architectural rules that matter:
  * OEE creates only oee_* tables and does not duplicate gateways/devices/tags;
  * a mapping pointing at a tag no gateway collects is REFUSED at save time -
    that is the difference between a machine that reports "unknown" for a week
    and one that tells you why on the day you configure it;
  * manual endpoints refuse machines that have manual input switched off.
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
sys.path.insert(0, os.path.join(ROOT, "backend"))
PORT = "8096"
API = "http://127.0.0.1:" + PORT
FAILS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:150]) if detail else ""
    print("  {0:58s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


# ---------------------------------------------------------- pure unit checks
print("[the arithmetic, before any I/O]")
from app.modules.oee import calc  # noqa: E402
from app.modules.oee.state_engine import (  # noqa: E402
    SignalReading, evaluate_signal_state, evaluate_power_state, resolve_state)

r = calc.compute_oee(calc.OeeInputs(
    planned_time_s=7.5 * 3600, runtime_s=6 * 3600, total_count=1000,
    reject_count=40, ideal_cycle_time_s=18.0))
check("worked example: A=0.800 P=0.833 Q=0.960 OEE=0.640",
      round(r.availability, 3) == 0.8 and round(r.performance, 3) == 0.833
      and round(r.quality, 3) == 0.96 and round(r.oee, 3) == 0.64,
      "A={0:.3f} P={1:.3f} Q={2:.3f} OEE={3:.3f}".format(
          r.availability, r.performance, r.quality, r.oee))
check("  good count is derived when only rejects are counted",
      r.good_count == 960.0, r.good_count)

none_perf = calc.compute_oee(calc.OeeInputs(
    planned_time_s=3600, runtime_s=1800, total_count=10))
check("no ideal cycle time -> performance is None, not 0",
      none_perf.performance is None and none_perf.oee is None)
no_prod = calc.compute_oee(calc.OeeInputs(
    planned_time_s=3600, runtime_s=1800, total_count=0, ideal_cycle_time_s=10))
check("no production -> quality is None, not 0", no_prod.quality is None)
over = calc.compute_oee(calc.OeeInputs(
    planned_time_s=3600, runtime_s=3600, total_count=1000,
    ideal_cycle_time_s=60, good_count=1000))
check("a wrong ideal cycle time cannot produce OEE > 100%",
      over.oee is not None and over.oee <= 1.0, over.oee)

# --- energy ---------------------------------------------------------------
kwh = calc.integrate_energy([(0, 10.0), (3600, 10.0)], 0, 3600)
check("10 kW for one hour is 10 kWh", abs(kwh - 10.0) < 1e-6, kwh)
waste = calc.estimate_wasted_energy(
    by_state={"stopped": 4.0}, state_seconds={"stopped": 3600.0},
    standby_power_kw=0.5, idle_power_kw=1.0)
check("waste counts only the excess over standby (4kWh - 0.5kW*1h = 3.5)",
      abs(waste["total_kwh"] - 3.5) < 1e-6, waste["total_kwh"])

# --- state engine ---------------------------------------------------------
maps = [{"enabled": True, "oee_function": "running_status", "tag_name": "Run",
         "condition_op": "truthy", "priority": 50},
        {"enabled": True, "oee_function": "fault_status", "tag_name": "Fault",
         "condition_op": "truthy", "priority": 10}]
rules = [{"id": "r1", "enabled": True, "measurement": "power_kw",
          "min_value": 3.0, "generated_status": "production", "priority": 10,
          "name": "Producing"}]
machine = {"default_status_source": "combined", "standby_power_kw": 0.5}


def verdict(readings, kw, counts=None):
    s = evaluate_signal_state(maps, readings)
    p = evaluate_power_state(rules, kw)
    return resolve_state(machine, s, p, power_kw=kw, counts_increasing=counts)


v = verdict({"Run": SignalReading(True), "Fault": SignalReading(False)}, 8.0)
check("signal + power agree -> high confidence",
      v.state == "running" and v.confidence == "high", v.to_dict())
v = verdict({"Run": SignalReading(False), "Fault": SignalReading(False)}, 8.0)
check("PLC stopped but power high -> conflict + energy waste",
      v.confidence == "conflict" and "energy_waste" in v.flags, v.to_dict())
v = verdict({"Run": SignalReading(True), "Fault": SignalReading(False)}, 8.0, False)
check("running but count not increasing -> blocked",
      "blocked" in v.flags, v.to_dict())
v = verdict({"Run": SignalReading(True), "Fault": SignalReading(True)}, 8.0)
check("fault outranks running", v.state == "faulted", v.state)
v = verdict({}, 8.0)
check("no signal data -> power fallback, low confidence",
      v.source == "power" and v.confidence == "low", v.to_dict())
v = resolve_state(machine, None, None)
check("no data at all -> unknown / missing, never 'stopped'",
      v.state == "unknown" and v.confidence == "missing", v.to_dict())
v = evaluate_signal_state(maps, {"Run": SignalReading(stale=True),
                                 "Fault": SignalReading(stale=True)})
check("stale tags produce NO verdict (a dead gateway is not 'stopped')",
      v is None)

# ------------------------------------------------------------- end to end
print()
print("[end to end through the real app]")
tmp = tempfile.mkdtemp(prefix="tn-oee-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
proc = subprocess.Popen([sys.executable, "-m", "app"],
                        cwd=os.path.join(ROOT, "backend"), env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(60):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read()
        break
    except Exception:
        time.sleep(2)


def call(method, path, token=None, body=None, timeout=60):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "null")
        except Exception:
            return e.code, None
    except Exception as e:
        return 0, str(e)[:200]


def finish(code):
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    return code


st, b = call("POST", "/api/auth/login", body={"username": "admin",
                                              "password": "admin"})
tok = (b or {}).get("token") if isinstance(b, dict) else None
check("admin login", st == 200 and bool(tok))
if not tok:
    sys.exit(finish(2))

# --- schema ---------------------------------------------------------------
st, h = call("GET", "/api/oee/health", tok)
tables = (h or {}).get("tables") or {}
check("all 16 OEE tables exist", st == 200 and len(tables) == 16, len(tables))
check("  and every one is readable",
      all(v >= 0 for v in tables.values()),
      [k for k, v in tables.items() if v < 0])
check("  the module creates ONLY oee_ tables",
      all(k.startswith("oee_") for k in tables), list(tables)[:3])

# --- defaults seeded ------------------------------------------------------
st, d = call("GET", "/api/oee/config/downtime_reasons", tok)
check("default downtime reasons seeded", st == 200 and d["count"] >= 15, d.get("count"))
st, q = call("GET", "/api/oee/config/quality_reasons", tok)
check("default quality reasons seeded", st == 200 and q["count"] >= 10, q.get("count"))
st, meta = call("GET", "/api/oee/meta", tok)
check("meta lists the machine states and OEE functions",
      st == 200 and len(meta["states"]) == 10 and len(meta["oee_functions"]) == 15,
      "{0} states / {1} functions".format(len(meta.get("states") or []),
                                          len(meta.get("oee_functions") or [])))

# --- a gateway to reference ----------------------------------------------
st, _ = call("PUT", "/api/app-store/domain", tok, {
    "domain": "gateway_configurations",
    "payload": [{"id": "gw-oee-test", "name": "Line 1 PLC",
                 "gateway_type": "allen_bradley", "plc_ip": "10.0.0.5",
                 "tags": ["MotorRunning", "Fault", "TotalCount", "Power_kW"]}],
    "actor": "test_oee_module"})
check("a gateway exists to reference", st == 200, st)

# --- machine --------------------------------------------------------------
st, m = call("POST", "/api/oee/config/machines", tok, {
    "name": "Filler 1", "machine_code": "FIL-01", "line": "Line 1",
    "area": "Packaging", "oee_enabled": True, "signal_enabled": True,
    "power_enabled": True, "manual_enabled": True,
    "default_status_source": "combined", "ideal_cycle_time_s": 18.0,
    "standby_power_kw": 0.5, "idle_power_kw": 1.5, "enabled": True})
machine_id = ((m or {}).get("item") or {}).get("id")
check("a machine can be created", st == 200 and bool(machine_id), machine_id)
st, pr = call("GET", "/api/oee/config/power_state_rules", tok)
check("  enabling power monitoring seeds starter power rules",
      st == 200 and pr["count"] >= 3, pr.get("count"))

# --- reference validation -------------------------------------------------
st, bad = call("POST", "/api/oee/config/signal_mappings", tok, {
    "machine_id": machine_id, "enabled": True, "source_type": "plc",
    "gateway_id": "gw-oee-test", "tag_name": "NoSuchTag",
    "oee_function": "running_status", "condition_op": "truthy"})
check("a mapping to an uncollected tag is REFUSED", st == 400,
      "status={0} {1}".format(st, (bad or {}).get("detail", "")[:70]))
st, bad2 = call("POST", "/api/oee/config/signal_mappings", tok, {
    "machine_id": machine_id, "gateway_id": "gw-does-not-exist",
    "tag_name": "MotorRunning", "oee_function": "running_status"})
check("  and so is one pointing at a gateway that does not exist", st == 400,
      "status={0}".format(st))

st, ok1 = call("POST", "/api/oee/config/signal_mappings", tok, {
    "machine_id": machine_id, "enabled": True, "source_type": "plc",
    "gateway_id": "gw-oee-test", "tag_name": "MotorRunning",
    "oee_function": "running_status", "condition_op": "truthy", "priority": 50})
check("a mapping to a REAL collected tag saves", st == 200,
      ((ok1 or {}).get("item") or {}).get("id"))
call("POST", "/api/oee/config/signal_mappings", tok, {
    "machine_id": machine_id, "enabled": True, "source_type": "plc",
    "gateway_id": "gw-oee-test", "tag_name": "TotalCount",
    "oee_function": "total_count", "condition_op": "truthy"})
st, pm = call("POST", "/api/oee/config/power_meter_mappings", tok, {
    "machine_id": machine_id, "enabled": True, "gateway_id": "gw-oee-test",
    "power_tag": "Power_kW"})
check("a power meter can be mapped to the machine", st == 200,
      ((pm or {}).get("item") or {}).get("id"))

# --- operator -------------------------------------------------------------
st, cyc = call("POST", "/api/oee/operator/cycle/start", tok,
               {"machine_id": machine_id, "source": "manual"})
check("the operator can start a cycle", st == 200,
      ((cyc or {}).get("cycle") or {}).get("id"))
st, cnt = call("POST", "/api/oee/operator/count", tok, {
    "machine_id": machine_id, "total_count": 1000, "reject_count": 40,
    "source": "manual"})
check("  and record production counts", st == 200)
st, neg = call("POST", "/api/oee/operator/count", tok, {
    "machine_id": machine_id, "total_count": -5, "source": "manual"})
check("  negative counts are refused", neg is not None and st == 400, st)
st, stp = call("POST", "/api/oee/operator/cycle/stop", tok,
               {"machine_id": machine_id, "result": "good"})
check("  and stop the cycle", st == 200,
      ((stp or {}).get("cycle") or {}).get("duration_s"))

st, ev = call("POST", "/api/oee/operator/state", tok, {
    "machine_id": machine_id, "state": "stopped", "comment": "test stop"})
event_id = ((ev or {}).get("event") or {}).get("id")
check("the operator can set the machine state", st == 200 and bool(event_id))
st, bads = call("POST", "/api/oee/operator/state", tok, {
    "machine_id": machine_id, "state": "not_a_state"})
check("  an unknown state is refused", st == 400, st)

reasons = (d or {}).get("items") or []
reason_id = reasons[0]["id"] if reasons else ""
st, conf = call("POST", "/api/oee/operator/downtime", tok, {
    "event_id": event_id, "downtime_reason_id": reason_id,
    "downtime_category": reasons[0]["category"] if reasons else "Unknown",
    "comment": "confirmed by test"})
check("a downtime reason can be confirmed", st == 200,
      ((conf or {}).get("event") or {}).get("downtime_reason_id"))

# --- manual gate ----------------------------------------------------------
call("POST", "/api/oee/config/machines", tok,
     {"id": machine_id, "name": "Filler 1", "manual_enabled": False})
st, blocked = call("POST", "/api/oee/operator/count", tok, {
    "machine_id": machine_id, "total_count": 5, "source": "manual"})
check("manual input off -> manual endpoints refuse", st == 400,
      (blocked or {}).get("detail", "")[:60])
call("POST", "/api/oee/config/machines", tok,
     {"id": machine_id, "name": "Filler 1", "manual_enabled": True})

# --- results --------------------------------------------------------------
st, res = call("GET", "/api/oee/machines/{0}/result?hours=24".format(machine_id), tok)
result = (res or {}).get("result") or {}
check("a machine result computes", st == 200 and "oee" in result,
      "counts total={0} reject={1}".format(result.get("total_count"),
                                           result.get("reject_count")))
check("  the recorded counts came through",
      result.get("total_count") == 1000 and result.get("reject_count") == 40,
      result.get("total_count"))
check("  quality = 960/1000 = 0.96",
      result.get("quality") is not None and abs(result["quality"] - 0.96) < 1e-6,
      result.get("quality"))

st, ov = call("GET", "/api/oee/overview?hours=24", tok)
check("the overview returns totals and machine cards",
      st == 200 and (ov or {}).get("ok") and len(ov.get("machines") or []) >= 1,
      "machines={0}".format(len(((ov or {}).get("machines") or []))))
card = (ov.get("machines") or [{}])[0]
check("  a machine card carries state, source and confidence",
      all(k in card for k in ("state", "status_source", "confidence")),
      "{0}/{1}/{2}".format(card.get("state"), card.get("status_source"),
                           card.get("confidence")))
check("  with no live tag data the card is honest about it",
      card.get("confidence") in ("missing", "low", "medium", "conflict"),
      card.get("confidence"))

st, tr = call("GET", "/api/oee/trend?hours=24&buckets=6", tok)
check("the trend returns the requested buckets",
      st == 200 and len(tr.get("buckets") or []) == 6,
      len((tr or {}).get("buckets") or []))

st, evs = call("GET", "/api/oee/machines/{0}/events?hours=24".format(machine_id), tok)
check("the state timeline is queryable",
      st == 200 and len(evs.get("events") or []) >= 1,
      len((evs or {}).get("events") or []))

# --- deletion -------------------------------------------------------------
st, dele = call("DELETE", "/api/oee/config/machines/{0}".format(machine_id), tok)
check("a machine can be deleted", st == 200 and dele.get("ok"), dele)

# ------------------------------------------------- wiring / architecture
print()
print("[the module is wired into the app, and duplicates nothing]")
SRC = os.path.join(ROOT, "frontend", "src")


def read(*parts):
    return io.open(os.path.join(*parts), encoding="utf-8", errors="replace").read()


app_jsx = read(SRC, "App.jsx")
css = read(SRC, "styles.css")
check("the OEE menu exists with its three pages",
      'id: "oee"' in app_jsx and '"OEE Overview"' in app_jsx
      and '"Operator Screen"' in app_jsx and '"OEE Configuration"' in app_jsx)
check("  the three pages render",
      all('activePage === "{0}"'.format(k) in app_jsx
          for k in ("oee_overview", "oee_operator", "oee_configuration")))
check("  and are licence-mapped to the oee module",
      'oee_overview: "oee"' in app_jsx)
check("configuration is handed the EXISTING gateways and devices",
      "gatewayConfigs={gatewayConfigsView}" in app_jsx
      and "devices={devicesView}" in app_jsx)
check("  so the tag picker can only offer real collected tags",
      "TagPicker" in read(SRC, "components", "OEE", "OeeConfiguration.jsx"))
check("the OEE styles reuse the app tokens, not their own theme",
      "var(--card)" in css and "var(--stroke)" in css and ".oee-machine-card" in css)

back = os.path.join(ROOT, "backend", "app")
schema_src = io.open(os.path.join(back, "modules", "oee", "schema.py"),
                     encoding="utf-8").read()
check("the schema creates no table outside the oee_ namespace",
      schema_src.count("CREATE TABLE IF NOT EXISTS") == 16
      and schema_src.count("CREATE TABLE IF NOT EXISTS oee_") == 16,
      schema_src.count("CREATE TABLE IF NOT EXISTS"))
# Look at the DDL, not the prose: the module docstring explains WHY there is
# no "REFERENCES gateway(id)", and matching that sentence is not a failure.
import re as _re
_ddl = schema_src.split('OEE_SCHEMA_SQL = """', 1)[-1].split('"""', 1)[0]
_ddl_no_comments = _re.sub(r"/\*.*?\*/", "", _ddl, flags=_re.S)
check("  and defines no foreign key into the collection system",
      "REFERENCES" not in _ddl_no_comments.upper(),
      [ln.strip() for ln in _ddl_no_comments.splitlines()
       if "REFERENCES" in ln.upper()][:2])
check("  with the reason written down where the next reader will look",
      "not a FOREIGN KEY" in schema_src or "soft reference" in schema_src.lower())

lite = io.open(os.path.join(back, "routers", "lite_local.py"),
               encoding="utf-8").read()
check("Lite exposes an oee capability flag", '"oee": _allowed("oee", "oee")' in lite)
pcat = io.open(os.path.join(back, "services", "permission_catalog.py"),
               encoding="utf-8").read()
check("permissions exist for viewing and for configuring",
      '"key": "oee"' in pcat and '"key": "oee_configuration"' in pcat)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
