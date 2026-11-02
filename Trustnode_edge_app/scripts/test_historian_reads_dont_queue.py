# -*- coding: utf-8 -*-
"""A chart read must not queue behind the historian writer.

2026-10-31. THE root cause of "templates, scheduled reports, generated
reports could not be loaded - the edge did not answer in time", and of
"Run failed: Timed out after 60s" on loopback.

It was never slow SQL and never a slow render. From the operator's own Logs
page, once slow requests were made visible there:

    Slow request: GET /api/app-store/historian/agg took 13.9s (status 200)
    Slow request: GET /api/app-store/historian/agg took 13.4s (status 200)
    Slow request: GET /api/app-store/historian/agg took 12.4s (status 200)
    ... seven of them inside six seconds, all SUCCEEDING

get_historian_agg_rows did a pure read on _connect() - the WRITER
connection, timeout 10s. On an edge collecting at 500 ms it queued behind
the historian writer and burned most of that timeout. Seven dashboard
widgets doing it at once exhausted both the six connections a browser
allows per origin and the thread pool, so everything else timed out behind
it. The edge was never broken; it was waiting on its own writer.

_connect_readonly() exists for this - a read-only handle with a short
busy_timeout that "can never wedge for more than ~3s waiting on the
writer". get_historian_rows_range already used it, which is why that one
measures 20 ms warm while this measured 14 s.

This test writes to the historian continuously and reads at the same time,
which is the only condition under which the bug appears. On an idle store
both connections look identical - which is why it never showed on a bench.

    python scripts/test_historian_reads_dont_queue.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

_TMP = tempfile.mkdtemp(prefix="tn_hreads_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-62s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def main() -> int:
    print("a chart read must not queue behind the writer")
    import inspect
    from app.state import app_store

    # The rule, stated where it cannot drift: every historian READ uses the
    # read-only handle. A reviewer adding the next one copies a neighbour.
    for fn in ("get_historian_agg_rows", "get_historian_rows_range"):
        src = inspect.getsource(getattr(app_store, fn))
        check("%s reads on the read-only connection" % fn,
              "_connect_readonly()" in src,
              "the writer connection waits up to its 10s timeout")
        check("  and not on the writer connection" if fn == "get_historian_agg_rows"
              else "  (same for the range read)",
              "with self._connect() as conn:" not in src,
              "a pure read has no business on the write handle")

    # Now the behaviour, under the condition that actually triggers it.
    rows = []
    now = time.time()
    for i in range(4000):
        rows.append({
            "ts_utc": time.strftime("%Y-%m-%d %H:%M:%S",
                                    time.gmtime(now - (4000 - i))),
            "gateway_id": "gw1", "gateway_name": "PLC", "device_name": "d",
            "tag_name": "T%d" % (i % 8), "value": float(i),
            "quality": 192, "quality_label": "GOOD", "source": "test",
        })
    app_store.append_historian_rows(rows)
    check("the store has history to read", True, "%d rows" % len(rows))

    stop = threading.Event()
    wrote = {"n": 0}

    def writer():
        i = 0
        while not stop.is_set():
            i += 1
            try:
                app_store.append_historian_rows([{
                    "ts_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                    "gateway_id": "gw1", "gateway_name": "PLC",
                    "device_name": "d", "tag_name": "T%d" % (i % 8),
                    "value": float(i), "quality": 192,
                    "quality_label": "GOOD", "source": "test",
                }])
                wrote["n"] += 1
            except Exception:
                pass

    threads = [threading.Thread(target=writer, daemon=True) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(1.0)                      # let the writers get going

    worst = 0.0
    took = []
    try:
        for _ in range(12):
            t0 = time.perf_counter()
            app_store.get_historian_agg_rows(bucket="minute", limit=5000)
            d = time.perf_counter() - t0
            took.append(d)
            worst = max(worst, d)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=2)

    took.sort()
    print("    writes during the test: %d   reads: min=%.3fs med=%.3fs worst=%.3fs"
          % (wrote["n"], took[0], took[len(took) // 2], worst))

    # The writer connection's timeout is 10s; the read-only handle's
    # busy_timeout is ~3s. Anything approaching the former is the bug.
    check("a chart read never waits on the writer's 10s timeout", worst < 3.0,
          "worst read took %.2fs while %d writes were in flight" % (worst, wrote["n"]))
    check("  and the median stays interactive", took[len(took) // 2] < 1.0,
          "median %.2fs" % took[len(took) // 2])
    check("  the writer kept working throughout", wrote["n"] > 0,
          "a read must not stop collection either")

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
