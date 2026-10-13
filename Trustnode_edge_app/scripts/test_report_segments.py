# -*- coding: utf-8 -*-
"""Per-section windows, segments, repetition and cross-segment aggregation.

2026-09-30, requested: a test report whose overall span comes from a start/stop
trigger, but whose charts, telemetry and KPIs may each cover a DIFFERENT span -
"4 different type of charts and data based in different type of time ranges".
The second test type repeats for a number of cycles, and a table must show an
aggregation per tag per test, then aggregate those results again.

What was wrong before:
  * apply_window_override flattened EVERY section to the run's window, so the
    moment a trigger was used the per-section ranges were destroyed - the two
    features could not be combined at all;
  * a KPI grid never passed a window to the historian, so every KPI in every
    report aggregated all of history. Cycle reports were silently wrong.
"""
from __future__ import annotations

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ.setdefault("TRUSTNODE_SKIP_DOTENV", "1")

# Point the workspace at a scratch directory BEFORE importing anything that
# can reach app.state. Importing it instantiates AppStore, which runs
# _ensure_schema() and ALTERs whatever database the environment points at -
# this file reached it through _fetch_kpi_value and tried to migrate the LIVE
# store on 2026-09-30. It failed only because that file is read-only.
import tempfile                                          # noqa: E402
_TMP = tempfile.mkdtemp(prefix="tn-segtest-")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "scratch.db")
os.environ.setdefault("TRUSTNODE_BOOT_INTEGRITY_CHECK", "never")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:160]) if detail else ""))
    if not ok:
        FAILS.append(name)


from app.services.report_renderer import (            # noqa: E402
    apply_window_override, select_segments, section_uses_run_window)

RUN = ("2026-09-30 09:00:00", "2026-09-30 10:00:00")
SEGMENTS = [
    {"name": "test1", "label": "Test 1", "index": 1,
     "from_utc": "2026-09-30 09:00:00", "to_utc": "2026-09-30 09:10:00"},
    {"name": "test2", "label": "Test 2", "index": 1,
     "from_utc": "2026-09-30 09:10:00", "to_utc": "2026-09-30 09:25:00"},
    {"name": "test2", "label": "Test 2", "index": 2,
     "from_utc": "2026-09-30 09:25:00", "to_utc": "2026-09-30 09:40:00"},
    {"name": "test2", "label": "Test 2", "index": 3,
     "from_utc": "2026-09-30 09:40:00", "to_utc": "2026-09-30 10:00:00"},
]


def sections_of(tpl):
    return tpl["definition"]["sections"]


print("TrustNode - report segments, per-section windows, repetition")
print()
print("[a trigger no longer flattens every section]")
tpl = {"definition": {"sections": [
    {"type": "line_chart", "title": "Whole run", "time_range": {"preset": "24h"}},
    {"type": "line_chart", "title": "Pinned", "time_range": {
        "preset": "custom", "from_utc": "2026-01-01 00:00:00", "to_utc": "2026-01-02 00:00:00"}},
    {"type": "table", "title": "No range at all"},
    {"type": "bar_chart", "title": "A batch", "time_range": {"preset": "batch", "batch_id": "b-7"}},
]}}
out = sections_of(apply_window_override(tpl, *RUN, segments=SEGMENTS))
check("a relative range follows the run",
      out[0]["time_range"] == {"preset": "custom", "from_utc": RUN[0], "to_utc": RUN[1]},
      out[0]["time_range"])
check("  a section with NO range follows the run too",
      out[2]["time_range"]["from_utc"] == RUN[0])
check("explicit dates are left alone",
      out[1]["time_range"]["from_utc"] == "2026-01-01 00:00:00",
      "the operator set these deliberately; a trigger must not overwrite them")
check("  a batch anchor is left alone",
      out[3]["time_range"] == {"preset": "batch", "batch_id": "b-7"},
      out[3]["time_range"])
check("  the original template is not mutated",
      tpl["definition"]["sections"][0]["time_range"]["preset"] == "24h")

print()
print("[a section can name a segment]")
tpl2 = {"definition": {"sections": [
    {"type": "line_chart", "title": "Test 1", "time_range": {"preset": "segment", "segment": "test1"}},
    {"type": "line_chart", "title": "3rd cycle", "time_range": {
        "preset": "segment", "segment": "test2", "index": 3}},
    {"type": "line_chart", "title": "Last cycle", "time_range": {
        "preset": "segment", "segment": "test2", "index": "last"}},
    {"type": "line_chart", "title": "Missing", "time_range": {
        "preset": "segment", "segment": "test9"}},
]}}
o2 = sections_of(apply_window_override(tpl2, *RUN, segments=SEGMENTS))
check("a named segment resolves to its window",
      (o2[0]["time_range"]["from_utc"], o2[0]["time_range"]["to_utc"])
      == ("2026-09-30 09:00:00", "2026-09-30 09:10:00"), o2[0]["time_range"])
