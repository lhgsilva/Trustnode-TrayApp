# -*- coding: utf-8 -*-
"""Multi-condition section triggers, and a cycle that re-opens on itself.

2026-10-11, requested:

  "we have safe guards to make sure the conditions are correct picked, for
   exemple when a condifition is start rigger =1 but this keep on until is
   off, also we should have more complext condition or for start and edn
   trigger including multpli conditions"

  "we need to make sure the data will be collected within the triggers the
   main schedule report start and end trigger when the report section do no
   clear do not use different trigger, make sure the section trigger also
   consider multiples conditions and all collect the data within that time
   window and never looses"

Three defects:

  1. A SECTION trigger took exactly ONE start condition and ONE stop
     condition (`trigger["start"]` was a single dict), so a section could not
     say "while Cycle_Active = 1 AND Phase = Heating" - it had to pick one and
     chart the wrong window.

  2. A section whose trigger never fired rendered as an impossible window,
     so the section printed "No data points" with no explanation. It now
     falls back to the window that ENCLOSES it - the schedule's own triggered
     window - and the caption says the window is not the section's own.

  3. The SCHEDULE's cycle start was level-triggered. A cycle closed while the
     start bit was still 1, so the very next 15s tick found a satisfying
     reading in history and opened another cycle: one physical run producing
     a report every quarter minute.

Runs the shipping code against a fake fetcher and a fake store - no PLC, no
historian, and nothing written to the live workspace.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
_TMP = tempfile.mkdtemp(prefix="tn-trigcond-")
os.environ.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=_TMP,
                  TRUSTNODE_APP_STORE_PATH=os.path.join(_TMP, "s.db"),
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)


from app.services.report_renderer import (                      # noqa: E402
    resolve_trigger_window, apply_section_triggers, _trigger_conditions, _NEVER)

print("TrustNode - multi-condition triggers and cycle edges")
print()

# ----------------------------------------------------------------- fixtures
# A run where the cycle bit and the phase name overlap only partly. Only
# 10:04..10:06 has BOTH Cycle_Active=1 and Phase="Heating".
#
#   time    Cycle_Active   Phase
#   10:00   0              Idle
#   10:02   0              Heating      <- phase first, cycle not yet
#   10:04   1              Heating      <- both: the real start
#   10:06   1              Cooling      <- phase changed: AND broken
#   10:08   0              Cooling      <- cycle dropped too
#   10:10   0              Idle
READINGS = {
    ("gw-1", "Cycle_Active"): [
        ("2026-10-11 10:00:00", 0.0), ("2026-10-11 10:02:00", 0.0),
        ("2026-10-11 10:04:00", 1.0), ("2026-10-11 10:06:00", 1.0),
        ("2026-10-11 10:08:00", 0.0), ("2026-10-11 10:10:00", 0.0),
    ],
    ("gw-1", "Phase"): [
        ("2026-10-11 10:00:00", "Idle"), ("2026-10-11 10:02:00", "Heating"),
        ("2026-10-11 10:04:00", "Heating"), ("2026-10-11 10:06:00", "Cooling"),
        ("2026-10-11 10:08:00", "Cooling"), ("2026-10-11 10:10:00", "Idle"),
    ],
    # Deliberately sampled on the ODD minutes: no stamp here coincides with
    # a Cycle_Active stamp, which is what makes the sample-and-hold load
    # bearing rather than incidental.
    ("gw-1", "Door_Closed"): [
        ("2026-10-11 10:01:00", 1.0), ("2026-10-11 10:05:00", 1.0),
        ("2026-10-11 10:09:00", 0.0),
    ],
    ("gw-1", "Temp"): [
        ("2026-10-11 10:00:00", 20.0), ("2026-10-11 10:02:00", 45.0),
        ("2026-10-11 10:04:00", 62.0), ("2026-10-11 10:06:00", 81.0),
        ("2026-10-11 10:08:00", 90.0), ("2026-10-11 10:10:00", 40.0),
    ],
}
OUTER = ("2026-10-11 10:00:00", "2026-10-11 10:10:00")


def fake_fetch(cond, frm, to):
    key = (str(cond.get("gateway_id") or ""), str(cond.get("tag_name") or ""))
    out = []
    for ts, val in READINGS.get(key, []):
        if (not frm or ts >= frm) and (not to or ts <= to):
            out.append({"ts_utc": ts,
                        "value": val if isinstance(val, (int, float)) else None,
                        "value_text": None if isinstance(val, (int, float)) else val})
    return out


def cond(tag, operator, value, gw="gw-1"):
    return {"gateway_id": gw, "tag_name": tag, "operator": operator, "value": value}


CYCLE_ON = cond("Cycle_Active", "==", 1)
HEATING = cond("Phase", "==", "Heating")
HOT = cond("Temp", ">=", 80)

# ---------------------------------------------------- the shapes accepted
print("[both the old and the new shape are read]")
one, logic = _trigger_conditions({"start": CYCLE_ON}, "start")
check("a single dict is one condition", one == [CYCLE_ON] and logic == "all",
      "every template already saved carries this shape")
many, logic2 = _trigger_conditions(
    {"start_conditions": [CYCLE_ON, HEATING], "start_logic": "any"}, "start")
check("  a list is read with its logic", len(many) == 2 and logic2 == "any")
check("  a list wins over a leftover single dict",
      _trigger_conditions({"start": HOT, "start_conditions": [CYCLE_ON]},
                          "start")[0] == [CYCLE_ON],
      "the editor writes both, so the list must be authoritative")
check("  a condition with no tag is ignored",
      _trigger_conditions({"start_conditions": [{"operator": "==", "value": 1}]},
                          "start")[0] == [],
      "a half-filled row must not silently become a trigger")
check("  nonsense logic falls back to ALL",
      _trigger_conditions({"start_conditions": [CYCLE_ON, HEATING],
                           "start_logic": "maybe"}, "start")[1] == "all")

# --------------------------------------------------- one condition: unchanged
print()
print("[one condition behaves exactly as it did]")
check("the cycle bit opens and closes the window",
      resolve_trigger_window({"start": CYCLE_ON}, *OUTER, fetch=fake_fetch)
      == ("2026-10-11 10:04:00", "2026-10-11 10:08:00"))
check("  a separate stop tag can close it",
      resolve_trigger_window({"start": CYCLE_ON, "stop": HOT}, *OUTER, fetch=fake_fetch)
      == ("2026-10-11 10:04:00", "2026-10-11 10:06:00"))
check("  a text phase still works",
      resolve_trigger_window({"start": HEATING}, *OUTER, fetch=fake_fetch)
      == ("2026-10-11 10:02:00", "2026-10-11 10:06:00"))
check("  no trigger leaves the section alone",
      resolve_trigger_window({}, *OUTER, fetch=fake_fetch) == OUTER)

# ------------------------------------------------------- AND across two tags
print()
print("[ALL: both conditions must hold at the same moment]")
w_and = resolve_trigger_window(
    {"start_conditions": [CYCLE_ON, HEATING], "start_logic": "all"},
    *OUTER, fetch=fake_fetch)
check("the window opens only when BOTH are true",
      w_and[0] == "2026-10-11 10:04:00",
      "Phase went Heating at 10:02 but the cycle bit was still 0, so a "
      "single-condition trigger started two minutes early: %s" % (w_and,))
check("  and closes when either stops being true",
      w_and[1] == "2026-10-11 10:06:00",
      "Phase changed to Cooling at 10:06 while the cycle bit stayed 1: %s" % (w_and,))
# Cycle_Active is sampled on the even minutes and Door_Closed on the odd
# ones, so the two never share a timestamp. Each entry on the merged clock
# must carry the LAST KNOWN value of every condition, or "all" is satisfied
# at no moment at all and the window silently falls back to the whole run.
w_hold = resolve_trigger_window(
    {"start_conditions": [CYCLE_ON, cond("Door_Closed", "==", 1)],
     "start_logic": "all"}, *OUTER, fetch=fake_fetch)
check("  a reading is HELD between samples",
      w_hold == ("2026-10-11 10:04:00", "2026-10-11 10:08:00"),
      "two tags are almost never written in the same millisecond: without "
      "sample-and-hold 'all' is true at no single timestamp and the whole "
      "run is charted instead: %s" % (w_hold,))

# -------------------------------------------------------- OR across two tags
print()
print("[ANY: the first to hold opens the window]")
w_or = resolve_trigger_window(
    {"start_conditions": [CYCLE_ON, HEATING], "start_logic": "any"},
    *OUTER, fetch=fake_fetch)
check("it opens at the earlier of the two", w_or[0] == "2026-10-11 10:02:00", w_or)
check("  and closes when neither holds any more",
      w_or[1] == "2026-10-11 10:08:00",
      "Cycle_Active was still 1 at 10:06, so ANY was still satisfied: %s" % (w_or,))

# --------------------------------------------------- multi-condition STOP
print()
print("[a stop side takes several conditions too]")
w_stop = resolve_trigger_window(
    {"start": CYCLE_ON,
     "stop_conditions": [HOT, cond("Phase", "==", "Cooling")],
     "stop_logic": "all"},
    *OUTER, fetch=fake_fetch)
check("ALL: it waits for both stop conditions",
      w_stop == ("2026-10-11 10:04:00", "2026-10-11 10:06:00"), w_stop)
w_stop_any = resolve_trigger_window(
    {"start": HEATING,
     "stop_conditions": [cond("Temp", ">=", 200), cond("Phase", "==", "Cooling")],
     "stop_logic": "any"},
    *OUTER, fetch=fake_fetch)
check("  ANY: an unreachable stop does not block the other",
      w_stop_any == ("2026-10-11 10:02:00", "2026-10-11 10:06:00"), w_stop_any)
check("  a stop that never fires keeps the data to the outer end",
      resolve_trigger_window(
          {"start": CYCLE_ON, "stop_conditions": [cond("Temp", ">=", 500)]},
          *OUTER, fetch=fake_fetch) == ("2026-10-11 10:04:00", OUTER[1]),
      "closing early would DROP every reading taken since the start - "
      "'never looses' was the requirement")

# ------------------------------------------------- the unresolved fallback
print()
print("[a trigger that never fires uses the report's own window]")
never = {"start_conditions": [cond("Phase", "==", "Vacuum")]}
check("it falls back to the enclosing window",
      resolve_trigger_window(never, *OUTER, fetch=fake_fetch) == OUTER,
      "it must not invent a different range, and it must not go blank")
check("  ALL with one unreachable condition falls back too",
      resolve_trigger_window(
          {"start_conditions": [CYCLE_ON, cond("Temp", ">=", 900)],
           "start_logic": "all"}, *OUTER, fetch=fake_fetch) == OUTER)
check("  on_miss='empty' still excludes everything",
      resolve_trigger_window(dict(never, on_miss="empty"), *OUTER,
                             fetch=fake_fetch)[0].startswith("9999"),
      "for a section documenting one OPTIONAL phase, blank is honest")

print()
print("[and the section is MARKED, so the report does not lie]")
tpl = {"definition": {"sections": [
    {"id": "a", "type": "line_chart", "title": "Heating phase",
     "time_range": {"preset": "custom", "from_utc": OUTER[0], "to_utc": OUTER[1]},
     "trigger": {"start_conditions": [cond("Phase", "==", "Vacuum")]}},
    {"id": "b", "type": "line_chart", "title": "Whole run",
     "time_range": {"preset": "custom", "from_utc": OUTER[0], "to_utc": OUTER[1]},
     "trigger": {"start_conditions": [CYCLE_ON, HEATING], "start_logic": "all"}},
    {"id": "c", "type": "line_chart", "title": "No trigger",
     "time_range": {"preset": "custom", "from_utc": OUTER[0], "to_utc": OUTER[1]}},
]}}
out = apply_section_triggers(tpl, fetch=fake_fetch)
secs = {s["id"]: s for s in out["definition"]["sections"]}
check("the fallen-back section is flagged",
      secs["a"].get("_trigger_fell_back") is True,
      "a chart of the whole run under a heading that says 'Heating phase' "
      "is a wrong report unless it says so")
check("  a resolved trigger is NOT flagged",
      secs["b"].get("_trigger_fell_back") is False
      and secs["b"]["time_range"]["from_utc"] == "2026-10-11 10:04:00",
      secs["b"]["time_range"])
check("  a section with no trigger is untouched",
      "_trigger_fell_back" not in secs["c"])

print()
print("[a row's children are still resolved]")
row_tpl = {"definition": {"sections": [
    {"id": "r", "type": "row", "columns": [
        {"id": "r1", "type": "line_chart",
         "time_range": {"preset": "custom", "from_utc": OUTER[0], "to_utc": OUTER[1]},
         "trigger": {"start_conditions": [CYCLE_ON, HEATING], "start_logic": "all"}},
    ]},
]}}
kid = apply_section_triggers(row_tpl, fetch=fake_fetch)[
    "definition"]["sections"][0]["columns"][0]
check("a trigger inside a row narrows its own window",
      kid["time_range"]["from_utc"] == "2026-10-11 10:04:00",
      "two charts sharing a line both rendered the whole run until "
      "2026-10-01; a multi-condition trigger must not undo that")

# ======================================================================= #
# the SCHEDULE's cycle: a start bit that stays high
# ======================================================================= #
print()
print("[a cycle must not re-open while its start bit is still high]")

from app.services.report_scheduler import ReportScheduler         # noqa: E402


class FakeStore:
    """Just enough of ReportsStore for _consider_cycle."""

    def __init__(self):
        self.row = {
            "id": "sch-1", "name": "Cycle", "template_id": "t1", "enabled": 1,
            "trigger_mode": "cycle", "tag_conditions": [CYCLE_ON],
            "condition_logic": "all", "stop_conditions": [HOT],
            "stop_condition_logic": "all", "cycle_started_utc": "",
            "cycle_report_id": "", "cycle_segments": [], "segment_tags": [],
            "start_edge": True,
        }

    def list_enabled_schedules_all_tenants(self):
        return [dict(self.row)]

    def set_cycle_state(self, sched_id, started, report_id, segments_json="[]"):
        self.row["cycle_started_utc"] = started
        self.row["cycle_report_id"] = report_id

    def mark_schedule_run(self, **kw):
        pass


class FakeRunner:
    def __init__(self):
        self.runs = []

    def run(self, schedule, **kw):
        self.runs.append(kw.get("window"))
        return {"ok": True}


def build(live_value, history):
    store = FakeStore()
    runner = FakeRunner()
    sched = ReportScheduler(
        runner, store,
        live_lookup=lambda gw, tag: live_value.get(tag),
        history_lookup=lambda gw, tag, since, to: history.get(tag, []),
        email_settings_lookup=lambda: None)
    return store, runner, sched


# The bit goes high and the temperature crosses 80 in the SAME tick, so the
# cycle opens and closes - and the bit is still high afterwards.
live = {"Cycle_Active": 1.0, "Temp": 95.0}
hist = {"Cycle_Active": [("2026-10-11 11:00:00", 1.0)],
        "Temp": [("2026-10-11 11:00:30", 95.0)]}
store, runner, sched = build(live, hist)
t = datetime(2026, 10, 11, 11, 0, 0, tzinfo=timezone.utc)
for i in range(6):
    sched._consider_cycle(store.list_enabled_schedules_all_tenants()[0],
                          t + timedelta(seconds=15 * i))
check("one physical run produced ONE report", len(runner.runs) == 1,
      "level-triggered, every 15s tick re-opened the cycle: %d report(s) - "
      "%s" % (len(runner.runs), runner.runs))
check("  and the schedule is left waiting, not open",
      store.row["cycle_started_utc"] == "", store.row["cycle_started_utc"])

# Now the bit drops. The NEXT rise must open a new cycle.
live["Cycle_Active"] = 0.0
sched._consider_cycle(store.list_enabled_schedules_all_tenants()[0],
                      t + timedelta(minutes=2))
check("  dropping the bit alone does not start one",
      len(runner.runs) == 1 and store.row["cycle_started_utc"] == "")
live["Cycle_Active"] = 1.0
hist["Cycle_Active"] = [("2026-10-11 11:03:00", 1.0)]
hist["Temp"] = []
sched._consider_cycle(store.list_enabled_schedules_all_tenants()[0],
                      t + timedelta(minutes=3))
check("  but the next RISE does", store.row["cycle_started_utc"] != "",
      "the guard must not wedge the schedule shut: %r"
      % store.row["cycle_started_utc"])

# Opting out restores the old behaviour, for a start tag the PLC self-clears.
store2, runner2, sched2 = build({"Cycle_Active": 1.0, "Temp": 95.0},
                                {"Cycle_Active": [("2026-10-11 11:00:00", 1.0)],
                                 "Temp": [("2026-10-11 11:00:30", 95.0)]})
store2.row["start_edge"] = False
for i in range(4):
    sched2._consider_cycle(store2.list_enabled_schedules_all_tenants()[0],
                           t + timedelta(seconds=15 * i))
check("start_edge=false keeps the level-triggered behaviour",
      len(runner2.runs) > 1,
      "a momentary pulse cleared by the PLC needs the old behaviour: "
      "%d report(s)" % len(runner2.runs))

print()
print("[the flag survives the save path]")
# The upsert rebuilds the row column by column: a field missing from either
# statement saves cleanly and then vanishes. That has happened twice before.
import io as _io                                                  # noqa: E402
RS = _io.open(os.path.join(ROOT, "backend", "app", "services", "reports_store.py"),
              encoding="utf-8").read()
check("start_edge is in BOTH save statements",
      RS.count("start_edge") >= 5,
      "update + insert columns + both parameter tuples + read-back = 5; "
      "found %d" % RS.count("start_edge"))
check("  and is read back out", '"start_edge": _row_flag("start_edge", True)' in RS)
AS = _io.open(os.path.join(ROOT, "backend", "app", "services", "app_store.py"),
              encoding="utf-8").read()
check("  the column is migrated", '("start_edge", "INTEGER NOT NULL DEFAULT 1")' in AS,
      "without the ALTER the column does not exist and every save would 500")
SRM = _io.open(os.path.join(ROOT, "frontend", "src", "components", "Reports",
                            "ScheduledReportsManager.jsx"), encoding="utf-8").read()
check("  and the form offers it", "start_edge: e.target.checked" in SRM)
APP = _io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
               encoding="utf-8").read()
check("  the UI sanitiser keeps it", "start_edge" in APP,
      "sanitizeScheduledReports rebuilds the row and drops what it does not "
      "name - the same allowlist trap as segment_tag")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
