# -*- coding: utf-8 -*-
"""The report data phase costs nothing that scales with the historian or with load.

2026-11-02, measured on a customer-shaped store (2.07M rows, 24 tags at 1 Hz,
collection writing every 500 ms) with a 4-6 page template - four charts of
three series, eight KPIs, a pie, two telemetry tables:

    before   7.0 s   app_settings re-read from SQLite 200x per render
    after    0.8 s

and at a real customer (8.5M rows/day, more days, slower disk) the same
code took 5-10 MINUTES. The causes, each now guarded here:

  1. KPI/pie stats aggregated RAW rows for the whole window, on the WRITER
     connection (10 s timeout, queued behind collection). Now whole minutes
     come from historian_rollup - exact by construction - and only the two
     edges are read raw, on the read-only handle.
  2. A cell with no rows in its window retried UNSCOPED - no tenant, no
     usable index - a SCAN of the whole historian per empty cell.
  3. Every historian read re-read app_settings under the global lock on the
     writer handle (200x per render). Cached 5 s.
  4. Chart series filtered the tag with LIKE '%tag%' - the tag index unused,
     every bucket of the window scanned, per series. Exact match first.
  5. One Line shape per chart segment (28 000 shapes, 360 000 validations).
     One PolyLine per series.
  6. The logo re-encoded and hashed on every page. One PDF form per logo.

    python scripts/test_report_data_phase_instant.py
"""
from __future__ import annotations

import inspect
import os
import sqlite3
import sys
import tempfile
import threading
import time

