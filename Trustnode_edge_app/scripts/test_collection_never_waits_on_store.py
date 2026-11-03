# -*- coding: utf-8 -*-
"""The collection cycle never waits for the store.

2026-11-03 architecture review, measured on HEAD: an outside connection
holding SQLite's write lock for 3 s held the collection cycle for 2.86 s,
because `_broadcast` awaited its own historian write (up to the writer's
10 s busy timeout). A store-and-forward collector never does that: the
scan continues, a slow store grows the queue, the writer drains it later in
order. Also: the rollup's "writer busy" signal watched the persist pool's
queue, which the historian write never used, so it was always False.

Now the cycle hands its rows to the gateway's persist pool and returns at
once; one flush per gateway at a time; a cycle that arrives while one is in
flight is buffered and drained by that flush before it returns; and the
busy signal reports an in-flight write or a non-empty buffer.

    python scripts/test_collection_never_waits_on_store.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

_TMP = tempfile.mkdtemp(prefix="tn_nowait_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-nowait"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-70s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def _ts(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) + ".000"


class FakeWorker:
    def __init__(self):
        self._ex = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tn-persist-test")
        self.commits = []

    def _get_persist_executor(self):
        return self._ex

    def _mark_historian_commit(self, n):
        self.commits.append(int(n))


def _rows(n, tag_prefix, t):
    return [{"ts_utc": _ts(t), "gateway_id": "gw1", "gateway_name": "PLC", "device_name": "d",
             "tag_name": "%s%02d" % (tag_prefix, i), "value": float(i), "quality": 192,
             "quality_label": "GOOD", "source": "test"} for i in range(n)]


def main() -> int:
    print("the collection cycle never waits for the store")
    import inspect
    from app.state import app_store, plc_manager
    import app.state as state_mod
    from app.tenant import set_current_tenant
    set_current_tenant("tenant-cust-nowait")

    check("the manager can schedule a write without waiting",
          hasattr(plc_manager, "_schedule_historian_write") and hasattr(plc_manager, "historian_write_pending"))
    src = inspect.getsource(plc_manager._broadcast)
    check("  and the cycle uses it instead of awaiting the write",
          "_schedule_historian_write(" in src and "await worker._run_collection_io(" not in src)
    check("the rollup's busy signal asks the manager, not an unused queue",
          "historian_write_pending()" in inspect.getsource(state_mod._collection_writer_busy))
    if FAIL:
        print("\n  %d passed, %d failed" % (len(PASS), len(FAIL)))
        return 1

    worker = FakeWorker()
    app_store.append_historian_rows(_rows(3, "seed", time.time() - 100))

    held = threading.Event()
    release = threading.Event()

    def holder():
        c = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"], timeout=10)
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT INTO app_logs(tenant_id, ts_utc, level, category, message, created_utc) "
                  "VALUES('t', ?, 'info', 'x', 'holder', ?)", (_ts(time.time()), _ts(time.time())))
        held.set()
        release.wait(timeout=60)
        c.commit()
        c.close()
    threading.Thread(target=holder, daemon=True).start()
    held.wait(5)

    t0 = time.perf_counter()
    first = plc_manager._schedule_historian_write(_rows(5, "A", time.time() - 20), worker)
    dt = time.perf_counter() - t0
    check("the cycle's hand-off returns at once while the writer is held", dt < 0.1 and first is True,
          "%.3fs submitted=%s" % (dt, first))
    time.sleep(0.3)
    check("  the busy signal is True while the write is in flight",
          plc_manager.historian_write_pending() and state_mod._collection_writer_busy())
    t0 = time.perf_counter()
    second = plc_manager._schedule_historian_write(_rows(5, "B", time.time() - 10), worker)
    dt = time.perf_counter() - t0
    check("a second cycle during the in-flight write is buffered, not waited for",
          dt < 0.1 and second is False, "%.3fs submitted=%s" % (dt, second))
    with plc_manager._historian_buffer_lock:
        buffered = len(plc_manager._historian_buffer)
    check("  one cycle sits in the FIFO buffer", buffered == 1, "%d" % buffered)

    release.set()
    deadline = time.time() + 20
    while time.time() < deadline and plc_manager.historian_write_pending():
        time.sleep(0.1)
    check("once the writer is free the in-flight write finishes and drains the buffer",
          not plc_manager.historian_write_pending(), "still pending after 20 s")
    con = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
    got = con.execute("SELECT tag_name FROM historian_readings WHERE tag_name LIKE 'A%' OR tag_name LIKE 'B%' "
                      "ORDER BY id").fetchall()
    con.close()
    names = [r[0] for r in got]
    check("  both cycles are stored", len(names) == 10, "%d rows" % len(names))
    check("  in order: the first cycle before the buffered one",
          names[:5] == ["A%02d" % i for i in range(5)] and names[5:] == ["B%02d" % i for i in range(5)], repr(names))
    check("  the gateway's commit stamp was called for each cycle", sorted(worker.commits) == [5, 5], repr(worker.commits))
    with plc_manager._historian_buffer_lock:
        buffered = len(plc_manager._historian_buffer)
    check("  and the buffer is empty again", buffered == 0, "%d" % buffered)

    # A write with no executor (no worker) still lands, on a plain thread.
    plc_manager._schedule_historian_write(_rows(2, "C", time.time() - 5), None)
    time.sleep(1.0)
    con = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
    n = con.execute("SELECT COUNT(*) FROM historian_readings WHERE tag_name LIKE 'C%'").fetchone()[0]
    con.close()
    check("a cycle with no worker still reaches the store", n == 2, "%d" % n)

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
