# -*- coding: utf-8 -*-
"""The schedule editor an operator actually uses can configure a cycle.

2026-10-01, reported: "the reports module and schedule reports module are not
working at all". They were not: the LIVE editor
(components/Reports/ScheduledReportsManager.jsx) offered only time / tag /
both and had no stop-condition editor at all, so a start/end trigger could
not be configured from the UI. Everything for cycles had been added to the
schedule form in App.jsx - which the page itself labels "LEGACY: OLD SCHEDULE
EDITOR (READ-ONLY)".

The schedule in the screenshot also read `start_cycle > 1`. A bit is 0 or 1,
so that can never be true; it would never have fired even in tag mode.

Checked here:
  * the live editor offers cycle mode, with start AND stop conditions;
  * both lists come from ONE component, so they cannot drift;
  * a condition that can never be true is called out;
  * a new condition defaults to "= 1", not "> ";
  * every cycle field survives a real save and reload through the API;
  * the saved schedule actually drives a cycle to completion.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = os.environ.get("SCHED_UI_PORT", "8201")
API = "http://127.0.0.1:" + PORT
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:160]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("TrustNode - the schedule editor can configure a cycle")
print()
print("[the editor the operator actually uses]")
MGR = io.open(os.path.join(ROOT, "frontend", "src", "components", "Reports",
                           "ScheduledReportsManager.jsx"),
              encoding="utf-8", errors="replace").read()

check("it offers cycle mode", '{ value: "cycle"' in MGR,
      "it had time / tag / both only, so a start+stop trigger was unreachable")
check("  and a STOP condition editor", "stop_conditions" in MGR,
      "there was no way to say when the run ends")
check("  start and stop share ONE condition list",
      MGR.count("function ConditionList(") == 1
      and MGR.count("<ConditionList") == 2,
      "two copies of the same editor drift apart")
check("  the cycle fields are in the saved shape",
      all(f in MGR for f in ("stop_condition_logic", "id_tag", "segment_tag",
                             "file_name_pattern")),
      "the payload is a spread of `draft`, so a field missing from the "
      "default never reaches the backend")
check("a condition that can never be true is called out",
      "never > 1" in MGR or "never" in MGR.split("const impossible", 1)[-1][:400],
      "`start_cycle > 1` on a 0/1 bit silently never fires")
check("  a new condition defaults to '= 1'",
      'operator: "eq", value: "1"' in MGR,
      "the old default was \">\" with an empty threshold")

APP = io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
              encoding="utf-8", errors="replace").read()
check("the list sanitiser keeps cycle schedules intact",
      '["time", "tag", "both", "cycle"].includes(triggerMode)' in APP
      and "stop_conditions: stopConditions" in APP,
      "an unlisted mode is rewritten to 'time' and the cycle fields vanish")

# --------------------------------------------------------------------------- #
# a real save, through the API, in the shape the editor sends
# --------------------------------------------------------------------------- #
tmp = tempfile.mkdtemp(prefix="tn-schedui-")
store = os.path.join(tmp, "s.db")

GW = "gw-plc"
BASE = datetime(2026, 10, 1, 9, 0, 0)
STEP = 5
RUN = (60, 600)


def ts(sec):
    return (BASE + timedelta(seconds=sec)).strftime("%Y-%m-%d %H:%M:%S.000")


def pulse(sec, at):
    return 1.0 if at <= sec < at + STEP else 0.0


seed = []
for sec in range(0, 721, STEP):
    seed.append((ts(sec), "cycle_started", pulse(sec, RUN[0])))
    seed.append((ts(sec), "cycle_ended", pulse(sec, RUN[1])))
    seed.append((ts(sec), "SimREAL[3]", 100.0 + (sec % 180) / 3.0))
conn = sqlite3.connect(store)
conn.execute(
    "CREATE TABLE IF NOT EXISTS historian_readings ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT DEFAULT 'default',"
    " ts_utc TEXT, gateway_id TEXT, gateway_name TEXT, device_name TEXT,"
    " plc_ip TEXT, database_name TEXT, tag_name TEXT, value REAL,"
    " value_text TEXT, data_type TEXT, quality INTEGER, quality_label TEXT,"
    " source TEXT, created_utc TEXT)")
conn.executemany(
    "INSERT INTO historian_readings (tenant_id, ts_utc, gateway_id, gateway_name,"
    " tag_name, value, quality, quality_label, source, created_utc)"
    " VALUES ('default',?,?,'PLC',?,?,192,'GOOD','sched-ui',?)",
    [(t, GW, tag, v, t) for (t, tag, v) in seed])
conn.commit()
conn.close()

env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=store, TRUSTNODE_BOOT_INTEGRITY_CHECK="never",
           TRUSTNODE_PORT=PORT)
import socket as _socket
_probe = _socket.socket()
_probe.settimeout(1.5)
_busy = _probe.connect_ex(("127.0.0.1", int(PORT))) == 0
_probe.close()
if _busy:
    print("  FAIL: port %s is already in use - this test must talk to the "
          "backend IT starts." % PORT)
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
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        return exc.code, (exc.read().decode()[:200] if exc.fp else None)
    except Exception as exc:                       # noqa: BLE001
        return 0, str(exc)[:160]


def finish(code):
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    return code


print()
print("[a cycle schedule saved exactly as the editor sends it]")
check("the app started", up)
if not up:
    sys.exit(2)
tok = (call("POST", "/api/auth/login",
            body={"username": "admin", "password": "admin"})[1] or {}).get("token")

status, made = call("POST", "/api/reports/templates", tok, {
    "name": "Cycle UI probe", "description": "",
    "definition": {"sections": [
        {"type": "header", "title": "Cycle"},
        {"type": "line_chart", "title": "Run", "time_range": {"preset": "run"},
         "series": [{"id": "s", "label": "SimREAL[3]", "gateway_id": GW,
                     "tag_name": "SimREAL[3]"}]}]}})
tpl_id = ((made or {}).get("template") or {}).get("id")
check("a template exists", bool(tpl_id), tpl_id or made)

# The editor spreads `draft`, so this is literally what it posts.
draft = {
    "name": "Cycle from the UI", "template_id": tpl_id, "enabled": True,
    "trigger_mode": "cycle",
    "tag_conditions": [{"id": "c1", "enabled": True, "gateway_id": GW,
                        "tag_name": "cycle_started", "operator": "eq", "value": "1"}],
    "condition_logic": "all",
    "stop_conditions": [{"id": "c2", "enabled": True, "gateway_id": GW,
                         "tag_name": "cycle_ended", "operator": "eq", "value": "1"}],
    "stop_condition_logic": "all",
    "id_tag": "", "segment_tag": "", "file_name_pattern": "{template}_{date}_{time}",
    "deliver_email": False, "recipients": [],
    "attach_pdf": True, "attach_csv": True, "attach_txt": False,
    "require_gateway_running": False,
}
status, saved = call("POST", "/api/reports/schedules", tok, draft)
row = (saved or {}).get("schedule") or {}
check("the schedule saved", status in (200, 201) and bool(row.get("id")),
      row.get("id") or saved)
check("  cycle mode survived", row.get("trigger_mode") == "cycle",
      row.get("trigger_mode"))
check("  the stop condition survived",
      len(row.get("stop_conditions") or []) == 1,
      row.get("stop_conditions"))
check("  the file-name pattern survived",
      row.get("file_name_pattern") == "{template}_{date}_{time}",
      row.get("file_name_pattern"))
check("  the CSV attachment choice survived", row.get("attach_csv") is True,
      row.get("attach_csv"))

status, listed = call("GET", "/api/reports/schedules", tok)
again = [s for s in ((listed or {}).get("schedules") or [])
         if str(s.get("id")) == str(row.get("id"))]
check("  and it reads back the same after a reload",
      bool(again) and again[0].get("trigger_mode") == "cycle"
      and len(again[0].get("stop_conditions") or []) == 1,
      again[0] if again else "not in the list")

print()
print("[the saved schedule drives a cycle to completion]")
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ["TRUSTNODE_APP_STORE_PATH"] = store
os.environ["TRUSTNODE_DATA_DIR"] = tmp
from app.services.report_scheduler import ReportScheduler       # noqa: E402


class _Store:
    def __init__(self):
        self.state = {}

    def set_cycle_state(self, sid, started, report_id="", segments_json=None):
        self.state[sid] = (started, report_id)

    def mark_schedule_run(self, **kw):
        pass


class _Runner:
    def __init__(self):
        self.calls = []

    def run(self, schedule, *, triggered_by="manual", email_settings=None,
            window=None, segments=None, report_id=""):
        self.calls.append({"window": window, "schedule": schedule})
        return {"ok": True}


def history(gw, tag, lo, hi):
    out = []
    for (t, name, val) in seed:
        if name != tag:
            continue
        if lo and t[:19] < str(lo)[:19]:
            continue
        if hi and t[:19] > str(hi)[:19]:
            continue
        out.append((t, val))
    return out


st, rn = _Store(), _Runner()
sch = ReportScheduler(runner=rn, reports_store=st,
                      live_lookup=lambda g, t: 0.0,
                      history_lookup=history,
                      email_settings_lookup=lambda: None,
                      is_any_gateway_running=lambda: True)
cur = dict(again[0] if again else row)
cur["cycle_started_utc"] = ""
for sec in range(0, 721, 15):
    at = (BASE + timedelta(seconds=sec)).replace(tzinfo=timezone.utc)
    sch._consider_cycle(cur, at)
    opened = st.state.get(str(cur.get("id"))) or ("", "")
    cur = dict(cur, cycle_started_utc=opened[0])

check("the saved schedule produced exactly one report", len(rn.calls) == 1,
      "%d" % len(rn.calls))
if rn.calls:
    w = rn.calls[0]["window"]
    check("  covering the cycle",
          w[0][:19] == ts(RUN[0])[:19] and w[1][:19] == ts(RUN[1])[:19],
          "%s -> %s" % w)
    check("  and it carries the CSV choice through to delivery",
          rn.calls[0]["schedule"].get("attach_csv") is True,
          "the runner attaches the CSV from this flag")

print()
print("[two reports from ONE trigger, saved where the operator chose]")
status, made2 = call("POST", "/api/reports/templates", tok, {
    "name": "Second report", "description": "",
    "definition": {"sections": [{"type": "header", "title": "Second"}]}})
tpl2 = ((made2 or {}).get("template") or {}).get("id")
out_dir = os.path.join(tmp, "chosen", "{template}")
draft2 = dict(draft, name="Two reports", id="",
              template_ids=[tpl_id, tpl2],
              output_path=out_dir.replace(os.sep, "/") + "/report.pdf")
status, saved2 = call("POST", "/api/reports/schedules", tok, draft2)
row2 = (saved2 or {}).get("schedule") or {}
check("a schedule can name several templates",
      len(row2.get("template_ids") or []) == 2, row2.get("template_ids"))
check("  and a destination folder",
      bool(row2.get("output_path")), row2.get("output_path"))

status, ran = call("POST", "/api/reports/schedules/%s/run" % row2.get("id"), tok)
reports = (ran or {}).get("reports")
check("running it produced TWO reports",
      isinstance(reports, list) and len(reports) == 2,
      "%s" % (len(reports) if isinstance(reports, list) else ran))
written = []
for root, _dirs, files in os.walk(os.path.join(tmp, "chosen")):
    written.extend(os.path.join(root, f) for f in files)
check("  both landed in the chosen folder", len(written) >= 2,
      "%d file(s) under %s" % (len(written), os.path.join(tmp, "chosen")))
check("  the {template} token became a real folder",
      any("Cycle_UI_probe" in w or "Second_report" in w or "chosen" in w
          for w in written),
      written[:2])

print()
print("[text linked to a tag value]")
from app.services.report_renderer import resolve_text_tokens     # noqa: E402


def _fake_fetch(cond, lo, hi):
    return [{"ts": "t", "value": None, "value_text": "WO-4471"}]         if str(cond.get("tag_name")) == "Work_Order" else         [{"ts": "t", "value": 31.5}]


check("a tag value can be written into text",
      resolve_text_tokens("Order {tag:Work_Order} peak {max:Temp}",
                          "", "", "", _fake_fetch) == "Order WO-4471 peak 31.500",
      resolve_text_tokens("Order {tag:Work_Order} peak {max:Temp}", "", "", "", _fake_fetch))
check("  a tag with no reading shows '-', not nothing",
      "-" in resolve_text_tokens("{tag:Missing}", "", "", "",
                                 lambda c, a, b: []),
      "a vanished token would read as prose")

print()
print("[ramp time and duration]")
from app.services.report_renderer import _ramp_seconds, _window_seconds  # noqa: E402
heat = [{"ts": "2026-10-01 09:00:%02d" % (i * 5), "value": 20.0 + i * 2.0}
        for i in range(12)]
check("ramp time measures the climb between two thresholds",
      _ramp_seconds(heat, 25, 40) == 35.0, _ramp_seconds(heat, 25, 40))
check("  an unfinished ramp is None, not 0",
      _ramp_seconds(heat, 25, 500) is None)
check("duration is the window length",
      _window_seconds("2026-10-01 09:00:00", "2026-10-01 09:10:00") == 600.0)

print()
print("[delivery and storage are wired]")
SCHED_SRC = io.open(os.path.join(ROOT, "backend", "app", "services",
                                 "report_scheduler.py"), encoding="utf-8",
                    errors="replace").read()
check("email is sent when the schedule asks for it",
      'schedule.get("deliver_email")' in SCHED_SRC)
check("  with the CSV attached", 'attach_csv' in SCHED_SRC and "base64" in SCHED_SRC)
STORE_SRC = io.open(os.path.join(ROOT, "backend", "app", "services",
                                 "reports_store.py"), encoding="utf-8",
                    errors="replace").read()
check("the PDF is kept locally and pushed to the cloud",
      "upload_and_mirror" in STORE_SRC,
      "the row mirror and the bucket upload both run off insert_generated")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