check("  an index picks that cycle (1-based)",
      o2[1]["time_range"]["from_utc"] == "2026-09-30 09:40:00",
      "cycle 3 of test2; an operator counts from one")
check("  'last' picks the final one",
      o2[2]["time_range"]["from_utc"] == "2026-09-30 09:40:00")
check("a segment that never ran renders EMPTY, not all-time",
      o2[3]["time_range"]["from_utc"].startswith("9999"),
      "widening to all history would look like a real measurement")

print()
print("[a section can repeat per cycle]")
tpl3 = {"definition": {"sections": [
    {"type": "line_chart", "title": "Cycle trend",
     "repeat_for": {"segment": "test2"}},
    {"type": "text", "title": "After", "text": "x"},
]}}
o3 = sections_of(apply_window_override(tpl3, *RUN, segments=SEGMENTS))
check("one section became one per cycle",
      len([s for s in o3 if s.get("type") == "line_chart"]) == 3,
      "%d chart(s) for 3 cycles of test2" % len([s for s in o3 if s.get("type") == "line_chart"]))
check("  each carries its own cycle window",
      [s["time_range"]["from_utc"] for s in o3 if s["type"] == "line_chart"]
      == ["2026-09-30 09:10:00", "2026-09-30 09:25:00", "2026-09-30 09:40:00"])
check("  and is titled so the reader can tell them apart",
      [s["title"] for s in o3 if s["type"] == "line_chart"]
      == ["Cycle trend - Test 2 1", "Cycle trend - Test 2 2", "Cycle trend - Test 2 3"],
      [s["title"] for s in o3 if s["type"] == "line_chart"])
check("  sections after it still follow", o3[-1]["title"] == "After")
check("repeating over a segment that never ran drops the section",
      not sections_of(apply_window_override(
          {"definition": {"sections": [{"type": "line_chart", "repeat_for": {"segment": "nope"}}]}},
          *RUN, segments=SEGMENTS)),
      "an empty chart with a cycle title would imply the cycle ran")

print()
print("[selection helper]")
check("every cycle of a test", len(select_segments(SEGMENTS, "test2")) == 3)
check("  no name means all segments", len(select_segments(SEGMENTS, "")) == 4)
check("  an out-of-range index selects nothing",
      select_segments(SEGMENTS, "test2", 9) == [])

print()
print("[the KPI grid finally has a window]")
import inspect                                          # noqa: E402
from app.services import report_renderer as rr          # noqa: E402
sig = inspect.signature(rr._fetch_kpi_value)
check("_fetch_kpi_value accepts a window",
      "from_utc" in sig.parameters and "to_utc" in sig.parameters, str(sig))
src = inspect.getsource(rr._fetch_kpi_value)
# Check the AGGREGATE call specifically. "from_utc=from_utc" also appears in
# the text-value fallback below it, so looking for the bare string passes even
# when the aggregate - the number the KPI actually shows - is unscoped.
_stats_call = src.split("get_historian_rule_stats", 1)[-1].split(")", 1)[0]
check("  and passes it to the AGGREGATE query",
      "from_utc=from_utc" in _stats_call and "to_utc=to_utc" in _stats_call,
      "without this every KPI aggregates ALL of history")
grid = inspect.getsource(rr.build_report_payload) if hasattr(rr, "build_report_payload") else ""
if not grid:
    grid = open(os.path.join(ROOT, "backend", "app", "services", "report_renderer.py"),
                encoding="utf-8").read()
check("  the kpi_grid section resolves its own range",
      "_fetch_kpi_value(it, kpi_from, kpi_to)" in grid,
      "the grid carried a time_range that nothing read")
# There are TWO kpi dispatch sites: the JSON payload builder above and the PDF
# story below. Fixing only one left every RENDERED report still aggregating
# all of history - which is the half an operator actually reads.
_pdf_kpi = grid.split("def _section_kpi_grid", 1)[1][:1400]
check("  and so does the PDF's own KPI drawer",
      "_fetch_kpi_value(item, kpi_from, kpi_to)" in _pdf_kpi,
      "the payload and the PDF are drawn by different code")

