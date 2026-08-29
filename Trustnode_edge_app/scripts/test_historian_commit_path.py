# -*- coding: utf-8 -*-
"""Nothing may stop a READ row from being COMMITTED to the local historian.

This is the most expensive failure mode this product has, because it does not
look like a failure: the gateway shows RUNNING, last_error is null, the tags
preview correctly, the sink DB fills - and the durable store stays empty. Every
historian reader (dashboards, batch triggers, trends, reports) goes blank.

It has now shipped TWICE, both times identically: a GatewayWorker method called
on PLCManager, raising AttributeError inside the write try-block, swallowed,
rows re-buffered forever.

  * 2026-07-16  self._run_collection_io(...)      -> historian empty, sink full
  * 2026-08-21  self._mark_historian_commit(...)  -> W:0, every Last Value blank
                (commit 4f51e13, whose release gate never ran on a built app)

The rule these tests encode: the commit is the product, everything else in that
loop is telemetry. Telemetry may fail. It may NEVER cost a row.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
import tempfile

# Importing app.state builds a real AppStore. Point it at a throwaway DB so this
# never touches (or is blocked by) the installed app's store.
_TMP = tempfile.mkdtemp(prefix="tn-commitpath-")
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "s.db")
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"

import threading  # noqa: E402
from collections import deque  # noqa: E402

from app.services.plc_manager import PLCManager  # noqa: E402
from app import state as app_state  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(f"  {name:58s}: {'PASS' if ok else 'FAIL'}{(' - ' + str(detail)[:100]) if detail else ''}")
    if not ok:
        FAILS.append(name)


def make_manager():
    """A PLCManager with only the buffer state the write path touches."""
    m = PLCManager.__new__(PLCManager)
    m._historian_buffer_lock = threading.Lock()
    m._historian_buffer = deque()
    m._historian_buffer_total_rows = 0
    m._historian_buffer_dropped = 0
    m._historian_buffer_last_drain_mono = 0.0
    return m


class Recorder:
    """Stands in for app_store, counting what actually committed."""

    def __init__(self, fail_times=0):
        self.commits = []
        self.fail_times = fail_times

    def append_historian_rows(self, rows):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("database is locked")
        self.commits.append(list(rows))

    @property
    def rows(self):
        return [r for c in self.commits for r in c]


def run(manager, rows, worker, rec):
    real = app_state.app_store
    app_state.app_store = rec
    try:
        manager._flush_historian_buffer_then_write(rows, worker)
    finally:
        app_state.app_store = real


ROW = [{"tag_name": "Port1_Pin2", "value": 1.0}]

print("[the commit survives any telemetry failure]")

# 1. baseline - a healthy stamp
class GoodWorker:
    def __init__(self):
        self.stamped = 0

    def _mark_historian_commit(self, count):
        self.stamped += count


m, w, rec = make_manager(), GoodWorker(), Recorder()
run(m, ROW, w, rec)
check("a normal cycle commits", len(rec.rows) == 1, f"{len(rec.rows)} row(s)")
check("  and the durable counter is stamped", w.stamped == 1, w.stamped)
check("  and nothing is left buffered", len(m._historian_buffer) == 0, len(m._historian_buffer))

# 2. THE SHIPPED BUG - the worker has no such method
class WorkerMissingStamp:
    pass


m, rec = make_manager(), Recorder()
run(m, ROW, WorkerMissingStamp(), rec)
check("a worker with NO stamp method still commits", len(rec.rows) == 1, f"{len(rec.rows)} row(s)")
check("  and does not buffer the committed row", len(m._historian_buffer) == 0,
      f"{len(m._historian_buffer)} cycle(s) buffered")

# 3. the manager itself passed as worker (the exact 4f51e13 shape)
m, rec = make_manager(), Recorder()
run(m, ROW, m, rec)
check("passing the MANAGER as worker still commits", len(rec.rows) == 1, f"{len(rec.rows)} row(s)")
check("  and does not buffer", len(m._historian_buffer) == 0, len(m._historian_buffer))

# 4. no worker at all
m, rec = make_manager(), Recorder()
run(m, ROW, None, rec)
check("no worker at all still commits", len(rec.rows) == 1, f"{len(rec.rows)} row(s)")

# 5. a stamp that raises outright
class ExplodingWorker:
    def _mark_historian_commit(self, count):
        raise ValueError("counter store unavailable")


m, rec = make_manager(), Recorder()
run(m, ROW, ExplodingWorker(), rec)
check("a stamp that RAISES cannot cost the row", len(rec.rows) == 1, f"{len(rec.rows)} row(s)")
check("  and cannot re-buffer it", len(m._historian_buffer) == 0, len(m._historian_buffer))

# 6. a row must not commit twice because telemetry failed
m, rec = make_manager(), Recorder()
run(m, ROW, ExplodingWorker(), rec)
run(m, [{"tag_name": "Port1_Pin2", "value": 0.0}], ExplodingWorker(), rec)
# Count alone is not enough: with the bug, cycle 1 committed then re-buffered,
# so cycle 2 re-committed the SAME row and the total was still 2. Assert the
# VALUES, so a duplicate can never read as success.
vals = [r["value"] for r in rec.rows]
check("two cycles commit exactly two rows", len(rec.rows) == 2, f"{len(rec.rows)} row(s)")
check("  and they are the two DISTINCT cycles, not one twice", vals == [1.0, 0.0], vals)

print("\n[a REAL write failure must still buffer - that behaviour is preserved]")

# 7. the genuine failure path still protects data
m, rec = make_manager(), Recorder(fail_times=1)
run(m, ROW, GoodWorker(), rec)
check("a failed write commits nothing", len(rec.rows) == 0, f"{len(rec.rows)} row(s)")
check("  and the row is buffered, not dropped", len(m._historian_buffer) == 1,
      len(m._historian_buffer))
check("  and the row count is tracked", m._historian_buffer_total_rows == 1,
      m._historian_buffer_total_rows)

# 8. and it drains on the next cycle, oldest first
w = GoodWorker()
run(m, [{"tag_name": "Port1_Pin2", "value": 0.0}], w, rec)
check("the next cycle drains the buffer", len(m._historian_buffer) == 0,
      len(m._historian_buffer))
check("  both cycles are now committed", len(rec.rows) == 2, f"{len(rec.rows)} row(s)")
check("  oldest first (chronological insert order)", rec.rows[0]["value"] == 1.0,
      rec.rows[0]["value"])

print()
print(f"RESULT: {'PASS' if not FAILS else 'FAIL - ' + ', '.join(FAILS)}")
sys.exit(0 if not FAILS else 2)
