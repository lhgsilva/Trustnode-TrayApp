# -*- coding: utf-8 -*-
"""The rollup never stands between collection and the database.

2026-11-02, from a customer edge the evening the minute rollup tick shipped:
"Gateway not collecting - no fresh sample in 90 s", every API call "did not
answer in time", a report stuck on "Generating...". One cause: the FIRST
rollup pass over months of 1 Hz history ran chunk after chunk under
BEGIN IMMEDIATE, the engine's writer-busy hook had never been wired, and
each minute's tick allowed 55 s of that. Measured here on 3 days of data
(a 20 s backfill): the collection writer's persist went from 0.15 s to
p50 1.35 s, max 5.0 s, six of nine writes over a second. Months of data at
a customer is the same, continuously.

Now: at most a few seconds of chunk work per tick, a pause after every
chunk at least as long as the chunk held the lock, the collection queue as
the writer-busy signal, and - without a policy - a first pass over the last
week rather than the archive.

    python scripts/test_rollup_yields_to_collection.py
"""
from __future__ import annotations

import inspect
import os
import sqlite3
import sys
import tempfile
import threading
import time

_TMP = tempfile.mkdtemp(prefix="tn_yield_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-yield"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-66s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def _ts(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) + ".000"


def main() -> int:
    print("the rollup yields to collection")
    from app.state import app_store, retention_engine
    from app.tenant import set_current_tenant
    set_current_tenant("tenant-cust-yield")
    from app.services import retention_engine as re_mod
    import app.state as state_mod

    check("the engine gets a writer-busy signal from collection",
          "writer_busy_fn=_collection_writer_busy" in inspect.getsource(state_mod)
          and retention_engine._writer_busy_fn is not None)
    check("a tick does a few seconds of work, not the whole minute",
          1 <= re_mod.ROLLUP_WORK_PER_TICK_S <= 10, "%s" % re_mod.ROLLUP_WORK_PER_TICK_S)
    check("  and pauses after every chunk", "ROLLUP_CHUNK_PAUSE_S" in inspect.getsource(retention_engine._rollup_range))
    check("without a policy the first pass covers the last week, not the archive",
          re_mod.NO_POLICY_BACKFILL_S == 7 * 86400 and "floor_ms" in inspect.getsource(retention_engine.rollup_once))

    # One day at 1 Hz, ten tags: a backfill several ticks long.
    now = int(time.time()) - 120
    tags = ["T%02d" % i for i in range(10)]
    con = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
    con.execute("PRAGMA journal_mode=WAL")
    batch = []
    t = now - 86400
    i = 0
    while t < now:
        for k, tag in enumerate(tags):
            batch.append(("tenant-cust-yield", _ts(t), "test", "gw1", "PLC", "d", "10.0.0.1", "db",
                          tag, float((i + k) % 100), None, "REAL", 192, "GOOD", _ts(t)))
        if len(batch) >= 100000:
            con.executemany("INSERT INTO historian_readings(tenant_id,ts_utc,source,gateway_id,gateway_name,"
                            "device_name,plc_ip,database_name,tag_name,value,value_text,data_type,quality,"
                            "quality_label,created_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            con.commit()
            batch = []
        t += 1
        i += 1
    if batch:
        con.executemany("INSERT INTO historian_readings(tenant_id,ts_utc,source,gateway_id,gateway_name,"
                        "device_name,plc_ip,database_name,tag_name,value,value_text,data_type,quality,"
                        "quality_label,created_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        con.commit()
    n_rows = con.execute("SELECT COUNT(*) FROM historian_readings").fetchone()[0]
    con.close()
    check("a day of 1 Hz history is in the store", n_rows >= 860000, "%d" % n_rows)

    stalls: list[float] = []
    stop = threading.Event()

    def writer():
        j = 0
        while not stop.is_set():
            j += 1
            t0 = time.perf_counter()
            app_store.append_historian_rows([{
                "ts_utc": _ts(time.time()), "gateway_id": "gw1", "gateway_name": "PLC",
                "device_name": "d", "tag_name": tag, "value": float(j % 100), "quality": 192,
                "quality_label": "GOOD", "source": "test"} for tag in tags])
            stalls.append(time.perf_counter() - t0)
            time.sleep(0.5)
    th = threading.Thread(target=writer, daemon=True)
    th.start()
    time.sleep(1.5)
    stalls.clear()

    ticks = 0
    t_all = time.perf_counter()
    done_rows = 0
    try:
        while ticks < 12 and time.perf_counter() - t_all < 90:
            t0 = time.perf_counter()
            out = retention_engine.rollup_once(reason="test")
            took = time.perf_counter() - t0
            ticks += 1
            done_rows += sum(int(r.get("rows") or 0) for r in out if r.get("tier") == "r60")
            if ticks == 1:
                check("one tick returns within its budget (the lock is free again)",
                      took < re_mod.ROLLUP_WORK_PER_TICK_S + 8, "%.1fs" % took)
            # keep ticking until the writer has been sampled enough times
            if done_rows >= 1400 and len(stalls) >= 10:
                break
            time.sleep(0.5)
    finally:
        stop.set()
        th.join(timeout=3)
    s = sorted(stalls)
    p50 = s[len(s) // 2] if s else 0
    p95 = s[int(len(s) * 0.95)] if s else 0
    worst = s[-1] if s else 0
    print("    writer during %d tick(s): n=%d p50=%.3fs p95=%.3fs max=%.3fs  minute rows so far=%d"
          % (ticks, len(s), p50, p95, worst, done_rows))
    check("collection keeps persisting while the backfill runs", len(s) >= 10, "%d writes" % len(s))
    check("  no write waits more than a second for the lock", worst < 1.0, "max %.2fs (was 5.0 s)" % worst)
    check("  and the typical write is unchanged", p50 < 0.3, "p50 %.3fs" % p50)
    check("the backfill still makes progress tick by tick", done_rows > 0, "%d minute rows" % done_rows)

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