print()
print("[the scheduler divides a run into segments]")
from datetime import datetime, timezone                  # noqa: E402
from app.services.report_scheduler import ReportScheduler  # noqa: E402
import json as _json                                     # noqa: E402


class _Store:
    def __init__(self):
        self.saved = {}
        self.runs = []

    def set_cycle_state(self, sid, started, report_id="", segments_json=None):
        cur = self.saved.get(sid, {})
        cur.update({"started": started, "report_id": report_id})
        if segments_json is not None:
            cur["segments"] = _json.loads(segments_json)
        self.saved[sid] = cur

    def mark_schedule_run(self, **kw):
        pass


class _Runner:
    def __init__(self):
        self.calls = []

    def run(self, schedule, *, triggered_by="manual", email_settings=None,
            window=None, segments=None, report_id=""):
        self.calls.append({"window": window, "segments": segments})
        return {"ok": True}


LIVE = {"Running": 1, "Done": 0, "Test_Step": "test1"}
store, runner = _Store(), _Runner()
sched = ReportScheduler(runner=runner, reports_store=store,
                        live_lookup=lambda gw, t: LIVE.get(t),
                        email_settings_lookup=lambda: None,
                        is_any_gateway_running=lambda: True)
SCH = {"id": "s1", "trigger_mode": "cycle",
       "tag_conditions": [{"tag_name": "Running", "operator": "==", "value": 1}],
       "stop_conditions": [{"tag_name": "Done", "operator": "==", "value": 1}],
       "segment_tag": "Test_Step", "segment_name": "test", "segment_label": "Test",
       "cycle_started_utc": "", "cycle_report_id": "", "cycle_segments": []}


def tick(minute, sc):
    sched._consider_cycle(sc, datetime(2026, 9, 30, 9, minute, 0, tzinfo=timezone.utc))


tick(0, SCH)                                   # opens the cycle
started = store.saved["s1"]["started"]
live = dict(SCH, cycle_started_utc=started)
for minute, step in ((1, "test1"), (5, "test2"), (9, "test2"), (13, "test2b")):
    LIVE["Test_Step"] = step
    live["cycle_segments"] = store.saved["s1"].get("segments", [])
    tick(minute, live)
segs = store.saved["s1"].get("segments", [])
check("a value change opens a new segment", len(segs) == 3,
      [(g.get("value"), g.get("from_utc")) for g in segs])
check("  the same value does NOT open another",
      [g.get("value") for g in segs] == ["test1", "test2", "test2b"],
      "the tag was read twice as test2; that is one sub-period, not two")
check("  each segment is closed when the next opens",
      all(g.get("to_utc") for g in segs[:-1]), segs)

LIVE["Done"] = 1
live["cycle_segments"] = segs
tick(20, live)
check("the run reports its segments", bool(runner.calls and runner.calls[0]["segments"]),
      "without them a summary table has no rows")
final = runner.calls[0]["segments"] if runner.calls else []
check("  the last segment is sealed at the cycle end",
      bool(final and final[-1].get("to_utc")), final[-1] if final else None)
check("  and they span the run", bool(final and final[0].get("from_utc") == started))

print()
print("[a summary table: one row per test, a footer across them]")
from app.services.report_renderer import build_segment_summary, _reduce  # noqa: E402
check("avg across per-test results", _reduce([1.0, 2.0, 6.0], "avg") == 3.0)
check("  min / max / sum",
      (_reduce([1.0, 2.0, 6.0], "min"), _reduce([1.0, 2.0, 6.0], "max"),
       _reduce([1.0, 2.0, 6.0], "sum")) == (1.0, 6.0, 9.0))
check("  median of an even count",
      _reduce([1.0, 2.0, 3.0, 4.0], "median") == 2.5)
check("  an empty column is None, not 0",
      _reduce([], "avg") is None,
      "0 would read as a measured zero")

# Canned readings: the point here is the GROUPING and the footer maths, not
# the historian. Peak torque per cycle: 10, 20, 30 -> average 20.
import app.services.report_renderer as _rr                # noqa: E402
_PEAKS = {"2026-09-30 09:10:00": 10.0, "2026-09-30 09:25:00": 20.0,
          "2026-09-30 09:40:00": 30.0}
_rr._fetch_kpi_value = lambda item, f="", t="": (_PEAKS.get(f), 1, None)

SUMMARY = {"type": "segment_summary", "segment": "test2",
           "columns": [{"tag_name": "Torque", "aggregation": "max", "title": "Peak torque"}],
           "footer_aggregation": "avg"}