_TMP = tempfile.mkdtemp(prefix="tn_rpt_fast_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-fast"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-66s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def _ts(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) + ".000"


def main() -> int:
    print("the report data phase is instant and does not scale with the historian")
    from app.state import app_store, retention_engine
    from app.tenant import set_current_tenant
    set_current_tenant("tenant-cust-fast")
    from app.services import report_renderer as rr
    from app.services import retention_engine as re_mod

    # ---- the rules, stated where they cannot drift ----------------------
    src_stats = inspect.getsource(app_store.get_historian_stats)
    src_rules = inspect.getsource(app_store.get_historian_rule_stats)
    src_range = inspect.getsource(app_store.get_historian_rows_range)
    src_agg = inspect.getsource(app_store.get_historian_agg_rows)
    src_bucket = inspect.getsource(app_store.bucket_raw_historian_rows)
    for name, src in (("get_historian_stats", src_stats), ("get_historian_rule_stats", src_rules)):
        check("%s reads on the read-only connection" % name,
              "_connect_readonly()" in src and "with self._connect() as conn:" not in src)
        check("  and asks the rollup first", "_rollup_window_stats(" in src)
    for name, src in (("stats", src_stats), ("rule stats", src_rules), ("range", src_range)):
        check("%s: the unscoped retry only exists for an edge with no identity" % name,
              '_current_tenant_id() == "default"' in src,
              "otherwise an empty window scans the whole historian")
    check("the rollup reader matches the tag exactly", "tag_name = :tag_exact" in src_agg
          and "LIKE LOWER(:tag_like)\"\n" not in src_agg.split("tag_exact")[0])
    check("  so does the live bucketer", "tag_name = :tag_exact" in src_bucket)
    check("app_settings is cached", hasattr(app_store, "_APP_SETTINGS_CACHE_TTL_S"))
    check("  and _prefer_cloud_reads uses the cache",
          "_get_app_settings()" in inspect.getsource(app_store._prefer_cloud_reads))
    check("a chart series is one PolyLine", "PolyLine(flat" in inspect.getsource(rr._section_chart))
    check("  with a drawing cap no reader can see", getattr(rr, "_DRAW_POINT_CAP", 0) >= 600)
    check("the logos are PDF forms, drawn once", "beginForm" in inspect.getsource(rr._on_page))
    check("rollups tick every minute, not every half hour", 10 <= re_mod.ROLLUP_TICK_S <= 120,
          "ROLLUP_TICK_S=%s" % re_mod.ROLLUP_TICK_S)

    # ---- behaviour: build 3 h at 1 Hz for 8 tags, a writer, rollups ------
    now = time.time()
    tags = ["T%d" % i for i in range(8)]
    con = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
    t = now - 3 * 3600
    batch = []
    i = 0
    while t < now - 90:
        for k, tag in enumerate(tags):
            batch.append(("tenant-cust-fast", _ts(t), "test", "gw1", "PLC", "d", "10.0.0.1", "db",
                          tag, float((i + k) % 100), None, "REAL", 192, "GOOD", _ts(t)))
        t += 1
        i += 1
    con.executemany("INSERT INTO historian_readings(tenant_id,ts_utc,source,gateway_id,gateway_name,"
                    "device_name,plc_ip,database_name,tag_name,value,value_text,data_type,quality,"
                    "quality_label,created_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
    con.commit()
    n_rows = con.execute("SELECT COUNT(*) FROM historian_readings").fetchone()[0]
    check("the store has history", n_rows > 80000, "%d rows" % n_rows)
    rolled = retention_engine.rollup_once(reason="test")
    check("rollup_once materialises without a policy", any(int(r.get("rows") or 0) > 0 for r in rolled),
          repr(rolled)[:120])

    stop = threading.Event()

    def writer():
        j = 0
        while not stop.is_set():
            j += 1
            app_store.append_historian_rows([{
                "ts_utc": _ts(time.time()), "gateway_id": "gw1", "gateway_name": "PLC",
                "device_name": "d", "tag_name": tag, "value": float(j % 100), "quality": 192,
                "quality_label": "GOOD", "source": "test"} for tag in tags])
            time.sleep(0.25)
    th = threading.Thread(target=writer, daemon=True)
    th.start()
    time.sleep(1.0)

    try:
        # KPI exactness: rollup-backed equals raw, for an unaligned window.
        a, b = _ts(now - 2 * 3600 - 17), _ts(now - 33)
        raw = con.execute("SELECT COUNT(*), AVG(value), MIN(value), MAX(value), SUM(value) FROM historian_readings "
                          "WHERE tenant_id=? AND gateway_id=? AND tag_name=? AND ts_utc>=? AND ts_utc<=?",
                          ("tenant-cust-fast", "gw1", "T3", a, b)).fetchone()
        t0 = time.perf_counter()
        rows = app_store.get_historian_rule_stats(
            rules=[{"id": "r", "label": "x", "gateway_id": "gw1", "tag_name": "T3", "operator": "any", "aggregation": "avg"}],
            gateway="gw1", from_utc=a, to_utc=b)
        kpi_s = time.perf_counter() - t0
        r0 = rows[0] if rows else {}
        check("a KPI from the rollup equals the raw aggregate exactly",
              r0.get("count") == raw[0] and abs(float(r0.get("value") or 0) - float(raw[1])) < 1e-9
              and r0.get("min") == raw[2] and r0.get("max") == raw[3] and abs(float(r0.get("sum") or 0) - float(raw[4])) < 1e-6,
              "store=%r raw=%r" % ({k: r0.get(k) for k in ("count", "value", "min", "max", "sum")}, tuple(raw)))
        check("  and takes milliseconds under a running writer", kpi_s < 0.1, "%.3fs" % kpi_s)
        t0 = time.perf_counter()
        empty = app_store.get_historian_rule_stats(
            rules=[{"id": "r", "label": "x", "gateway_id": "gw1", "tag_name": "NoSuchTag", "operator": "any", "aggregation": "avg"}],
            gateway="gw1", from_utc=a, to_utc=b)
        empty_s = time.perf_counter() - t0
        check("an empty KPI cell is not a table scan", empty_s < 0.1 and empty and empty[0].get("count") == 0,
              "%.3fs" % empty_s)
        t0 = time.perf_counter()
        pie = app_store.get_historian_stats(from_utc=a, to_utc=b, gateway="gw1", tag="T5")
        pie_s = time.perf_counter() - t0
        rawp = con.execute("SELECT COUNT(*), AVG(value) FROM historian_readings WHERE tenant_id=? AND gateway_id=? "
                           "AND tag_name=? AND ts_utc>=? AND ts_utc<=?", ("tenant-cust-fast", "gw1", "T5", a, b)).fetchone()
        check("pie stats from the rollup equal raw", pie and pie[0]["count"] == rawp[0]
              and abs(pie[0]["avg"] - rawp[1]) < 1e-9, "%r vs %r" % (pie[:1], tuple(rawp)))
        check("  in milliseconds", pie_s < 0.1, "%.3fs" % pie_s)
        t0 = time.perf_counter()
        agg = app_store.get_historian_agg_rows(bucket="minute", from_utc=a, to_utc=b, gateway="gw1", tag="T1")
        agg_s = time.perf_counter() - t0
        check("a chart series over the window is index-bound",
              agg_s < 0.15 and len(agg) > 100 and all(r.get("tag") == "T1" for r in agg), "%.3fs rows=%d" % (agg_s, len(agg)))
        check("  the exact name does not match a sibling tag",
              not any(r.get("tag") != "T1" for r in agg), "LIKE '%T1%' would also return T10..T19")

        # app_settings: one SQLite read per 5 s, however many historian reads.
        reads = {"n": 0}
        _orig_connect_ro = app_store._connect_readonly

        def _counting_ro():
            reads["n"] += 1
            return _orig_connect_ro()
        app_store._app_settings_cache = None
        app_store._connect_readonly = _counting_ro
        before = reads["n"]
        for _ in range(50):
            app_store._prefer_cloud_reads()
            app_store._is_cloud_auto_sync_enabled()
        app_store._connect_readonly = _orig_connect_ro
        check("100 settings lookups cost one read", reads["n"] - before <= 2, "%d reads" % (reads["n"] - before))

        # The whole render, under the writer.
        ser = lambda t_: {"id": "s-" + t_, "label": t_, "gateway_id": "gw1", "tag_name": t_,
                          "color": "#14a89a", "axis": "left", "multiplier": 1, "offset": 0}
        tpl = {"id": "tpl-fast", "name": "Fast", "definition": {"sections": [
            {"type": "header", "title": "Fast", "show_generated_at": True},
            {"type": "line_chart", "title": "A", "time_range": {"preset": "custom", "from_utc": a, "to_utc": b},
             "readings_count": 5000, "series": [ser("T0"), ser("T1"), ser("T2")]},
            {"type": "line_chart", "title": "B", "time_range": {"preset": "custom", "from_utc": a, "to_utc": b},
             "readings_count": 5000, "series": [ser("T3"), ser("T4"), ser("T5")]},
            {"type": "kpi_grid", "title": "K", "time_range": {"preset": "custom", "from_utc": a, "to_utc": b},
             "items": [{"id": "k%d" % n, "label": "T%d" % n, "gateway_id": "gw1", "tag_name": "T%d" % n,
                        "aggregation": ["avg", "max", "min", "count"][n % 4]} for n in range(8)]
                      + [{"id": "kx", "label": "none", "gateway_id": "gw1", "tag_name": "NoSuchTag", "aggregation": "avg"}]},
            {"type": "pie_chart", "title": "P", "data_source_type": "tag_direct",
             "time_range": {"preset": "custom", "from_utc": a, "to_utc": b}, "gateway_id": "gw1", "tag_name": "T6",
             "query_result_aggregation": "count"},
            {"type": "table", "title": "T", "time_range": {"preset": "none"}, "row_limit": 200, "readings_count": 200,
             "series": [ser("T7"), ser("T0")]},
        ]}}
        rr.render_template_to_pdf(tpl)                      # warm the fonts
        # 2026-11-04: best of three. One sample on a machine that is also
        # building or collecting measured 2.9 s with the same code that
        # measures 0.8 s idle; a real slowdown is slow three times.
        render_s = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            path, n, _ = rr.render_template_to_pdf(tpl)
            render_s = min(render_s, time.perf_counter() - t0)
        # 2.5 s: measured 0.8-1.2 s here, 1.5 s while an installer build shared
        # the CPU; the regression this guards was 7.0 s.
        check("a 2-chart/9-KPI/pie/table report renders in under 2.5 s with collection running",
              render_s < 2.5 and n > 10000, "%.2fs, %d bytes" % (render_s, n))
    finally:
        stop.set()
        th.join(timeout=2)
        con.close()

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
