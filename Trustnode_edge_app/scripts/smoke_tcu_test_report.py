# -*- coding: utf-8 -*-
"""End-to-end: a TCU functional test report, rendered to a real PDF.

2026-09-30. The operator described a P100M TCU functional test and showed the
layout they need: pre/post checks, headline KPIs (max flow, dead-head
pressure, max temperature, peak heating and cooling RATE), asset identity and
technical specification tables, several charts - some over the whole test,
some over one phase - a telemetry table, a per-cycle results table whose
average IS the final result, and an alarm log.

This seeds that test into an ISOLATED workspace, builds the template through
the API exactly as the designer would save it, renders it, and leaves the PDF
on disk to look at.

It never touches the live install: it starts its own backend on a scratch
TRUSTNODE_DATA_DIR and a spare port.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = os.environ.get("TCU_SMOKE_PORT", "8196")
API = "http://127.0.0.1:" + PORT
FAILS = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


GW = "gw-tcu"
GW_NAME = "TCU Test Rig"
tmp = tempfile.mkdtemp(prefix="tn-tcu-")
store = os.path.join(tmp, "s.db")

# --------------------------------------------------------------------------- #
# Seed a functional test: an initial assessment, then a 3-cycle final test,
# each running the phases the operator listed.
# --------------------------------------------------------------------------- #
PHASES = ["Enabling", "Deadhead", "Heating", "Holding", "Cooling", "Holding"]
BASE = (datetime.utcnow() - timedelta(hours=3)).replace(microsecond=0)
rows = []
t = BASE
STEP = timedelta(seconds=5)


def emit(ts, tag, value=None, text=None):
    rows.append((ts.strftime("%Y-%m-%d %H:%M:%S"), tag, value, text))


def run_phases(test_name, cycle, temp0):
    """One pass of the recipe. Returns the temperature it ended on."""
    global t
    temp = temp0
    for phase in PHASES:
        emit(t, "Test_Step", text=test_name)
        emit(t, "Cycle_Number", value=float(cycle))
        emit(t, "Phase_Name", text=phase)
        for i in range(12):          # 12 samples x 5s = one minute per phase
            if phase == "Heating":
                temp += 0.85         # about +10.2 C/min
            elif phase == "Cooling":
                temp -= 0.90         # about -10.8 C/min
            elif phase == "Deadhead":
                temp += 0.05
            flow = 0.0 if phase in ("Enabling", "Deadhead") else 92.0 + (i % 5) * 0.7
            press = 6.5 if phase == "Deadhead" else 2.4 + (i % 3) * 0.1
            emit(t, "TCU_Temp", value=round(temp, 2))
            emit(t, "TCU_Flow", value=round(flow, 2))
            emit(t, "TCU_Pressure", value=round(press, 2))
            emit(t, "Ref_Temp", value=round(temp - 0.4, 2))
            t += STEP
    return temp


run_phases("initial", 1, 20.0)
for _c in (1, 2, 3):
    run_phases("final", _c, 20.0)
END = t

conn = sqlite3.connect(store)
conn.execute(
    "CREATE TABLE IF NOT EXISTS historian_readings ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " tenant_id TEXT NOT NULL DEFAULT 'default', ts_utc TEXT NOT NULL,"
    " gateway_id TEXT NULL, gateway_name TEXT NULL, device_name TEXT NULL,"
    " plc_ip TEXT NULL, database_name TEXT NULL, tag_name TEXT NOT NULL,"
    " value REAL NULL, value_text TEXT NULL, data_type TEXT NULL,"
    " quality INTEGER NULL, quality_label TEXT NULL, source TEXT NULL,"
    " created_utc TEXT NOT NULL)")
conn.executemany(
    "INSERT INTO historian_readings (tenant_id, ts_utc, gateway_id, gateway_name,"
    " device_name, tag_name, value, value_text, quality, quality_label, source,"
    " created_utc) VALUES ('default',?,?,?,?,?,?,?,192,'GOOD','tcu-smoke',?)",
    [(ts, GW, GW_NAME, "P100M", tag, val, txt, ts) for (ts, tag, val, txt) in rows])
conn.commit()
conn.close()

print("TrustNode - TCU functional test report")
print()
print("[a seeded functional test]")
print("  %d readings, %s -> %s (initial + 3 final cycles x %d phases)"
      % (len(rows), BASE.strftime("%H:%M:%S"), END.strftime("%H:%M:%S"), len(PHASES)))

env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=store, TRUSTNODE_BOOT_INTEGRITY_CHECK="never",
           TRUSTNODE_PORT=PORT)
# Refuse to run against a stranger. An orphaned backend from an earlier run
# kept answering on this port with ITS code, so every API check here was
# silently testing a stale build while the direct render used the current one.
# Same trap as the 2026-08-22 port-8000 squatting incident.
import socket as _socket                                  # noqa: E402
_probe = _socket.socket()
_probe.settimeout(1.5)
_busy = _probe.connect_ex(("127.0.0.1", int(PORT))) == 0
_probe.close()
if _busy:
    print("  FAIL: something is already listening on port %s." % PORT)
    print("        Stop it first - this test must talk to the backend IT starts,")
    print("        not to whatever happens to answer.")
    sys.exit(2)

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


def call(method, path, tok=None, body=None):
    headers = {"Content-Type": "application/json"}
    if tok:
        headers["Authorization"] = "Bearer " + tok
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        return exc.code, (exc.read().decode()[:200] if exc.fp else None)
    except Exception as exc:
        return 0, str(exc)[:160]


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
check("signed in", bool(tok))

# Keep the PDF inside the scratch workspace. _resolve_reports_dir() honours
# the CONFIGURED reports directory before TRUSTNODE_DATA_DIR, so an isolated
# run still writes into the operator's Documents folder unless it says
# otherwise - this smoke test left a stray PDF there on its first run.
_reports_dir = os.path.join(tmp, "reports")
os.makedirs(_reports_dir, exist_ok=True)
_st, _ = call("POST", "/api/directories", tok, {"overrides": {"reports": _reports_dir}})
check("the report directory points at the scratch workspace", _st == 200, _st)

WINDOW = {"preset": "custom",
          "from_utc": BASE.strftime("%Y-%m-%d %H:%M:%S"),
          "to_utc": END.strftime("%Y-%m-%d %H:%M:%S")}
INCH = '1 1/2" female'


def phase_trigger(phase):
    return {"start": {"gateway_id": GW, "tag_name": "Phase_Name",
                      "operator": "==", "value": phase}}


def series(tag, label, color):
    return {"id": tag, "label": label, "gateway_id": GW, "tag_name": tag,
            "color": color}


MEAS = [("TCU_Temp", "Temperature", "#ef5350"),
        ("TCU_Flow", "Flow", "#42a5f5"),
        ("TCU_Pressure", "Pressure", "#66bb6a")]

definition = {"sections": [
    {"type": "header", "title": "Testing Report",
     "subtitle": "P100M TCU - functional test", "show_generated_at": True},

    {"type": "static_table", "title": "Pre / post test checks",
     "header": ["Check", "Result"],
     "rows": [["Electrics Checked", "PASS"],
              ["Solenoid Valves & Coils Checked", "PASS"],
              ["Physical Condition of Unit Checked", "PASS"],
              ["Heating & Cooling Function Passed & Checked", "PASS"],
              ["Operation of Tank Filling Checked", "PASS"],
              ["Safety Thermostat Set Correctly & Checked", "PASS"]]},

    {"type": "kpi_grid", "title": "Headline results", "columns": 5,
     "time_range": dict(WINDOW),
     "items": [
         {"label": "Max flow rate (L/min)", "gateway_id": GW,
          "tag_name": "TCU_Flow", "aggregation": "max"},
         {"label": "Dead head pressure (bar)", "gateway_id": GW,
          "tag_name": "TCU_Pressure", "aggregation": "max"},
         {"label": "Max temperature (C)", "gateway_id": GW,
          "tag_name": "TCU_Temp", "aggregation": "max"},
         {"label": "Peak heating rate (C/min)", "gateway_id": GW,
          "tag_name": "TCU_Temp", "aggregation": "rate_max"},
         {"label": "Peak cooling rate (C/min)", "gateway_id": GW,
          "tag_name": "TCU_Temp", "aggregation": "rate_min"},
     ]},

    {"type": "static_table", "title": "Asset identity", "align_columns": ["left", "left"],
     "header": ["Field", "Value"],
     "rows": [["Asset ID", "B-001072"], ["JJVC Product Number", "10065258"],
              ["Serial Number", "116221580023"], ["Product Type", "P100M TCU"],
              ["Classification", "*CTQ* 4GT Regloplas P100M TCU"],
              ["Vistakon Unit No", "1162-2158"],
              ["Specification", "P100M/8/SG85/3K/RT100"]]},

    {"type": "static_table", "title": "Technical specification",
     "header": ["Field", "Value"],
     "rows": [["Heat Transfer Fluid", "Water"], ["Cooling Type", "Indirect"],
              ["Outlet Temp Max", "100 C"], ["Safety Thermostat Setting", "80 C"],
              ["Pump Capacity / Type", "SG85"], ["Controller", "RT100"],
              ["Operating Voltage", "400 V/50 Hz/3 PE"],
              ["Connections (Outlet/Inlet)", INCH],
              ["Dimensions W/H/D", "300/708/1132 mm"],
              ["Flow Meter Type", "Grundfos (Vortex)"],
              ["Delivery Head Max", "60 m"],
              ["Rated heating capacity", "8 kW"],
              ["Rated cooling capacity", "120.0 kW at 100C outlet / 20C CW"]]},

    {"type": "line_chart", "title": "Temperature / flow / pressure - combined",
     "time_range": dict(WINDOW),
     "series": [series(tag, lbl, col) for tag, lbl, col in MEAS]},

    {"type": "area_chart", "title": "Temperature profile", "time_range": dict(WINDOW),
     "series": [series("TCU_Temp", "TCU", "#ef5350"),
                series("Ref_Temp", "Reference", "#ffa726")]},
    {"type": "line_chart", "title": "Flow rate profile", "time_range": dict(WINDOW),
     "series": [series("TCU_Flow", "Flow", "#42a5f5")]},
    {"type": "line_chart", "title": "Pressure profile", "time_range": dict(WINDOW),
     "series": [series("TCU_Pressure", "Pressure", "#66bb6a")]},

    # Two charts on one line, each over its own phase.
    {"type": "row", "columns": [
        {"type": "line_chart", "title": "Heating phase detail",
         "time_range": dict(WINDOW), "trigger": phase_trigger("Heating"),
         "x_axis_label": "Time", "y_axis_label": "Temperature", "y_axis_unit": "C",
         "series": [series("TCU_Temp", "Temperature", "#ef5350")]},
        {"type": "bar_chart", "title": "Cooling phase detail",
         "time_range": dict(WINDOW), "trigger": phase_trigger("Cooling"),
         "x_axis_label": "Time", "y_axis_label": "Temperature", "y_axis_unit": "C",
         "series": [series("TCU_Temp", "Temperature", "#42a5f5")]},
    ]},

    {"type": "table", "title": "Telemetry", "time_range": dict(WINDOW),
     "row_limit": 40,
     "series": [series(tag, lbl, col) for tag, lbl, col in MEAS],
     "columns": [{"key": "ts", "title": "Time"},
                 {"key": "value", "series_id": "TCU_Temp", "title": "Temp (C)"},
                 {"key": "value", "series_id": "TCU_Flow", "title": "Flow (L/min)"},
                 {"key": "value", "series_id": "TCU_Pressure", "title": "Pressure (bar)"}]},

    {"type": "segment_summary", "title": "Final test - result per cycle",
     "stream": "cycle", "segment_header": "Cycle", "gateway_id": GW,
     "columns": [{"tag_name": "TCU_Temp", "aggregation": "max", "title": "Max temp (C)"},
                 {"tag_name": "TCU_Flow", "aggregation": "avg", "title": "Avg flow (L/min)"},
                 {"tag_name": "TCU_Temp", "aggregation": "rate_max",
                  "title": "Heating (C/min)"}],
     "footer_aggregation": "avg", "footer_label": "Final result", "format": "2dp",
     "align_columns": ["left", "right", "right", "right"]},

    {"type": "segment_summary", "title": "Results by phase",
     "stream": "phase", "segment_header": "Phase", "gateway_id": GW,
     "columns": [{"tag_name": "TCU_Temp", "aggregation": "avg", "title": "Avg temp (C)"},
                 {"tag_name": "TCU_Flow", "aggregation": "avg", "title": "Avg flow (L/min)"},
                 {"tag_name": "TCU_Pressure", "aggregation": "max",
                  "title": "Max press (bar)"}],
     "format": "2dp"},

    {"type": "static_table", "title": "Alarm log",
     "header": ["Time", "Tag", "Message", "Severity"],
     "rows": [["-", "-", "No alarms recorded during this test", "-"]]},
]}

print()
print("[the template, saved through the API as the designer would]")
status, made = call("POST", "/api/reports/templates", tok,
                    {"name": "Testing Report", "description": "TCU functional test",
                     "definition": definition})
tpl = (made or {}).get("template") or {}
tpl_id = tpl.get("id")
check("the template saved", status in (200, 201) and bool(tpl_id), tpl_id or made)
saved_sections = (tpl.get("definition") or {}).get("sections") or []
check("  every section survived the save",
      len(saved_sections) == len(definition["sections"]),
      "%d of %d" % (len(saved_sections), len(definition["sections"])))
_row_saved = [x for x in saved_sections if isinstance(x, dict) and x.get("type") == "row"]
print("  saved row section:", json.dumps(_row_saved[0], default=str)[:400] if _row_saved else "NO ROW SECTION")
def _with_triggers(secs):
    n = 0
    for sec in secs:
        if not isinstance(sec, dict):
            continue
        if sec.get("trigger"):
            n += 1
        if sec.get("type") == "row":
            n += _with_triggers(sec.get("columns") or [])
    return n


check("  the per-section triggers survived",
      _with_triggers(saved_sections) == 2,
      "the heating and cooling charts each carry one, inside the row")

print()
print("[the segments the run produced]")
# A cycle-triggered run hands its segments to the renderer. The generate
# endpoint has no run behind it, so derive them from the seeded tags the same
# way the scheduler would - that is what fills the per-cycle and per-phase
# tables. Without this they correctly render "no test segments".
def _derive(tag, stream):
    seen, out, cur = [], [], None
    for ts, name, val, txt in rows:
        if name != tag:
            continue
        v = txt if txt is not None else ("%g" % val if val is not None else "")
        if cur is not None and cur["value"] == v:
            continue
        if cur is not None:
            cur["to_utc"] = ts
        seen.append(v)
        cur = {"stream": stream, "name": stream, "label": v, "value": v,
               "index": seen.count(v), "from_utc": ts, "to_utc": ""}
        out.append(cur)
    if cur is not None and not cur["to_utc"]:
        cur["to_utc"] = END.strftime("%Y-%m-%d %H:%M:%S")
    return out


SEGMENTS = (_derive("Test_Step", "test") + _derive("Cycle_Number", "cycle")
            + _derive("Phase_Name", "phase"))
check("segments were derived from the run",
      len([g for g in SEGMENTS if g["stream"] == "phase"]) >= 20,
      "test=%d cycle=%d phase=%d"
      % (len([g for g in SEGMENTS if g["stream"] == "test"]),
         len([g for g in SEGMENTS if g["stream"] == "cycle"]),
         len([g for g in SEGMENTS if g["stream"] == "phase"])))
with open(os.path.join(tmp, "segments.json"), "w", encoding="utf-8") as fh:
    json.dump(SEGMENTS, fh)
with open(os.path.join(tmp, "definition.json"), "w", encoding="utf-8") as fh:
    json.dump({"name": "Testing Report", "definition": definition}, fh)

print()
print("[the report itself]")
status, gen = call("POST", "/api/reports/templates/%s/generate" % tpl_id, tok)
record = (gen or {}).get("generated") or {} if isinstance(gen, dict) else {}
if not isinstance(gen, dict):
    print("  generate error:", str(gen)[:300])
path = str(record.get("file_path") or "")
check("a PDF was produced", status == 200 and bool(path), gen if status != 200 else path)
head = b""
if path and os.path.exists(path):
    with open(path, "rb") as fh:
        head = fh.read(5)
check("  it is a real PDF", head == b"%PDF-", head or "no file on disk")
size = os.path.getsize(path) if path and os.path.exists(path) else 0
check("  and it carries the charts and tables", size > 20000, "%d bytes" % size)

sample = ""
if head == b"%PDF-":
    sample = os.path.join(ROOT, "scripts", "validation_out",
                          "TCU_Testing_Report_sample.pdf")
    os.makedirs(os.path.dirname(sample), exist_ok=True)
    shutil.copyfile(path, sample)

# Render again WITH the segments, exactly as a cycle-triggered run does, so
# the sample shows the per-cycle and per-phase tables filled in.
print()
print("[the same report as a cycle-triggered run]")
proc2 = subprocess.run(
    [sys.executable, os.path.join(ROOT, "scripts", "_render_with_segments.py"),
     os.path.join(tmp, "definition.json"), os.path.join(tmp, "segments.json"),
     BASE.strftime("%Y-%m-%d %H:%M:%S"), END.strftime("%Y-%m-%d %H:%M:%S")],
    cwd=os.path.join(ROOT, "backend"), env=env, capture_output=True,
    text=True, timeout=300)
cyc_path = (proc2.stdout or "").strip().splitlines()[-1] if proc2.stdout.strip() else ""
check("a cycle-style report rendered", bool(cyc_path) and os.path.exists(cyc_path),
      (proc2.stderr or "")[-160:] or cyc_path)
if cyc_path and os.path.exists(cyc_path):
    sample = os.path.join(ROOT, "scripts", "validation_out",
                          "TCU_Testing_Report_sample.pdf")
    shutil.copyfile(cyc_path, sample)

# Page images, so the layout can be eyeballed without opening a PDF.
shots = []
if sample and os.path.exists(sample):
    try:
        import pymupdf
        doc = pymupdf.open(sample)
        outdir = os.path.join(ROOT, "scripts", "validation_out")
        for i, page in enumerate(doc, 1):
            img = os.path.join(outdir, "TCU_Testing_Report_p%d.png" % i)
            page.get_pixmap(dpi=110).save(img)
            shots.append(img)
        doc.close()
    except Exception as exc:
        print("  (preview images unavailable: %s)" % str(exc)[:80])
check("preview images were produced", len(shots) >= 3, "%d page(s)" % len(shots))

print()
print("SAMPLE: %s" % (sample or "(not produced)"))
for img in shots:
    print("PREVIEW: %s" % img)
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