hdr, rows = build_segment_summary(SUMMARY, SEGMENTS)
check("the header names the column", hdr == ["Test", "Peak torque"], hdr)
check("  one row per cycle plus the footer", len(rows) == 4,
      "3 cycles of test2 + AVG; got %d" % len(rows))
check("  the footer is labelled", rows[-1][0] == "AVG", rows[-1][0])
check("  each row carries THAT cycle's result",
      [r[1] for r in rows[:3]] == ["10.000", "20.000", "30.000"],
      [r[1] for r in rows[:3]])
check("  the footer aggregates the per-test results",
      rows[-1][1] == "20.000",
      "avg(10,20,30) = 20 - this is the 'final result in each test, then "
      "calculate average' the operator asked for")
check("  rows are titled per cycle",
      [r[0] for r in rows[:3]] == ["Test 2 1", "Test 2 2", "Test 2 3"],
      [r[0] for r in rows[:3]])
h2, r2 = build_segment_summary({**SUMMARY, "footer_aggregation": ""}, SEGMENTS)
check("  no footer when none is asked for", len(r2) == 3, len(r2))

print()
print("[a TCU functional test: test / cycle / phase at once]")
# 2026-09-30: "initial assessment (short test) ... final test (that might have
# multiple cycles) ... for both tests we have different phases". Three tags
# move independently, so a phase change must not disturb the cycle around it.
LIVE2 = {"Run": 1, "Done": 0, "Test": "initial", "Cycle": "1", "Phase": "Enabling"}
store2, runner2 = _Store(), _Runner()
s2 = ReportScheduler(runner=runner2, reports_store=store2,
                     live_lookup=lambda gw, t: LIVE2.get(t),
                     email_settings_lookup=lambda: None,
                     is_any_gateway_running=lambda: True)
SCH2 = {"id": "tcu", "trigger_mode": "cycle",
        "tag_conditions": [{"tag_name": "Run", "operator": "==", "value": 1}],
        "stop_conditions": [{"tag_name": "Done", "operator": "==", "value": 1}],
        "segment_tags": [{"name": "test", "tag": "Test"},
                         {"name": "cycle", "tag": "Cycle"},
                         {"name": "phase", "tag": "Phase"}],
        "cycle_started_utc": "", "cycle_report_id": "", "cycle_segments": []}


def tick2(minute, sc):
    s2._consider_cycle(sc, datetime(2026, 9, 30, 10, minute, 0, tzinfo=timezone.utc))


tick2(0, SCH2)
started2 = store2.saved["tcu"]["started"]
live2 = dict(SCH2, cycle_started_utc=started2)
SCRIPT = [
    (1, "initial", "1", "Enabling"), (2, "initial", "1", "Heating"),
    (3, "initial", "1", "Holding"),  (4, "final", "1", "Enabling"),
    (5, "final", "1", "Deadhead"),   (6, "final", "1", "Heating"),
    (8, "final", "2", "Enabling"),   (9, "final", "2", "Heating"),
    (11, "final", "3", "Enabling"),  (12, "final", "3", "Heating"),
]
for minute, test, cyc, phase in SCRIPT:
    LIVE2.update({"Test": test, "Cycle": cyc, "Phase": phase})
    live2["cycle_segments"] = store2.saved["tcu"].get("segments", [])
    tick2(minute, live2)
LIVE2["Done"] = 1
live2["cycle_segments"] = store2.saved["tcu"].get("segments", [])
tick2(15, live2)
segs2 = runner2.calls[0]["segments"] if runner2.calls else []

def st(name):
    return [g for g in segs2 if g.get("stream") == name]

check("the three streams are tracked independently",
      len(st("test")) == 2 and len(st("cycle")) == 3 and len(st("phase")) >= 8,
      "test=%d cycle=%d phase=%d" % (len(st("test")), len(st("cycle")), len(st("phase"))))
check("  a phase change does not close the cycle",
      st("cycle")[1]["from_utc"] < st("cycle")[1]["to_utc"],
      "cycle 2 spans its phases")
check("  the final test has 3 cycles",
      len([g for g in st("cycle")]) == 3)
check("every segment is closed when the run ends",
      all(g.get("to_utc") for g in segs2),
      [g for g in segs2 if not g.get("to_utc")][:2])

print()
print("[a section addresses a phase by the tag VALUE - the reported gap]")
heating = select_segments(segs2, "Heating", None, "phase")
check("asking for 'Heating' finds the heating phases", len(heating) >= 3,
      "%d found - before this fix the matcher compared the STREAM name and "
      "returned nothing" % len(heating))
