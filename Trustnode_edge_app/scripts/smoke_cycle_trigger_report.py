# -*- coding: utf-8 -*-
"""The cycle trigger, end to end: bits -> window -> report -> PDF + images.

2026-10-01. The PLC drives

    cycle_started  / cycle_ended      the whole run
    cycle_started1 / cycle_ended1     inner cycle 1
    cycle_started2 / cycle_ended2     inner cycle 2

and SimREAL[3] / SimREAL[4] are the values. This seeds exactly that shape,
then checks the chain the live rig will use:

  * the SCHEDULER opens the run on the start bit and closes it on the stop
    bit, producing one window per cycle - not one report a minute;
  * the report is pinned to that window;
  * each inner chart is narrowed by its OWN trigger, so it covers less than
    the run;
  * a rate KPI is computed over the run.

It renders the same template the "TCU Live Cycle Test" preset builds, and
leaves a PDF plus a PNG per page to look at.

Isolated: its own scratch workspace is set BEFORE anything imports app.state,
which migrates whatever database the environment points at.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ.setdefault("TRUSTNODE_SKIP_DOTENV", "1")
_TMP = tempfile.mkdtemp(prefix="tn-cyctrig-")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "s.db")
os.environ.setdefault("TRUSTNODE_BOOT_INTEGRITY_CHECK", "never")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


GW = "gw-plc"
# A run of 10 minutes at 5 s, with the two inner cycles nested inside it.
BASE = datetime(2026, 10, 1, 9, 0, 0)
STEP = 5
RUN = (60, 600)          # seconds from BASE
INNER1 = (120, 300)
INNER2 = (360, 540)


def ts(sec):
    return (BASE + timedelta(seconds=sec)).strftime("%Y-%m-%d %H:%M:%S.000")


def pulse(sec, at):
    """The bit is high for exactly one sample, like a ladder coil pulse."""
    return 1.0 if at <= sec < at + STEP else 0.0


rows = []
for sec in range(0, 721, STEP):
    rows.append((ts(sec), "cycle_started", pulse(sec, RUN[0])))
    rows.append((ts(sec), "cycle_ended", pulse(sec, RUN[1])))
    rows.append((ts(sec), "cycle_started1", pulse(sec, INNER1[0])))
    rows.append((ts(sec), "cycle_ended1", pulse(sec, INNER1[1])))
    rows.append((ts(sec), "cycle_started2", pulse(sec, INNER2[0])))
    rows.append((ts(sec), "cycle_ended2", pulse(sec, INNER2[1])))
    # A sawtooth, like the PLC's simulated REALs.
    rows.append((ts(sec), "SimREAL[3]", 100.0 + (sec % 180) / 3.0))
    rows.append((ts(sec), "SimREAL[4]", 1000.0 + (sec % 240) / 2.0))

conn = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
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
    " VALUES ('default',?,?,'PLC',?,?,192,'GOOD','cycle-smoke',?)",
    [(t, GW, tag, val, t) for (t, tag, val) in rows])
conn.commit()
conn.close()

print("TrustNode - cycle trigger -> report")
print()
print("[a seeded run]")
print("  %d readings | run %s..%s | inner1 %s..%s | inner2 %s..%s"
      % (len(rows), ts(RUN[0])[11:19], ts(RUN[1])[11:19],
         ts(INNER1[0])[11:19], ts(INNER1[1])[11:19],
         ts(INNER2[0])[11:19], ts(INNER2[1])[11:19]))

from datetime import timezone                                   # noqa: E402
from app.services.report_scheduler import ReportScheduler       # noqa: E402
from app.services.report_renderer import (                      # noqa: E402
    render_template_to_pdf, resolve_trigger_window, apply_window_override,
    apply_section_triggers)

# --------------------------------------------------------------------------- #
# 1. the scheduler turns the bits into ONE window
# --------------------------------------------------------------------------- #
print()
print("[the scheduler opens on the start bit and closes on the stop bit]")

LIVE = {"cycle_started": 0.0, "cycle_ended": 0.0}


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
        self.calls.append({"window": window, "triggered_by": triggered_by})
        return {"ok": True}


store, runner = _Store(), _Runner()
sched = ReportScheduler(runner=runner, reports_store=store,
                        live_lookup=lambda gw, t: LIVE.get(t),
                        email_settings_lookup=lambda: None,
                        is_any_gateway_running=lambda: True)
SCH = {"id": "live", "trigger_mode": "cycle",
       "tag_conditions": [{"gateway_id": GW, "tag_name": "cycle_started",
                           "operator": "==", "value": 1}],
       "stop_conditions": [{"gateway_id": GW, "tag_name": "cycle_ended",
                            "operator": "==", "value": 1}],
       "cycle_started_utc": "", "cycle_report_id": "", "cycle_segments": []}

cur = dict(SCH)
for sec in range(0, 721, 60):
    LIVE["cycle_started"] = pulse(sec, RUN[0])
    LIVE["cycle_ended"] = pulse(sec, RUN[1])
    at = BASE + timedelta(seconds=sec)
    sched._consider_cycle(cur, at.replace(tzinfo=timezone.utc))
    opened = store.state.get("live") or ("", "")
    cur = dict(SCH, cycle_started_utc=opened[0])

check("exactly ONE report was produced", len(runner.calls) == 1,
      "%d - a level trigger would fire once a minute" % len(runner.calls))
win = runner.calls[0]["window"] if runner.calls else ("", "")
check("  it covers the cycle, start to stop",
      win[0].startswith("2026-10-01 09:01") and win[1].startswith("2026-10-01 09:10"),
      "%s -> %s" % win)
check("  and is recorded as cycle-triggered",
      runner.calls and runner.calls[0]["triggered_by"] == "cycle")

# --------------------------------------------------------------------------- #
# 1b. the pulse must survive the tick - the reported failure
# --------------------------------------------------------------------------- #
print()
print("[a SHORT pulse between two ticks is still caught]")
# The daemon wakes every 15 s and used to read only the instantaneous live
# value, so a coil high for one scan was invisible: toggling the PLC inputs
# produced no report at all. Reading what was RECORDED catches it whenever it
# happened - and stamps the cycle with the pulse's own time, not the tick's.


def history(gw, tag, lo, hi):
    out = []
    for (t, name, val) in rows:
        if name != tag:
            continue
        if lo and t[:19] < str(lo)[:19]:
            continue
        if hi and t[:19] > str(hi)[:19]:
            continue
        out.append((t, val))
    return out


store2, runner2 = _Store(), _Runner()
pulsed = ReportScheduler(runner=runner2, reports_store=store2,
                         live_lookup=lambda gw, t: 0.0,   # live NEVER sees it
                         history_lookup=history,
                         email_settings_lookup=lambda: None,
                         is_any_gateway_running=lambda: True)
cur2 = dict(SCH, id="pulse")
# Tick every 15 s, deliberately never landing on the 5 s pulse.
for sec in range(0, 721, 15):
    at = (BASE + timedelta(seconds=sec)).replace(tzinfo=timezone.utc)
    pulsed._consider_cycle(cur2, at)
    opened = store2.state.get("pulse") or ("", "")
    cur2 = dict(cur2, cycle_started_utc=opened[0])

check("the pulse opened and closed a cycle", len(runner2.calls) == 1,
      "%d report(s) - the live value is 0 at every tick, so this only works "
      "by reading what was recorded" % len(runner2.calls))
if runner2.calls:
    pw = runner2.calls[0]["window"]
    check("  the window is the PULSE's own timestamps, not the tick's",
          pw[0][:19] == ts(RUN[0])[:19] and pw[1][:19] == ts(RUN[1])[:19],
          "%s -> %s (expected %s -> %s)"
          % (pw[0], pw[1], ts(RUN[0]), ts(RUN[1])))

# --------------------------------------------------------------------------- #
# 2. the inner triggers narrow their own sections
# --------------------------------------------------------------------------- #
print()
print("[each inner chart is narrowed by its OWN bits]")


def trig(start, stop):
    return {"start": {"gateway_id": GW, "tag_name": start, "operator": "!=", "value": 0},
            "stop": {"gateway_id": GW, "tag_name": stop, "operator": "!=", "value": 0}}


run_from, run_to = ts(RUN[0]), ts(RUN[1])
w1 = resolve_trigger_window(trig("cycle_started1", "cycle_ended1"), run_from, run_to)
w2 = resolve_trigger_window(trig("cycle_started2", "cycle_ended2"), run_from, run_to)
check("inner 1 resolves to its own window",
      w1[0][:19] == ts(INNER1[0])[:19] and w1[1][:19] == ts(INNER1[1])[:19], w1)
check("inner 2 resolves to its own window",
      w2[0][:19] == ts(INNER2[0])[:19] and w2[1][:19] == ts(INNER2[1])[:19], w2)
check("  both sit inside the run",
      run_from[:19] <= w1[0][:19] and w2[1][:19] <= run_to[:19])

# --------------------------------------------------------------------------- #
# 3. the report, built like the shipped preset
# --------------------------------------------------------------------------- #
VALUES = ["SimREAL[3]", "SimREAL[4]"]


def series(tag, color):
    return {"id": tag, "label": tag, "gateway_id": GW, "tag_name": tag, "color": color}


definition = {"sections": [
    {"type": "header", "title": "TCU Cycle Test",
     "subtitle": "generated automatically when the cycle ends",
     "show_generated_at": True},
    {"type": "static_table", "title": "Cycle",
     "header": ["What", "Value"],
     "rows": [["Gateway", GW], ["Start bit", "cycle_started"],
              ["Stop bit", "cycle_ended"], ["Window", "%s -> %s" % (win[0], win[1])]]},
    {"type": "kpi_grid", "title": "Cycle results", "columns": 4,
     "time_range": {"preset": "run"},
     "items": [
         {"label": "SimREAL[3] max", "gateway_id": GW, "tag_name": "SimREAL[3]",
          "aggregation": "max"},
         {"label": "SimREAL[3] avg", "gateway_id": GW, "tag_name": "SimREAL[3]",
          "aggregation": "avg"},
         {"label": "Peak rise (per min)", "gateway_id": GW, "tag_name": "SimREAL[3]",
          "aggregation": "rate_max"},
         {"label": "SimREAL[4] avg", "gateway_id": GW, "tag_name": "SimREAL[4]",
          "aggregation": "avg"},
     ]},
    {"type": "line_chart", "title": "Whole cycle", "time_range": {"preset": "run"},
     "x_axis_label": "Time", "y_axis_label": "Value",
     "series": [series(VALUES[0], "#ef5350"), series(VALUES[1], "#42a5f5")]},
    {"type": "line_chart", "title": "Inner cycle 1", "time_range": {"preset": "run"},
     "trigger": trig("cycle_started1", "cycle_ended1"),
     "x_axis_label": "Time", "y_axis_label": VALUES[0],
     "series": [series(VALUES[0], "#26a69a")]},
    {"type": "bar_chart", "title": "Inner cycle 2", "time_range": {"preset": "run"},
     "trigger": trig("cycle_started2", "cycle_ended2"), "side_by_side": True,
     "x_axis_label": "Time", "y_axis_label": VALUES[0],
     "series": [series(VALUES[0], "#7e57c2")]},
    {"type": "table", "title": "Telemetry", "time_range": {"preset": "run"},
     "row_limit": 40, "align_columns": ["left", "right", "right"],
     "series": [series(t, "#888") for t in VALUES],
     "columns": ([{"key": "ts", "title": "Time"}] +
                 [{"key": "value", "series_id": t, "title": t} for t in VALUES])},
]}

print()
print("[the sections the renderer will actually draw]")
pinned = apply_section_triggers(apply_window_override(
    {"definition": definition}, win[0], win[1]))
secs = pinned["definition"]["sections"]


def window_of(title):
    for s in secs:
        if s.get("title") == title:
            tr = s.get("time_range") or {}
            return tr.get("from_utc", ""), tr.get("to_utc", "")
        if s.get("type") == "row":
            for c in s.get("columns") or []:
                if c.get("title") == title:
                    tr = c.get("time_range") or {}
                    return tr.get("from_utc", ""), tr.get("to_utc", "")
    return ("", "")


whole = window_of("Whole cycle")
i1 = window_of("Inner cycle 1")
i2 = window_of("Inner cycle 2")
check("the whole-cycle chart uses the run window",
      whole[0][:19] == win[0][:19] and whole[1][:19] == win[1][:19], whole)
check("inner 1 covers LESS than the run", i1[0] > whole[0] and i1[1] < whole[1],
      "%s vs run %s" % (i1, whole))
check("inner 2 covers LESS than the run", i2[0] > whole[0] and i2[1] < whole[1],
      "%s vs run %s" % (i2, whole))
check("  and the two inner windows do not overlap", i1[1] <= i2[0],
      "%s then %s" % (i1, i2))

print()
print("[the PDF]")
try:
    path, nbytes, _sha = render_template_to_pdf(
        {"name": "TCU Live Cycle Test", "definition": definition},
        window=(win[0], win[1]))
    ok = nbytes > 20000
except Exception as exc:                                  # noqa: BLE001
    path, nbytes, ok = "", 0, False
    check("the report rendered", False, str(exc)[:160])
if path:
    check("the report rendered", ok, "%d bytes" % nbytes)

out_dir = os.path.join(ROOT, "scripts", "validation_out")
os.makedirs(out_dir, exist_ok=True)
sample = os.path.join(out_dir, "Cycle_Trigger_Report.pdf")
shots = []
if path and os.path.exists(path):
    import shutil
    shutil.copyfile(path, sample)
    try:
        import pymupdf
        doc = pymupdf.open(sample)
        for i, page in enumerate(doc, 1):
            img = os.path.join(out_dir, "Cycle_Trigger_p%d.png" % i)
            page.get_pixmap(dpi=110).save(img)
            shots.append(img)
        doc.close()
    except Exception as exc:                              # noqa: BLE001
        print("  (page images unavailable: %s)" % str(exc)[:80])
check("page images were produced", len(shots) >= 2, "%d page(s)" % len(shots))

print()
print("SAMPLE: %s" % (sample if shots or path else "(none)"))
for img in shots:
    print("PREVIEW: %s" % img)
print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
