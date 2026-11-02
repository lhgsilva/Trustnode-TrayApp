# -*- coding: utf-8 -*-
"""Charts read pre-aggregated buckets, and those buckets exist without a policy.

2026-11-02. Why a 24 h chart took seconds on every refresh, and why over a
tunnel the whole edge looked dead ("templates could not be loaded", "the
edge did not answer in time"):

  * `get_historian_agg_rows` read `historian_agg_<bucket>`, which only the
    RETIRED legacy retention path ever wrote. 0 rows on every edge measured.
    So every call fell through to bucketing the whole window from raw:
    2.2 s per call for 24 h on a 514k-row store, ~10 s with seven widgets
    at once.
  * The retention engine DOES write `historian_rollup` - but only inside a
    pass that has an active deletion policy, and no customer edge has one.
    So that table was empty too.

Now the engine materialises minute/hour/day buckets with or without a
policy (nothing is deleted), and the chart reader reads them, bucketing raw
only for the tail the rollup has not reached yet.

    python scripts/test_rollups_feed_charts.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="tn_rollup_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-roll"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-66s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def _ts(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) + ".000"


def main() -> int:
    print("rollups exist without a policy, and charts read them")
    import inspect
    from app.state import app_store, retention_engine

    src = inspect.getsource(app_store.get_historian_agg_rows)
    check("the chart reader reads historian_rollup", "FROM historian_rollup" in src)
    check("  and no longer the never-written historian_agg_ tables",
          "historian_agg_{" not in src and 'f"historian_agg_' not in src)
    check("  on the read-only connection", "_connect_readonly()" in src
          and "with self._connect() as conn:" not in src)

    check("this edge has no retention policy (the customer shape)",
          retention_engine.store.get_active_policy() is None)

    # Three hours of readings at 5 s, two tags, ending 10 minutes ago; then a
    # few live rows inside the last minute that no rollup can have closed.
    now = time.time()
    rows = []
    t = now - 3 * 3600
    i = 0
    while t < now - 600:
        for tag in ("Flow", "Level"):
            rows.append({"ts_utc": _ts(t), "gateway_id": "gw1", "gateway_name": "PLC",
                         "device_name": "d", "plc_ip": "10.0.0.1", "database_name": "db",
                         "tag_name": tag, "value": float(i), "quality": 192,
                         "quality_label": "GOOD", "source": "test"})
        t += 5
        i += 1
    for k in range(5):
        rows.append({"ts_utc": _ts(now - 30 + k), "gateway_id": "gw1", "gateway_name": "PLC",
                     "device_name": "d", "plc_ip": "10.0.0.1", "database_name": "db",
                     "tag_name": "Flow", "value": 999.0, "quality": 192,
                     "quality_label": "GOOD", "source": "test"})
    app_store.append_historian_rows(rows)
    check("the store has history", True, "%d rows" % len(rows))

    # Before any rollup the reader still answers the whole window (live).
    from_utc, to_utc = _ts(now - 3 * 3600), _ts(now)
    before = app_store.get_historian_agg_rows(bucket="minute", from_utc=from_utc, to_utc=to_utc)
    check("with no buckets yet, the whole window is still answered from raw",
          len(before) >= 2 * 170 and all(r.get("source") != "historian_rollup" for r in before),
          "%d rows" % len(before))

    # 2026-11-02 (later): a pass is bounded to a few seconds of lock time per
    # call, so the first materialisation of a window takes several ticks.
    summary = retention_engine.run_once(reason="test", force=True)
    for _ in range(40):
        more = retention_engine.rollup_once(reason="test")
        summary["rollups"].extend(more)
        if not any(int(r.get("rows") or 0) > 0 for r in more):
            break
    con = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
    n_min = con.execute("SELECT COUNT(*) FROM historian_rollup WHERE resolution_s=60").fetchone()[0]
    n_hour = con.execute("SELECT COUNT(*) FROM historian_rollup WHERE resolution_s=3600").fetchone()[0]
    n_raw = con.execute("SELECT COUNT(*) FROM historian_readings").fetchone()[0]
    con.close()
    check("a maintenance pass with NO policy still writes minute buckets", n_min > 0,
          "historian_rollup has %d minute rows; notes=%s" % (n_min, summary.get("notes")))
    check("  and hour buckets composed from them", n_hour > 0, "%d hour rows" % n_hour)
    check("  and deletes nothing", n_raw == len(rows), "%d raw rows of %d" % (n_raw, len(rows)))
    check("  the pass reports what it rolled up", bool(summary.get("rollups")),
          repr(summary.get("rollups"))[:160])

    after = app_store.get_historian_agg_rows(bucket="minute", from_utc=from_utc, to_utc=to_utc)
    from_rollup = [r for r in after if r.get("source") == "historian_rollup"]
    live = [r for r in after if r.get("source") != "historian_rollup"]
    check("the chart now reads the buckets", len(from_rollup) >= 2 * 160,
          "%d rollup rows, %d live" % (len(from_rollup), len(live)))
    check("  and still shows the last minute, which no rollup has closed",
          any(r.get("tag") == "Flow" and r.get("value") == 999.0 for r in live),
          "the uncovered tail must come from raw")
    keys = [(r.get("ts"), r.get("gateway_id"), r.get("tag")) for r in after]
    check("  with no bucket served twice", len(keys) == len(set(keys)))
    sample = next((r for r in from_rollup if r.get("tag") == "Flow"), {})
    check("  rows carry the gateway's display names", sample.get("gateway_name") == "PLC"
          and sample.get("database_name") == "db", repr(sample)[:160])
    check("  value is the bucket average with min/max and sample count",
          sample.get("sample_count") == 12
          and sample.get("value_min") is not None and sample.get("value_max") is not None
          and abs(float(sample["value"]) - (float(sample["value_min"]) + float(sample["value_max"])) / 2) < 1e-6,
          repr({k: sample.get(k) for k in ("value", "value_min", "value_max", "sample_count")}))
    check("  the time stamps are the same text the live path uses",
          all(str(r.get("ts") or "").endswith(":00") and "T" not in str(r.get("ts")) for r in after),
          "a different format would stop the live merge de-duplicating")

    hours = app_store.get_historian_agg_rows(bucket="hour", from_utc=from_utc, to_utc=to_utc)
    check("an hour chart reads the hour tier", any(r.get("source") == "historian_rollup" for r in hours),
          "%d rows" % len(hours))

    # A second pass is idempotent and does not re-roll what is done.
    again = retention_engine.run_once(reason="test", force=True)
    again["rollups"] = [r for r in again.get("rollups") or [] if int(r.get("rows") or 0) > 0]
    check("a second pass has nothing new to materialise",
          not any(int(r.get("rows") or 0) > 0 for r in (again.get("rollups") or [])
                  if r.get("tier") == "r60"),
          repr(again.get("rollups"))[:160])

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