check("  a stream narrows it", not select_segments(segs2, "Heating", None, "cycle"))
check("  the 2nd heating phase is addressable",
      bool(select_segments(segs2, "Heating", 2, "phase")))
check("  a cycle is addressable by its number",
      bool(select_segments(segs2, "3", None, "cycle")),
      "Cycle tag read '3'")
check("  and the final test by name",
      bool(select_segments(segs2, "final", None, "test")))
tpl_ph = {"definition": {"sections": [
    {"type": "line_chart", "title": "Heating",
     "time_range": {"preset": "segment", "stream": "phase", "segment": "Heating", "index": 1}}]}}
bound = sections_of(apply_window_override(tpl_ph, started2, "2026-09-30 10:15:00",
                                          segments=segs2))
check("a chart bound to a phase gets that phase's window",
      bound[0]["time_range"]["from_utc"] == heating[0]["from_utc"],
      bound[0]["time_range"])

print()
print("[each section carries its OWN start/stop trigger, from gateway tags]")
# 2026-09-30: "for each section of my report I want to be able to pick
# different start and end trigger where the data inside of the section would
# be loaded, I want to be able to select from the gateways tags" - and "all of
# this should be able to be reused when using schedule reports".
from app.services.report_renderer import (                 # noqa: E402
    apply_section_triggers, resolve_trigger_window, _cond_true)

READINGS = {
    ("gw-1", "Phase"): [
        ("2026-09-30 10:00:00", "Enabling"), ("2026-09-30 10:02:00", "Heating"),
        ("2026-09-30 10:05:00", "Holding"),  ("2026-09-30 10:08:00", "Cooling"),
    ],
    ("gw-1", "Temp"): [
        ("2026-09-30 10:00:00", 20.0), ("2026-09-30 10:03:00", 55.0),
        ("2026-09-30 10:06:00", 80.0), ("2026-09-30 10:09:00", 40.0),
    ],
}


def fake_fetch(cond, f, t):
    key = (str(cond.get("gateway_id") or ""), str(cond.get("tag_name") or ""))
    out = []
    for ts, val in READINGS.get(key, []):
        if (not f or ts >= f) and (not t or ts <= t):
            out.append({"ts_utc": ts,
                        "value": val if isinstance(val, (int, float)) else None,
                        "value_text": None if isinstance(val, (int, float)) else val})
    return out


OUTER = ("2026-09-30 10:00:00", "2026-09-30 10:10:00")
w = resolve_trigger_window(
    {"start": {"gateway_id": "gw-1", "tag_name": "Phase", "operator": "==", "value": "Heating"}},
    *OUTER, fetch=fake_fetch)
check("a text tag starts and ends the window",
      w == ("2026-09-30 10:02:00", "2026-09-30 10:05:00"), w)

w2 = resolve_trigger_window(
    {"start": {"gateway_id": "gw-1", "tag_name": "Phase", "operator": "==", "value": "Heating"},
     "stop":  {"gateway_id": "gw-1", "tag_name": "Temp",  "operator": ">=", "value": 80}},
    *OUTER, fetch=fake_fetch)
check("  a DIFFERENT tag can close it",
      w2 == ("2026-09-30 10:02:00", "2026-09-30 10:06:00"), w2)

w3 = resolve_trigger_window(
    {"start": {"gateway_id": "gw-1", "tag_name": "Temp", "operator": ">=", "value": 50}},
    *OUTER, fetch=fake_fetch)
check("  a numeric threshold works too",
      w3[0] == "2026-09-30 10:03:00", w3)

w4 = resolve_trigger_window(
    {"start": {"gateway_id": "gw-1", "tag_name": "Phase", "operator": "==", "value": "Vacuum"}},
    *OUTER, fetch=fake_fetch)
# 2026-10-11, requested: "the data will be collected within the triggers the
# main schedule report start and end trigger when the report section do no
# clear do not use different trigger". A section whose trigger never fires now
# falls back to the window that ENCLOSES it - the schedule's own triggered
# window - rather than printing nothing. apply_section_triggers marks it so
# the caption says the window is not the section's own.
check("a trigger that never fires falls back to the ENCLOSING window",
      w4 == OUTER,
      "it must not invent a different range, and it must not go blank: %s" % (w4,))
w4b = resolve_trigger_window(
    {"start": {"gateway_id": "gw-1", "tag_name": "Phase", "operator": "==", "value": "Vacuum"},
     "on_miss": "empty"},
    *OUTER, fetch=fake_fetch)
