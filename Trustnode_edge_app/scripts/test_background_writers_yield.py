# -*- coding: utf-8 -*-
"""Background writers take the write lock briefly, and never for a read.

2026-11-03 architecture review, three findings on one file
(`trustnode_app_store.db`):

  * a rollup chunk ran `INSERT ... SELECT` under BEGIN IMMEDIATE, so the
    write lock was held for the whole index read of a 300 s chunk - on a
    customer's disk, seconds, three of them every minute;
  * the cloud forwarder stamped "idle" into data_sync_state on EVERY tick
    (80 ms) and on every historian append when it had nothing to send -
    5+ fsyncs a second under the global lock, competing with collection;
  * `upsert_domain` / `upsert_domain_scoped` waited for the global lock
    without limit and for SQLite up to 10 s - from 20 request routes, not
    only the one that was bounded.

    python scripts/test_background_writers_yield.py
"""
from __future__ import annotations

import inspect
import os
import sqlite3
import sys
import tempfile
import threading
import time

_TMP = tempfile.mkdtemp(prefix="tn_bgw_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-bgw"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-70s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def _ts(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) + ".000"


def main() -> int:
    print("background writers yield")
    from app.state import app_store, retention_engine
    from app.tenant import set_current_tenant
    set_current_tenant("tenant-cust-bgw")
    from app.services import retention_engine as re_mod
    from app.services import app_store as store_mod

    # ---- 1. the rollup reads raw on a read-only handle, writes briefly ----
    src = inspect.getsource(retention_engine._rollup_range)
    check("the rollup chunk reads the raw aggregation on a read-only handle first",
          "_ROLLUP_RAW_SELECT" in src and "connect(readonly=True)" in src)
    check("  and the write transaction only upserts the result rows",
          "_ROLLUP_UPSERT" in src and "_ROLLUP_FROM_RAW" not in src)
    check("  the upsert carries the same 18 columns as the select",
          getattr(re_mod, "_ROLLUP_UPSERT", "").count("?") == 18
          and getattr(re_mod, "_ROLLUP_RAW_SELECT", "").lstrip().startswith("SELECT"))

    # Measure the longest time the engine holds SQLite's write lock while
    # rolling up an hour of 1 Hz data for 50 tags: an outside writer that
    # tries every 50 ms must never wait longer than a short upsert.
    now = int(time.time()) - 120
    tags = ["T%02d" % i for i in range(50)]
    con = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
    con.execute("PRAGMA journal_mode=WAL")
    batch = []
    for t in range(now - 3600, now):
        for k, tag in enumerate(tags):
            batch.append(("tenant-cust-bgw", _ts(t), "test", "gw1", "PLC", "d", "10.0.0.1", "db",
                          tag, float((t + k) % 100), None, "REAL", 192, "GOOD", _ts(t)))
    con.executemany("INSERT INTO historian_readings(tenant_id,ts_utc,source,gateway_id,gateway_name,"
                    "device_name,plc_ip,database_name,tag_name,value,value_text,data_type,quality,"
                    "quality_label,created_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
    con.commit()
    con.close()
    waits = []
    stop = threading.Event()

    def prober():
        c = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"], timeout=10)
        while not stop.is_set():
            t0 = time.perf_counter()
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT INTO app_logs(tenant_id, ts_utc, level, category, message, created_utc) "
                      "VALUES('t', ?, 'info', 'x', 'probe', ?)", (_ts(time.time()), _ts(time.time())))
            c.commit()
            waits.append(time.perf_counter() - t0)
            time.sleep(0.05)
        c.close()
    th = threading.Thread(target=prober, daemon=True)
    th.start()
    time.sleep(0.3)
    waits.clear()
    rolled = 0
    for _ in range(6):
        out = retention_engine.rollup_once(reason="test")
        rolled += sum(int(r.get("rows") or 0) for r in out if r.get("tier") == "r60")
        if rolled >= 50 * 60:
            break
    stop.set()
    th.join(timeout=3)
    worst = max(waits) if waits else 0.0
    print("    rolled %d minute rows; an outside writer waited at most %.3fs (n=%d)" % (rolled, worst, len(waits)))
    check("the rollup made progress", rolled > 0, "%d rows" % rolled)
    check("  and never held the write lock longer than a short upsert (< 0.5 s)", worst < 0.5, "%.3fs" % worst)

    # ---- 2. an idle forwarder does not stamp state every tick ----------------
    src = inspect.getsource(app_store._flush_data_outbox_once)
    check("an idle forwarder stamps 'idle' at most once a minute",
          "_idle_sync_stamp_mono" in src and ">= 60.0" in src)

    # ---- 3. every config write is bounded, not only the one route ----------
    check("upsert_domain_scoped waits a bounded time for the lock and for SQLite",
          "_bounded_write(" in inspect.getsource(app_store.upsert_domain_scoped)
          and "_bounded_write(" in inspect.getsource(app_store.upsert_domain))
    check("  StoreBusy is an OperationalError, so existing handlers still catch it",
          issubclass(getattr(store_mod, "StoreBusy", type(None)), sqlite3.OperationalError))
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
    try:
        app_store.upsert_domain_scoped("tenant-cust-bgw|-|edge-b", "devices", [{"id": "a"}], actor="t")
        what = "no error"
    except Exception as exc:
        what = type(exc).__name__
    dt = time.perf_counter() - t0
    check("a direct store write answers within 16 s while the writer is held", dt < 16.0, "%.1fs" % dt)
    check("  with StoreBusy", what == "StoreBusy", what)
    release.set()
    time.sleep(0.5)
    app_store.upsert_domain_scoped("tenant-cust-bgw|-|edge-b", "devices", [{"id": "b"}], actor="t")
    got = app_store.get_scoped_domain_payload("tenant-cust-bgw|-|edge-b", "devices")
    check("  and the next write lands once the writer is free", got == [{"id": "b"}], repr(got))

    # The HTTP layer turns StoreBusy into a 503 for every route, not one.
    from app.main import app as fastapi_app
    # main.py exports a dispatcher over the FastAPI app; look through it.
    fastapi_app = getattr(fastapi_app, "_main", fastapi_app)
    handlers = getattr(fastapi_app, "exception_handlers", {})
    check("the API maps StoreBusy to a 503 for every route",
          any(getattr(k, "__name__", "") == "StoreBusy" for k in handlers))

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
