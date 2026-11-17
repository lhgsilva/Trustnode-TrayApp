# -*- coding: utf-8 -*-
"""Every start / stop trigger shape, in the schedule's cycle and in report sections.

2026-11-17, from the owner: "a problem to identify rising up and down of the
signal of triggers ... start when it is 1 ... an option if the start went to
0 before the end trigger ... all options covered, including one tag to start
and stop, and a signal staying 1 or only a pulse ... we cannot have a report
fail to generate ... as soon as it sees a trigger to start it should show on
the generated page with status Processing", and "make sure the report module
and pdf generation, using the section triggers and report trigger, will be
correct". Also: the "section trigger did not fire; showing the report window"
note is not to be shown.

    python scripts/test_trigger_modes.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="tn_trig_")
os.environ.update(TRUSTNODE_APP_STORE_PATH=os.path.join(_TMP, "s.db"), TRUSTNODE_DATA_DIR=_TMP, TRUSTNODE_SKIP_DOTENV="1",
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_TENANT_ID="tenant-cust-trig")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "backend"))
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-80s %s%s" % (name, "PASS" if ok else "FAIL", ("  - " + detail) if (not ok and detail) else ""))


def T(i):
    return "2026-11-17 10:00:%02d" % i


def main() -> int:
    from app.services import trigger_cycles as tc

    print("[the start / stop machine]")

    def run(seq, mode, drop="ignore"):
        ev, _ = tc.walk([(T(i), s, p) for i, (s, p) in enumerate(seq)], None, mode, drop)
        return [(k, int(ts[-2:])) for k, ts in ev]

    # start held at 1, a separate stop pulse
    check("start held at 1, stop pulse at 8: one cycle 1..8",
          run([(0, 0), (1, 0), (1, 0), (1, 0), (1, 0), (1, 0), (1, 0), (1, 0), (1, 1), (1, 0)], "stop")
          == [("open", 1), ("close", 8)])
    # an end bit still 1 from the last part must not close the new one
    check("a stop still true when the start rises waits for it to go false and back to true",
          run([(0, 1), (0, 1), (1, 1), (1, 1), (1, 0), (1, 0), (1, 0), (1, 1)], "stop")
          == [("open", 2), ("close", 7)])
    # a start PULSE with a stop: the drop is ignored by default
    check("start pulse + stop pulse: runs from the start pulse to the stop pulse",
          run([(0, 0), (1, 0), (0, 0), (0, 0), (0, 0), (0, 1), (0, 0)], "stop") == [("open", 1), ("close", 5)])
    check("  'end the cycle there' when the start drops first",
          run([(0, 0), (1, 0), (1, 0), (0, 0), (0, 1)], "stop", "end") == [("open", 1), ("close", 3)])
    check("  'discard the cycle' when the start drops first (no report)",
          run([(0, 0), (1, 0), (1, 0), (0, 0), (0, 1)], "stop", "discard") == [("open", 1), ("discard", 3)])
    check("  a start that stays up while the stop comes is a normal end for every option",
          all(run([(0, 0), (1, 0), (1, 1), (0, 0)], "stop", d) == [("open", 1), ("close", 2)] for d in ("ignore", "end", "discard")))
    # one tag held while running
    check("one tag held at 1 while running: ends when it goes to 0",
          run([(0, None), (1, None), (1, None), (1, None), (0, None), (0, None)], "start_false") == [("open", 1), ("close", 4)])
    # one tag pulsed: pulse = start, next pulse = stop
    check("one pulsed tag: the first pulse starts, the next pulse ends",
          run([(0, None), (1, None), (0, None), (0, None), (1, None), (0, None), (1, None), (0, None)], "next_start")
          == [("open", 1), ("close", 4), ("open", 6)])
    check("  a pulse held over several readings counts once",
          run([(0, None), (1, None), (1, None), (0, None), (1, None), (1, None)], "next_start") == [("open", 1), ("close", 4)])
    check("after a close, the start must drop and rise again (no re-open on a held 1)",
          run([(1, 0), (1, 1), (1, 0), (1, 0), (0, 0), (1, 0)], "stop") == [("open", 0), ("close", 1), ("open", 5)])
    check("unknown options read as the defaults; nothing raises on odd input",
          tc.end_mode_for("weird", True) == "stop" and tc.end_mode_for("stop", False) == "start_false"
          and tc.drop_action_for(None) == "ignore" and tc.walk([], None, "x", "y")[0] == [])

    # ---- the schedule's cycle, end to end, with real records ---------------------------------
    print("\n[the schedule's cycle: listed as Collecting from its start, the same record becomes the report]")
    from app.state import app_store  # noqa: F401  (the store the reports store lives in)
    from app.tenant import set_current_tenant
    set_current_tenant("tenant-cust-trig")
    from app.state import reports_store
    from app.services.report_scheduler import ReportRunner, ReportScheduler
    tpl = reports_store.upsert_template({"name": "Part", "definition": {"sections": [{"type": "header", "title": "P"}]}})
    made = []

    def render(template, window=None, segments=None, file_name=None):
        p = Path(_TMP) / ("r%d.pdf" % len(made))
        p.write_bytes(b"%PDF-1.4 test")
        made.append(window)
        return p, p.stat().st_size, "sha"
    runner = ReportRunner(reports_store, render, None)
    sched = reports_store.upsert_schedule({
        "name": "Line A", "template_id": tpl["id"], "enabled": True, "trigger_mode": "cycle",
        "tag_conditions": [{"gateway_id": "gw1", "tag_name": "Start", "operator": "==", "value": 1}],
        "stop_conditions": [{"gateway_id": "gw1", "tag_name": "End", "operator": "==", "value": 1}],
        "condition_logic": "all", "stop_condition_logic": "all", "start_drop": "ignore", "cycle_end": ""})
    check("the schedule saves and reads back its trigger options",
          sched.get("cycle_end") == "" and sched.get("start_drop") == "ignore", repr({k: sched.get(k) for k in ("cycle_end", "start_drop")}))
    s2 = reports_store.upsert_schedule(dict(sched, cycle_end="next_start", start_drop="discard"))
    check("  editing them persists (both save statements)",
          s2.get("cycle_end") == "next_start" and s2.get("start_drop") == "discard")
    reports_store.upsert_schedule(dict(sched, cycle_end="", start_drop="ignore"))

    HIST: dict[str, list] = {"Start": [], "End": []}
    LIVE: dict[str, float] = {}
    sc = ReportScheduler(runner, reports_store, live_lookup=lambda gw, t: LIVE.get(t),
                         history_lookup=lambda gw, t, since, until: [r for r in HIST.get(t, []) if r[0] > since and r[0] <= until],
                         email_settings_lookup=lambda: None)
    t0 = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
    st = lambda d: d.strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
    sc._last_seen[sched["id"]] = st(t0)
    HIST["End"] = [(st(t0 + timedelta(seconds=1)), 1.0)]      # the end bit still 1 from the last part
    HIST["Start"] = [(st(t0 + timedelta(seconds=2)), 1.0)]
    LIVE.update(Start=1.0, End=1.0)
    sc._consider_cycle(reports_store.get_schedule(sched["id"]), t0 + timedelta(seconds=15))
    rows = reports_store.list_generated(schedule_id=sched["id"], limit=10)
    check("the moment the start is seen, the report is listed as Collecting",
          len(rows) == 1 and rows[0].get("render_state") == "collecting" and (rows[0].get("meta") or {}).get("window_from") == st(t0 + timedelta(seconds=2)),
          repr([(r.get("render_state"), (r.get("meta") or {}).get("window_from")) for r in rows]))
    check("  and the stale end bit did not close it", reports_store.get_schedule(sched["id"]).get("cycle_started_utc") != "" and not made)
    HIST["End"] += [(st(t0 + timedelta(seconds=20)), 0.0), (st(t0 + timedelta(seconds=25)), 1.0)]
    LIVE.update(End=1.0)
    sc._consider_cycle(reports_store.get_schedule(sched["id"]), t0 + timedelta(seconds=30))
    rows = reports_store.list_generated(schedule_id=sched["id"], limit=10)
    check("the end bit rising closes it: the SAME record becomes the ready report, with its window",
          len(rows) == 1 and rows[0].get("render_state") == "ready"
          and (rows[0].get("meta") or {}).get("window_to") == st(t0 + timedelta(seconds=25)) and made == [(st(t0 + timedelta(seconds=2)), st(t0 + timedelta(seconds=25)))],
          repr([(r.get("render_state"), (r.get("meta") or {}).get("window_to")) for r in rows]) + " made=%r" % made)

    # discard
    reports_store.upsert_schedule(dict(reports_store.get_schedule(sched["id"]), start_drop="discard"))
    HIST["Start"] += [(st(t0 + timedelta(seconds=40)), 0.0), (st(t0 + timedelta(seconds=41)), 1.0), (st(t0 + timedelta(seconds=50)), 0.0)]
    sc._consider_cycle(reports_store.get_schedule(sched["id"]), t0 + timedelta(seconds=55))
    rows = reports_store.list_generated(schedule_id=sched["id"], limit=10)
    states = sorted(r.get("render_state") for r in rows)
    check("'discard': a start dropping before the end cancels that cycle's record, no report made",
          states == ["cancelled", "ready"] and len(made) == 1, repr(states))

    # a long cycle is not "failed" when it becomes pending
    rec = reports_store.insert_generated({"tenant_id": "tenant-cust-trig", "template_id": tpl["id"], "template_name": "Part",
                                          "schedule_id": "x", "schedule_name": "x", "triggered_by": "cycle", "file_path": "",
                                          "file_name": "", "file_bytes": 0, "file_sha256": "",
                                          "meta": {"render_state": "collecting"}})
    import sqlite3
    con = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
    con.execute("UPDATE generated_reports SET created_utc = ? WHERE id = ?",
                ((datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S.000"), rec["id"]))
    con.commit(); con.close()
    reports_store.update_generated_render(rec["id"], render_state="pending")
    got = reports_store.get_generated(rec["id"])
    check("a 3-hour cycle turning 'pending' is pending, not 'failed' (the hour counts from pending)",
          got.get("render_state") == "pending", repr(got.get("render_state")))
    n_failed = reports_store.fail_orphaned_pending()
    still = reports_store.list_generated(schedule_id=sched["id"], limit=10)
    check("  and the boot sweep of orphaned 'pending' leaves 'collecting' alone",
          all(r.get("render_state") != "failed" for r in still), "swept %s" % n_failed)

    # ---- section triggers --------------------------------------------------------------------
    print("\n[section triggers inside the report]")
    from app.services import report_renderer as rr

    def fetch_from(series):
        def fetch(cond, a, b):
            return [{"ts_utc": ts, "value": v} for ts, v in series.get(cond.get("tag_name"), [])]
        return fetch
    W0, W1 = T(0), "2026-11-17 10:01:00"
    trig = {"start": {"tag_name": "Heat", "operator": "==", "value": 1}, "stop": {"tag_name": "Done", "operator": "==", "value": 1}}
    data = {"Heat": [(T(1), 0), (T(5), 1), (T(30), 1)], "Done": [(T(0), 1), (T(10), 1), (T(20), 0), (T(40), 1)]}
    check("a section's stop already true at its start waits for false -> true (was: ended at the next reading)",
          rr.resolve_trigger_window(trig, W0, W1, fetch_from(data)) == (T(5), T(40)), repr(rr.resolve_trigger_window(trig, W0, W1, fetch_from(data))))
    pulses = {"Run": [(T(1), 0), (T(3), 1), (T(4), 0), (T(20), 1), (T(21), 0)]}
    one = {"start": {"tag_name": "Run", "operator": "==", "value": 1}, "end": "next_start"}
    check("a section on one pulsed tag: first pulse to the next pulse",
          rr.resolve_trigger_window(one, W0, W1, fetch_from(pulses)) == (T(3), T(20)), repr(rr.resolve_trigger_window(one, W0, W1, fetch_from(pulses))))
    held = {"Run": [(T(1), 0), (T(3), 1), (T(9), 1), (T(12), 0)]}
    check("a section on one held tag: while it is 1",
          rr.resolve_trigger_window({"start": {"tag_name": "Run", "operator": "==", "value": 1}}, W0, W1, fetch_from(held)) == (T(3), T(12)))
    skip = {"Heat": [(T(1), 1), (T(2), 0), (T(10), 1), (T(30), 1)], "Done": [(T(0), 0), (T(5), 0), (T(25), 1)]}
    check("a section with 'discard': a start that dropped before the stop is skipped, the next run is used",
          rr.resolve_trigger_window(dict(trig, start_drop="discard"), W0, W1, fetch_from(skip)) == (T(10), T(25)),
          repr(rr.resolve_trigger_window(dict(trig, start_drop="discard"), W0, W1, fetch_from(skip))))
    check("a section whose trigger never fires covers the report window (and no note is printed)",
          rr.resolve_trigger_window(trig, W0, W1, fetch_from({"Heat": [(T(1), 0)], "Done": []})) == (W0, W1)
          and 'warnings.append("section trigger did not fire' not in open(rr.__file__, encoding="utf-8").read())
    bad = rr.resolve_trigger_window(trig, W0, W1, lambda c, a, b: (_ for _ in ()).throw(RuntimeError("store down")))
    check("a section trigger whose readings cannot be read never fails the report", bad == (W0, W1), repr(bad))

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