check("  on_miss='empty' still excludes everything",
      w4b[0].startswith("9999") and w4b[1].startswith("9999"),
      "for a section documenting one OPTIONAL phase, blank is the honest "
      "answer: %s" % (w4b,))

w5 = resolve_trigger_window(
    {"start": {"gateway_id": "gw-1", "tag_name": "Phase", "operator": "==", "value": "Cooling"}},
    *OUTER, fetch=fake_fetch)
check("  still-running at report time closes at the outer end",
      w5 == ("2026-09-30 10:08:00", OUTER[1]), w5)

check("no trigger leaves the section alone",
      resolve_trigger_window({}, *OUTER, fetch=fake_fetch) == OUTER)

print()
print("  [and it works for a SCHEDULED report, not just a cycle]")
sched_tpl = {"definition": {"sections": [
    {"type": "line_chart", "title": "Heating only",
     "time_range": {"preset": "custom", "from_utc": OUTER[0], "to_utc": OUTER[1]},
     "trigger": {"start": {"gateway_id": "gw-1", "tag_name": "Phase",
                           "operator": "==", "value": "Heating"}}},
    {"type": "table", "title": "Everything",
     "time_range": {"preset": "custom", "from_utc": OUTER[0], "to_utc": OUTER[1]}},
]}}
done = sections_of(apply_section_triggers(sched_tpl, fetch=fake_fetch))
check("the triggered section narrows to its phase",
      (done[0]["time_range"]["from_utc"], done[0]["time_range"]["to_utc"])
      == ("2026-09-30 10:02:00", "2026-09-30 10:05:00"),
      done[0]["time_range"])
check("  a section with no trigger is untouched",
      done[1]["time_range"]["from_utc"] == OUTER[0])
check("  the original template is not mutated",
      sched_tpl["definition"]["sections"][0]["time_range"]["from_utc"] == OUTER[0])

RENDER = io.open(os.path.join(ROOT, "backend", "app", "services",
                              "report_renderer.py"), encoding="utf-8").read()
_entry = RENDER.split("def render_template_to_pdf", 1)[1][:1400]
check("every render resolves section triggers",
      "apply_section_triggers(template)" in _entry,
      "inside the `if window` block it would only work for cycle reports")

print()
print("[rate KPIs - the two headline numbers of a TCU test]")
from app.services.report_renderer import _rate_per_minute     # noqa: E402
# get_historian_rows_range returns "ts"; the first version read "ts_utc" only,
# skipped every sample and rendered "-" where a peak rate should be.
heat = [{"ts": "2026-09-30 10:00:%02d" % (i * 5), "value": 20.0 + i * 0.85}
        for i in range(12)]
cool = [{"ts": "2026-09-30 10:00:%02d" % (i * 5), "value": 100.0 - i * 0.90}
        for i in range(12)]
check("peak heating rate is per MINUTE",
      round(_rate_per_minute(heat, "rate_max")[0], 1) == 10.2,
      _rate_per_minute(heat, "rate_max"))
check("  peak cooling rate is negative",
      round(_rate_per_minute(cool, "rate_min")[0], 1) == -10.8,
      _rate_per_minute(cool, "rate_min"))
check("  the ts_utc spelling still works",
      _rate_per_minute([{"ts_utc": r["ts"], "value": r["value"]} for r in heat],
                       "rate_max")[0] is not None,
      "other fetchers carry ts_utc")
check("  samples less than a second apart are ignored",
      _rate_per_minute([{"ts": "2026-09-30 10:00:00", "value": 1.0},
                        {"ts": "2026-09-30 10:00:00", "value": 9.0}],
                       "rate_max")[0] is None,
      "dividing by ~0 manufactures enormous slopes from jitter")

print()
print("[the trigger against a REAL historian, not a stub]")
# The checks above inject their own fetch, so they cannot catch a mismatch
# between what the fetcher returns and what the resolver reads. That is
# exactly what shipped: get_historian_rows_range returns "ts", the resolver
# read "ts_utc", nothing ever matched, and the resulting EMPTY window meant
# "all available" to every fetcher below - so a chart asking for the heating
# phase rendered the entire run.
import sqlite3 as _sq                                     # noqa: E402
_db = os.environ["TRUSTNODE_APP_STORE_PATH"]
_c = _sq.connect(_db)
_c.execute("CREATE TABLE IF NOT EXISTS historian_readings ("
           " id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT DEFAULT 'default',"
           " ts_utc TEXT, gateway_id TEXT, gateway_name TEXT, device_name TEXT,"
           " plc_ip TEXT, database_name TEXT, tag_name TEXT, value REAL,"
           " value_text TEXT, data_type TEXT, quality INTEGER, quality_label TEXT,"
           " source TEXT, created_utc TEXT)")
