# -*- coding: utf-8 -*-
"""Short test cycles never merge, and a gated stop signal is still seen.

2026-11-16, from the part-test customer: parts are tested one after another,
some shorter than the report scheduler's 15 s tick; collection is gated on
"test running", so the 0 that ends a test is never STORED.

  * two parts inside one tick give two cycle reports with their own windows,
    stamped to the readings (they merged or the second was missed);
  * a part that starts and ends inside one tick is reported (it was missed);
  * a stop condition whose value was never stored (gated) closes from the
    live value;
  * the scheduler's live lookup and the batch watcher read the collection
    gate's own cache when it is newer than the last stored value.

    python scripts/test_short_cycles_and_gated_stop.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

_TMP = tempfile.mkdtemp(prefix="tn_shortcyc_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-cyc"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "backend"))
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-70s %s%s" % (name, "PASS" if ok else "FAIL", ("  - " + detail) if (not ok and detail) else ""))


def st(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    print("short cycles and a gated stop")
    from app.services.report_scheduler import ReportScheduler

    class _Store:
        def __init__(self):
            self.state = {}

        def set_cycle_state(self, schedule_id, started_utc, report_id="", segments_json=None):
            self.state[schedule_id] = (started_utc, report_id)

        def mark_schedule_run(self, **kw):
            pass

    class _Runner:
        def __init__(self):
            self.calls = []

        def run(self, schedule, *, triggered_by="manual", email_settings=None, window=None, segments=None, report_id=""):
            self.calls.append({"window": window, "report_id": report_id})
            return {"ok": True}

    T0 = datetime(2026, 11, 16, 10, 0, 0, tzinfo=timezone.utc)
    HIST = {}
    LIVE = {}

    def history(gw, tag, since, until):
        rows = HIST.get(tag, [])
        return [(ts, v) for ts, v in rows if (not since or ts > since) and ts <= until]

    def make(schedule_extra=None):
        store, runner = _Store(), _Runner()
        s = ReportScheduler(runner=runner, reports_store=store, live_lookup=lambda gw, t: LIVE.get(t),
                            history_lookup=history, email_settings_lookup=lambda: None,
                            is_any_gateway_running=lambda: True)
        sched = {"id": "sch", "trigger_mode": "cycle",
                 "tag_conditions": [{"tag_name": "Run", "operator": "==", "value": 1}], "condition_logic": "all",
                 "stop_conditions": [], "stop_condition_logic": "all",
                 "cycle_started_utc": "", "cycle_report_id": "", "cycle_segments": []}
        sched.update(schedule_extra or {})
        return s, store, runner, sched

    # ---- A. two parts inside one 15 s tick --------------------------------------
    HIST.clear(); LIVE.clear()
    HIST["Run"] = [(st(T0 + timedelta(seconds=1)), 1), (st(T0 + timedelta(seconds=4)), 0),
                   (st(T0 + timedelta(seconds=6)), 1), (st(T0 + timedelta(seconds=9)), 0)]
    LIVE["Run"] = 0
    s, store, runner, sched = make()
    s._last_seen["sch"] = st(T0)
    s._consider_cycle(sched, T0 + timedelta(seconds=15))
    wins = [c["window"] for c in runner.calls]
    check("two parts inside one tick give two reports, stamped to the readings",
          wins == [(st(T0 + timedelta(seconds=1)), st(T0 + timedelta(seconds=4))),
                   (st(T0 + timedelta(seconds=6)), st(T0 + timedelta(seconds=9)))], repr(wins))
    check("  and no cycle is left open", store.state.get("sch", ("", ""))[0] == "", repr(store.state))

    # ---- B. a part that starts and ends inside one tick --------------------------
    HIST.clear(); LIVE.clear()
    HIST["Run"] = [(st(T0 + timedelta(seconds=2)), 1), (st(T0 + timedelta(seconds=7)), 0)]
    LIVE["Run"] = 0
    s, store, runner, sched = make()
    s._last_seen["sch"] = st(T0)
    s._consider_cycle(sched, T0 + timedelta(seconds=15))
    wins = [c["window"] for c in runner.calls]
    check("a 5-second part is reported with its own window",
          wins == [(st(T0 + timedelta(seconds=2)), st(T0 + timedelta(seconds=7)))], repr(wins))

    # ---- C. the next tick: a third part, and nothing is reported twice -------------
    HIST["Run"] += [(st(T0 + timedelta(seconds=20)), 1), (st(T0 + timedelta(seconds=26)), 0)]
    s._consider_cycle(sched, T0 + timedelta(seconds=30))
    wins = [c["window"] for c in runner.calls]
    check("the next tick reports only the new part", len(wins) == 2 and wins[1] == (st(T0 + timedelta(seconds=20)), st(T0 + timedelta(seconds=26))), repr(wins))

    # ---- D. a gated stop: the 0 was never stored ----------------------------------
    HIST.clear(); LIVE.clear()
    HIST["Run"] = [(st(T0 + timedelta(seconds=1)), 1)]       # the 0 is never written (collection gated on Run)
    LIVE["Run"] = 0                                          # but the gate reads it
    s, store, runner, sched = make({"stop_conditions": [{"tag_name": "Run", "operator": "==", "value": 0}]})
    s._last_seen["sch"] = st(T0)
    s._consider_cycle(sched, T0 + timedelta(seconds=15))
    check("a cycle opened from history", store.state.get("sch", ("",))[0] == st(T0 + timedelta(seconds=1)) or runner.calls, repr(store.state))
    s._consider_cycle(sched, T0 + timedelta(seconds=30))
    wins = [c["window"] for c in runner.calls]
    check("  and closes from the LIVE stop value although no 0 was stored",
          len(wins) == 1 and wins[0][0] == st(T0 + timedelta(seconds=1)), repr(wins))

    # ---- E. the gate's cache answers the scheduler and the batch watcher ---------
    from app.state import app_store, plc_manager
    from app import state as _st
    from app.tenant import set_current_tenant
    set_current_tenant("tenant-cust-cyc")
    now = time.time()
    with app_store._live_cache_lock:
        app_store._local_live_latest_cache[("tenant-cust-cyc", "gw1", "Run")] = {
            "value": 1.0, "ts_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - 30)) + ".000"}
    plc_manager.global_live_values["gw1::Run"] = {"value": 0.0, "ts_epoch": now}
    check("the scheduler's live value is the gate's newer 0, not the stored 1", _st._scheduler_live_lookup("gw1", "Run") == 0.0,
          repr(_st._scheduler_live_lookup("gw1", "Run")))
    plc_manager.global_live_values["gw1::Run"] = {"value": 0.0, "ts_epoch": now - 120}
    check("  and the stored value when the gate's is older", _st._scheduler_live_lookup("gw1", "Run") == 1.0,
          repr(_st._scheduler_live_lookup("gw1", "Run")))
    plc_manager.global_live_values["gw1::Run"] = {"value": 0.0, "ts_epoch": now}
    app_store.append_historian_rows([{"ts_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - 30)) + ".000", "gateway_id": "gw1",
                                      "gateway_name": "PLC", "device_name": "d", "plc_ip": "1.1.1.1", "database_name": "db",
                                      "tag_name": "Run", "value": 1.0, "quality": 192, "quality_label": "GOOD", "source": "t"}])
    from app.modules.batch_management.triggers import _latest_values
    check("the batch watcher sees the gate's 0 too", _latest_values(["Run"]).get("Run") == 0.0, repr(_latest_values(["Run"])))

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
