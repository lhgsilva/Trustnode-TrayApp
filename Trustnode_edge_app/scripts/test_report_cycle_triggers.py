# -*- coding: utf-8 -*-
"""A cycle produces ONE report, covering exactly the cycle.

2026-09-29, requested: "generated reports based on start/stop conditions based
on PLC tags, for example start and end cycle, and based on the start and end
time of these triggers collect all information about tags, KPIs and charts
created in the report template. So every time we have a new start and end time
a new report is generated automatically." Plus: a tag carrying a unique ID to
name and filter the report, and a custom file name instead of a UUID.

What existed before: a tag trigger that was LEVEL-based and evaluated once a
minute. It fired a report while the condition was true - sixty reports for an
hour-long cycle - and the report's content came from the TEMPLATE's own range
(24h, custom, or a batch), never from the trigger. So a cycle that ran from
09:00 to 09:12 produced a report of the last twenty-four hours.

The three pieces are tested together because they share one mechanism: the
window, the captured id and the file name all come from the cycle.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ.setdefault("TRUSTNODE_SKIP_DOTENV", "1")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


from app.services.report_renderer import (                 # noqa: E402
    apply_window_override, build_report_file_name)
from app.services.report_scheduler import ReportScheduler  # noqa: E402

print("TrustNode - cycle-triggered reports")
print()
print("[the report covers the CYCLE, not the template's own range]")
tpl = {"definition": {"sections": [
    {"type": "chart", "tag_name": "Temp", "time_range": {"preset": "24h"}},
    {"type": "table", "tag_name": "Temp", "time_range": {"preset": "7d"}},
]}}
out = apply_window_override(tpl, "2026-09-29 09:00:00", "2026-09-29 09:12:00")
ranges = [s.get("time_range") for s in out["definition"]["sections"]]
check("every section is pinned to the cycle window",
      all(r.get("preset") == "custom" and r.get("from_utc") == "2026-09-29 09:00:00"
          and r.get("to_utc") == "2026-09-29 09:12:00" for r in ranges),
      ranges)
check("  the original template is not mutated",
      tpl["definition"]["sections"][0]["time_range"]["preset"] == "24h",
      "a template edited in place would corrupt every later run")

print()
print("[the file name is the operator's, not a uuid]")
name = build_report_file_name("{template}_{id}_{date}_{time}",
                              template_name="Cycle Report", report_id="BATCH-4471",
                              started="2026-09-29 18:54:12")
check("placeholders resolve", name == "Cycle_Report_BATCH-4471_20260929_185412.pdf", name)
check("  an empty pattern keeps the uuid behaviour",
      build_report_file_name("") == "",
      "existing schedules must not change file naming")
check("  a name cannot escape the reports folder",
      "/" not in build_report_file_name("../../etc/{id}", report_id="x")
      and "\\\\" not in build_report_file_name("..\\\\..\\\\{id}", report_id="x"),
      build_report_file_name("../../etc/{id}", report_id="x"))
check("  the extension is added once",
      build_report_file_name("run.pdf").count(".pdf") == 1,
      build_report_file_name("run.pdf"))

print()
print("[the cycle opens on start and closes on stop - edge, not level]")


class _Store:
    def __init__(self):
        self.state = {}
        self.runs = []

    def set_cycle_state(self, schedule_id, started_utc, report_id="",
                        segments_json=None):
        # segments_json: the run's sub-periods, added 2026-09-30. Accepted and
        # ignored here - this test is about the cycle's own open/close edges.
        self.state[schedule_id] = (started_utc, report_id)

    def mark_schedule_run(self, **kw):
        pass


class _Runner:
    def __init__(self):
        self.calls = []

    def run(self, schedule, *, triggered_by="manual", email_settings=None,
            window=None, segments=None, report_id=""):
        self.calls.append({"window": window, "report_id": report_id,
                           "triggered_by": triggered_by})
        return {"ok": True}


LIVE = {"Cycle_Running": 0, "Cycle_Complete": 0, "Batch_Number": "SN-0001"}
store, runner = _Store(), _Runner()
sched = ReportScheduler(runner=runner, reports_store=store,
                        # (gateway, tag) - the same contract the condition evaluator uses.
                        live_lookup=lambda gw, t: LIVE.get(t),
                        email_settings_lookup=lambda: None,
                        is_any_gateway_running=lambda: True)

SCHEDULE = {
    "id": "sch-cycle", "trigger_mode": "cycle",
    "tag_conditions": [{"tag_name": "Cycle_Running", "operator": "==", "value": 1}],
    "condition_logic": "all",
    "stop_conditions": [{"tag_name": "Cycle_Complete", "operator": "==", "value": 1}],
    "stop_condition_logic": "all",
    "id_tag": "Batch_Number",
    "cycle_started_utc": "", "cycle_report_id": "",
}


def tick(minute, sch):
    sched._consider_cycle(sch, datetime(2026, 9, 29, 9, minute, 0, tzinfo=timezone.utc))


tick(0, SCHEDULE)
check("nothing happens while the start condition is false",
      store.state.get("sch-cycle") is None and not runner.calls)

LIVE["Cycle_Running"] = 1
tick(1, SCHEDULE)
opened = store.state.get("sch-cycle")
check("the start condition OPENS a cycle", bool(opened and opened[0]), opened)
check("  and captures the id tag at the START", opened and opened[1] == "SN-0001",
      "a serial read at the END may already be the next part")
check("  no report yet", not runner.calls, "a level test would have fired here")

# the condition stays true for ten more minutes
live_sched = dict(SCHEDULE, cycle_started_utc=opened[0], cycle_report_id=opened[1])
for m in range(2, 12):
    tick(m, live_sched)
check("a cycle still running produces NO reports", not runner.calls,
      "%d report(s) - the old level trigger fired one a minute" % len(runner.calls))

LIVE["Cycle_Complete"] = 1
tick(12, live_sched)
check("the stop condition closes it and generates ONE report",
      len(runner.calls) == 1, "%d call(s)" % len(runner.calls))
if runner.calls:
    call = runner.calls[0]
    check("  the report window is the cycle",
          call["window"] == ("2026-09-29 09:01:00", "2026-09-29 09:12:00"),
          call["window"])
    check("  the captured id travels with it", call["report_id"] == "SN-0001")
    check("  and it is recorded as cycle-triggered", call["triggered_by"] == "cycle")
check("  the cycle state is cleared for the next run",
      store.state.get("sch-cycle") == ("", ""), store.state.get("sch-cycle"))

print()
print("[the cycle fields survive every hand-written list they pass through]")
# This codebase loses fields to allowlists repeatedly. sanitizeScheduledReports
# clamps trigger_mode to a fixed list and rebuilds the row field by field: an
# unlisted mode is silently rewritten to "time" and the cycle fields vanish, so
# the schedule would look configured in the UI and never run as a cycle.
import io as _io                                          # noqa: E402
APP = _io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
               encoding="utf-8", errors="replace").read()
check("the UI offers cycle mode", '<option value="cycle">' in APP)
check("  the sanitiser ACCEPTS cycle",
      '["time", "tag", "both", "cycle"].includes(triggerMode)' in APP,
      "an unlisted mode is rewritten to 'time' without a word")
for field in ("stop_conditions", "stop_condition_logic", "id_tag", "file_name_pattern"):
    check("  %-20s survives the sanitiser" % field,
          APP.count(field) >= 2, "dropped on the way in or out")
check("start and stop share ONE condition editor",
      'addScheduleTagCondition("stop_conditions")' in APP
      and 'field = "tag_conditions"' in APP,
      "two editors for the same shape drift apart")

SCHED = _io.open(os.path.join(ROOT, "backend", "app", "services",
                              "reports_store.py"), encoding="utf-8",
                 errors="replace").read()
check("the store accepts cycle as a trigger mode",
      '"time", "tag", "both", "cycle"' in SCHED,
      "the backend would fall back to 'time' and the schedule would fire hourly")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