_seed = []
for _i in range(12):
    _ts = "2026-10-01 10:%02d:00" % _i
    _ph = "Enabling" if _i < 4 else ("Heating" if _i < 8 else "Cooling")
    _seed.append((_ts, "gw-x", "Phase_Name", None, _ph, _ts))
    _seed.append((_ts, "gw-x", "Temp", 20.0 + _i, None, _ts))
_c.executemany("INSERT INTO historian_readings (tenant_id, ts_utc, gateway_id,"
               " tag_name, value, value_text, quality, quality_label, created_utc)"
               " VALUES ('default',?,?,?,?,?,192,'GOOD',?)", _seed)
_c.commit()
_c.close()

_cond = {"gateway_id": "gw-x", "tag_name": "Phase_Name",
         "operator": "==", "value": "Heating"}
_real = resolve_trigger_window({"start": _cond},
                               "2026-10-01 10:00:00", "2026-10-01 10:11:00")
check("the heating phase is found through the real fetcher",
      _real == ("2026-10-01 10:04:00", "2026-10-01 10:08:00"), _real)

_miss = resolve_trigger_window(
    {"start": dict(_cond, value="Vacuum")},
    "2026-10-01 10:00:00", "2026-10-01 10:11:00")
check("  a phase that never ran falls back to the report window",
      _miss == ("2026-10-01 10:00:00", "2026-10-01 10:11:00"), _miss)
_miss_empty = resolve_trigger_window(
    {"start": dict(_cond, value="Vacuum"), "on_miss": "empty"},
    "2026-10-01 10:00:00", "2026-10-01 10:11:00")
check("    unless the section asked to be left empty",
      _miss_empty[0].startswith("9999"), _miss_empty)

print()
print("[a trigger reads ITS tag, and finds a pulse on the boundary]")
# Found on 2026-10-01 validating against a PLC running cycle_started /
# cycle_started1 / cycle_started2:
#   * the store treated a tag name CONTAINING an underscore as a wildcard
#     search, so a trigger on `cycle_started` also read `cycle_started1`
#     and `cycle_started2` - and the main cycle resolved to an inner one;
#   * a range filter is normalised to 'SS.fff', so a reading stored without
#     milliseconds sorted BEFORE a window starting on its own second and the
#     start pulse - which sits exactly there - was dropped.
_c2 = _sq.connect(_db)
_pulses = []
for _i in range(12):
    _ts = "2026-10-02 08:%02d:00" % _i          # no milliseconds, on purpose
    _pulses.append((_ts, "gw-p", "cycle_started", 1.0 if _i == 0 else 0.0, _ts))
    _pulses.append((_ts, "gw-p", "cycle_started1", 1.0 if _i == 3 else 0.0, _ts))
    _pulses.append((_ts, "gw-p", "cycle_ended", 1.0 if _i == 10 else 0.0, _ts))
_c2.executemany("INSERT INTO historian_readings (tenant_id, ts_utc, gateway_id,"
                " tag_name, value, quality, quality_label, created_utc)"
                " VALUES ('default',?,?,?,?,192,'GOOD',?)", _pulses)
_c2.commit()
_c2.close()

_win = resolve_trigger_window(
    {"start": {"gateway_id": "gw-p", "tag_name": "cycle_started",
               "operator": "!=", "value": 0},
     "stop": {"gateway_id": "gw-p", "tag_name": "cycle_ended",
              "operator": "!=", "value": 0}},
    "2026-10-02 08:00:00", "2026-10-02 08:11:00")
check("a start pulse ON the window boundary is found",
      _win[0] == "2026-10-02 08:00:00", _win)
check("  and the cycle closes on its own end bit",
      _win[1] == "2026-10-02 08:10:00", _win)
# The underscore-as-wildcard bug is asserted at the SOURCE, not through a
# query: the behaviour only diverges when a neighbouring tag pulses FIRST,
# which makes a behavioural test pass for the wrong reason more often than it
# catches anything. What must never come back is the condition itself.
_AS = io.open(os.path.join(ROOT, "backend", "app", "services", "app_store.py"),
              encoding="utf-8", errors="replace").read()
check("no query treats an underscore as a wildcard",
      '"_" in tag_txt' not in _AS,
      'SQL LIKE reads "_" as any-character, so a filter on `cycle_started` '
      'also matched `cycle_started1`, and `active_power_w` matched '
      '`active_power_w_raw` - with the index lost as well')
check("  a real wildcard still works, with the underscore escaped",
      _AS.count(chr(39) + "!" + chr(39)) >= 2,
      "both the rows and the stats query")

print()
print("[the new fields survive every hand-written list]")
# This codebase loses fields to allowlists repeatedly: the schedule upsert
# rebuilds the row column by column, and the UI sanitiser rebuilds the object
# field by field. A field missing from either saves cleanly and then vanishes.
RS = io.open(os.path.join(ROOT, "backend", "app", "services", "reports_store.py"),
             encoding="utf-8", errors="replace").read()
_ins = RS.split("INSERT INTO scheduled_reports (", 1)[1]
_cols = _ins.split(")", 1)[0]
_n_cols = len([c for c in _cols.replace(chr(10), " ").split(",") if c.strip()])
_n_vals = _ins.split("VALUES (", 1)[1].split(")", 1)[0].count("?")
check("the insert's columns and placeholders match", _n_cols == _n_vals,
      "%d columns vs %d placeholders - every save would 500" % (_n_cols, _n_vals))
for field in ("segment_tag", "segment_gateway"):
    check("  %-16s is in BOTH save statements" % field,
          RS.count(field) >= 4,
          "present %d time(s); update+insert+params+read = 4" % RS.count(field))
check("  and is read back out", '"segment_tag": _row_text' in RS)
check("cycle_segments_json is migrated",
      'cycle_segments_json' in io.open(
          os.path.join(ROOT, "backend", "app", "services", "app_store.py"),
          encoding="utf-8", errors="replace").read(),
      "the column would not exist and set_cycle_state would throw")

APP = io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
              encoding="utf-8", errors="replace").read()
check("the UI sanitiser keeps segment_tag",
      "segment_tag: String(s.segment_tag" in APP,
      "sanitizeScheduledReports rebuilds the row and drops what it does not name")
check("  and the schedule form offers it", "scheduleForm.segment_tag" in APP)

DES = io.open(os.path.join(ROOT, "frontend", "src", "components", "Reports",
                           "ReportTemplateDesigner.jsx"), encoding="utf-8",
              errors="replace").read()
check("the designer offers a run window", '{ value: "run"' in DES)
check("  and a segment window", '{ value: "segment"' in DES)
check("  and the Test summary table", 'type: "segment_summary"' in DES)
check("  and repeat-per-cycle", "repeat_for" in DES)
# Checked by MECHANISM, not by the button prose - the labels were shortened
# on 2026-10-01 and an assertion on the old wording failed while the feature
# was perfectly fine.
# 2026-10-11: each side became a LIST of conditions (TriggerSide), so the
# single-dict write it used to assert is gone. The mechanism is the same -
# the picker still writes through to section.trigger - so assert that.
check("a section can pick its own start/stop trigger from gateway tags",
      "function TriggerPicker(" in DES
      and "function TriggerSide(" in DES
      and 'side="start"' in DES and 'side="stop"' in DES,
      "the reported gap: no per-section trigger options at all")
check("  the pickers list the gateway's tags",
      DES.split("function TriggerPicker(", 1)[1][:1600].count("tagList(") >= 1)
check("  and the trigger reaches the section",
      "const setTrigger = (next) => onChange({ trigger: next });" in DES)
check("  a side still writes its FIRST condition to the old key",
      "[side]: clean[0] || null," in DES,
      "a template edited here must still resolve in an older build, which "
      "reads trigger.start / trigger.stop and knows nothing of the list")
check("the Testing Report preset exists",
      'value: "tcu_testing_report"' in DES and "function tcuTestingReport(" in DES)
for want in ("Part details", "segment_summary", "Alarm log", "Telemetry",
             "Heating phase detail", "Cooling phase detail"):
    check("  it includes %-22s" % want, want in DES)
check("  its per-phase sections carry triggers",
      DES.count("tcuPhaseTrigger(") >= 3,
      "the phases must each narrow their own window")
check("  cycles are summarised and averaged",
      'stream: "cycle"' in DES and 'footer_label: "Final result"' in DES,
      "the average of the cycles IS the final test result")
check("the KPI grid finally has a range control",
      DES.split('type === "kpi_grid"', 1)[1][:1200].count("TIME_PRESETS") >= 1,
      "it carried a time_range that the UI never let anyone set")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
