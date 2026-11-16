# -*- coding: utf-8 -*-
"""The data filter: rows chosen by OTHER tags' values at the same time.

2026-11-16, from the owner: a filter "not only for the time but also based on
a tag value ... the timestamp of the data row and the tag value in the same
timestamp ... multiple conditions, and / or ... any dashboard widget, any
csv engine and any report section".

Seeds two gateways. Gateway A reads Run / Temp / Flow in one cycle (one
ts_utc for all three); gateway B reads Recipe on its own clock. Then asks
the four store readers every surface goes through, the HTTP routes, the
Data Export builder and the in-memory evaluator the export sinks use.

    python scripts/test_data_filter.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="tn_dfilter_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-df"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "backend"))
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-74s %s%s" % (name, "PASS" if ok else "FAIL", ("  - " + detail) if (not ok and detail) else ""))


def main() -> int:
    print("the data filter")
    from app.state import app_store
    from app.tenant import set_current_tenant
    from app.services import historian_filter as hf
    set_current_tenant("tenant-cust-df")

    # ---- seed: 20 cycles of gateway A at 1 s, Run = 1 for cycles 5..14 ---------------
    rows = []
    base = "2026-11-16 10:00:%02d.000"
    for i in range(20):
        ts = base % i
        run = 1.0 if 5 <= i <= 14 else 0.0
        for tag, val in (("Run", run), ("Temp", 20.0 + i), ("Flow", 100.0 + i)):
            rows.append({"ts_utc": ts, "gateway_id": "gw-A", "gateway_name": "A", "device_name": "d", "plc_ip": "1",
                         "database_name": "db", "tag_name": tag, "value": val, "quality": 192, "quality_label": "GOOD", "source": "t"})
    # gateway B: Recipe 7 from 10:00:00, 9 from 10:00:10 (its own stamps, 0.5 s off)
    rows.append({"ts_utc": "2026-11-16 10:00:00.500", "gateway_id": "gw-B", "gateway_name": "B", "device_name": "d", "plc_ip": "2",
                 "database_name": "db", "tag_name": "Recipe", "value": 7.0, "quality": 192, "quality_label": "GOOD", "source": "t"})
    rows.append({"ts_utc": "2026-11-16 10:00:10.500", "gateway_id": "gw-B", "gateway_name": "B", "device_name": "d", "plc_ip": "2",
                 "database_name": "db", "tag_name": "Recipe", "value": 9.0, "quality": 192, "quality_label": "GOOD", "source": "t"})
    app_store.append_historian_rows(rows)
    con = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
    n = con.execute("SELECT COUNT(*) FROM historian_readings").fetchone()[0]
    con.close()
    check("seeded 62 rows (20 cycles x 3 tags + 2)", n == 62, str(n))

    run_on = {"logic": "all", "conditions": [{"tag_name": "Run", "operator": "eq", "value1": 1}]}
    both = {"logic": "all", "conditions": [{"tag_name": "Run", "operator": "eq", "value1": 1},
                                           {"tag_name": "Temp", "operator": "gte", "value1": 30}]}
    either = {"logic": "any", "conditions": [{"tag_name": "Temp", "operator": "lt", "value1": 22},
                                             {"tag_name": "Temp", "operator": "between", "value1": 37, "value2": 38}]}
    recipe9 = {"logic": "all", "conditions": [{"gateway_id": "gw-B", "tag_name": "Recipe", "operator": "eq", "value1": 9}]}

    # ---- parse ---------------------------------------------------------------------
    check("the filter parses from JSON text and drops a half-typed condition",
          hf.parse_filter(json.dumps({"conditions": [{"tag_name": "Run", "operator": ">", "value1": "1"}, {"tag_name": "", "operator": "eq", "value1": 1},
                                                     {"tag_name": "X", "operator": "eq", "value1": "abc"}]}))
          == {"logic": "all", "conditions": [{"gateway_id": "", "tag_name": "Run", "operator": "gt", "value1": 1.0, "value2": None}]})
    check("  nothing usable -> None (a reader behaves as before)", hf.parse_filter("") is None and hf.parse_filter("{}") is None
          and hf.parse_filter({"conditions": [{"tag_name": "A"}]}) is None and hf.parse_filter({"enabled": False, "conditions": [{"tag_name": "A", "value1": 1}]}) is None)

    # ---- the range reader ------------------------------------------------------------
    flow = app_store.get_historian_rows_range(gateway="gw-A", tag="Flow", limit=100)
    check("without a filter: 20 Flow rows", len(flow) == 20, str(len(flow)))
    flow_on = app_store.get_historian_rows_range(gateway="gw-A", tag="Flow", limit=100, data_filter=run_on)
    vals = sorted(float(r["value"]) for r in flow_on)
    check("Flow rows while Run = 1 in the SAME cycle: the 10 cycles 5..14", vals == [105.0 + i for i in range(10)], repr(vals))
    flow_both = app_store.get_historian_rows_range(gateway="gw-A", tag="Flow", limit=100, data_filter=json.dumps(both))
    vals = sorted(float(r["value"]) for r in flow_both)
    check("  ALL: Run = 1 AND Temp >= 30 -> cycles 10..14 (JSON text accepted)", vals == [110.0 + i for i in range(5)], repr(vals))
    flow_any = app_store.get_historian_rows_range(gateway="gw-A", tag="Flow", limit=100, data_filter=either)
    vals = sorted(float(r["value"]) for r in flow_any)
    check("  ANY: Temp < 22 OR Temp between 37..38 -> cycles 0,1,17,18", vals == [100.0, 101.0, 117.0, 118.0], repr(vals))
    flow_rec = app_store.get_historian_rows_range(gateway="gw-A", tag="Flow", limit=100, data_filter=recipe9)
    vals = sorted(float(r["value"]) for r in flow_rec)
    check("  another gateway's tag is sample-and-held: Recipe = 9 from 10:00:10.5 -> cycles 11..19",
          vals == [111.0 + i for i in range(9)], repr(vals))
    none = app_store.get_historian_rows_range(gateway="gw-A", tag="Flow", limit=100,
                                              data_filter={"conditions": [{"tag_name": "Missing", "operator": "eq", "value1": 1}]})
    check("  a condition on a tag the cycle never has keeps nothing", len(none) == 0, str(len(none)))

    # ---- the bucket reader -------------------------------------------------------------
    b = app_store.bucket_series_rows(gateway="gw-A", tag="Flow", bucket_seconds=5, from_utc="2026-11-16 10:00:00", to_utc="2026-11-16 10:00:20",
                                     data_filter=run_on)
    by = {r.get("ts") or r.get("bucket_utc"): r for r in b}
    counts = {k[-8:]: int(v.get("sample_count") or 0) for k, v in by.items()}
    check("bucketed (5 s) Flow under Run = 1: 5 rows in 10:00:05 and 10:00:10, none in 10:00:00 / 10:00:15",
          counts.get("10:00:05") == 5 and counts.get("10:00:10") == 5 and "10:00:00" not in counts and "10:00:15" not in counts, repr(counts))

    # ---- the stats reader ----------------------------------------------------------------
    st = app_store.get_historian_stats(gateway="gw-A", tag="Flow", from_utc="2026-11-16 10:00:00", data_filter=run_on)
    s0 = st[0] if st else {}
    check("stats under Run = 1: count 10, min 105, max 114", int(s0.get("count") or 0) == 10 and float(s0.get("min") or 0) == 105 and float(s0.get("max") or 0) == 114, repr(s0)[:160])
    st_all = app_store.get_historian_stats(gateway="gw-A", tag="Flow", from_utc="2026-11-16 10:00:00")
    check("  and 20 without the filter (the rollup fast path is untouched)", int((st_all[0] if st_all else {}).get("count") or 0) == 20)

    # ---- the rule-stats reader -------------------------------------------------------------
    rs = app_store.get_historian_rule_stats(rules=[{"id": "r1", "tag_name": "Flow", "operator": "gte", "value1": 110, "aggregation": "count"},
                                                   {"id": "r2", "tag_name": "Flow", "operator": "any", "aggregation": "avg"}],
                                            gateway="gw-A", from_utc="2026-11-16 10:00:00", data_filter=run_on)
    byid = {r["id"]: r for r in rs}
    check("rule stats under Run = 1: Flow >= 110 counts 5; avg of Flow is 109.5",
          int(byid.get("r1", {}).get("value") or 0) == 5 and abs(float(byid.get("r2", {}).get("value") or 0) - 109.5) < 1e-6, repr(rs)[:200])

    # ---- Data Export's builder ----------------------------------------------------------------
    from app.services.data_export import build_query
    sql, params, cols = build_query({"gateways": ["gw-A"], "tags": ["Flow"], "data_filter": run_on}, "tenant-cust-df")
    con = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
    got = con.execute(sql, params).fetchall()
    con.close()
    check("Data Export: the same predicate with ? params -> 10 rows", len(got) == 10 and "EXISTS" in sql, "%d rows" % len(got))

    # ---- in memory (an export sink's cycle) ----------------------------------------------------
    cyc = [{"gateway_id": "gw-A", "ts_utc": "x", "tag_name": "Run", "value": 1}, {"gateway_id": "gw-A", "ts_utc": "x", "tag_name": "Flow", "value": 5}]
    check("in memory: a cycle with Run = 1 passes, Run = 0 does not",
          len(hf.filter_rows(hf.parse_filter(run_on), cyc)) == 2
          and len(hf.filter_rows(hf.parse_filter(run_on), [dict(cyc[0], value=0), cyc[1]])) == 0)
    check("  another gateway's condition asks the live lookup",
          hf.cycle_passes(hf.parse_filter(recipe9), {"Run": 1}, "gw-A", lookup=lambda g, t: 9 if (g, t) == ("gw-B", "Recipe") else None)
          and not hf.cycle_passes(hf.parse_filter(recipe9), {"Run": 1}, "gw-A", lookup=lambda g, t: 7))

    # ---- over HTTP -----------------------------------------------------------------------------
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth import create_access_token
    tok = create_access_token({"sub": "admin", "role": "admin", "tenant_id": "tenant-cust-df", "permissions": {}, "modules": []}, expires_seconds=600)
    client = TestClient(app, client=("127.0.0.1", 50000))
    H = {"Authorization": "Bearer " + tok, "Host": "127.0.0.1:8000"}
    r = client.get("/api/app-store/historian/range", params={"gateway": "gw-A", "tag": "Flow", "limit": 100, "data_filter": json.dumps(run_on)}, headers=H)
    check("GET /historian/range?data_filter=... -> 10 rows", r.status_code == 200 and len(r.json().get("rows") or []) == 10, "%s %s" % (r.status_code, r.text[:100]))
    r = client.get("/api/app-store/historian/agg", params={"gateway": "gw-A", "tag": "Flow", "bucket_seconds": 5, "from_utc": "2026-11-16 10:00:00",
                                                           "to_utc": "2026-11-16 10:00:20", "data_filter": json.dumps(run_on)}, headers=H)
    check("GET /historian/agg?bucket_seconds=5&data_filter=... -> 2 buckets", r.status_code == 200 and len(r.json().get("rows") or []) == 2, "%s %s" % (r.status_code, r.text[:100]))
    r = client.get("/api/app-store/historian/agg", params={"gateway": "gw-A", "tag": "Flow", "bucket": "minute", "from_utc": "2026-11-16 10:00:00",
                                                           "data_filter": json.dumps(run_on)}, headers=H)
    check("  a grain (minute) with a filter buckets in SQL: 1 bucket of 10", r.status_code == 200 and len(r.json().get("rows") or []) == 1, "%s %s" % (r.status_code, r.text[:100]))
    r = client.get("/api/app-store/historian/stats", params={"gateway": "gw-A", "tag": "Flow", "from_utc": "2026-11-16 10:00:00", "data_filter": json.dumps(run_on)}, headers=H)
    check("GET /historian/stats?data_filter=... -> count 10", r.status_code == 200 and int((r.json().get("rows") or [{}])[0].get("count") or 0) == 10, r.text[:120])
    r = client.post("/api/app-store/historian/rule-stats", json={"rules": [{"id": "r1", "tag_name": "Flow", "operator": "gte", "value1": 110, "aggregation": "count"}],
                                                                 "gateway": "gw-A", "from_utc": "2026-11-16 10:00:00", "data_filter": run_on}, headers=H)
    check("POST /historian/rule-stats with data_filter -> 5", r.status_code == 200 and int((r.json().get("rows") or [{}])[0].get("value") or 0) == 5, r.text[:120])
    r = client.post("/api/data-export/preview", json={"gateways": ["gw-A"], "tags": ["Flow"], "data_filter": run_on}, headers=H)
    ok = r.status_code == 200 and (int(r.json().get("total") or r.json().get("count") or len(r.json().get("rows") or [])) == 10)
    check("POST /data-export/preview with data_filter -> 10", ok, "%s %s" % (r.status_code, r.text[:140]))

    # ---- the export sinks (CSV / TXT / Excel engine) -------------------------------------------
    from app.services import plc_manager as pm
    from app.services import file_sinks
    from app.models import GatewayReading
    sink = pm._saved_sink({"id": "c1", "engine": "csv_file", "file_path": "x.csv", "data_filter": run_on})
    check("a saved connection carries its data filter to the worker", sink.get("data_filter") == hf.parse_filter(run_on), repr(sink.get("data_filter")))

    class _W:   # the worker's view: its id and its config
        gateway_id = "gw-A"
        config = type("C", (), {})()
    def _rd(tag, val):
        return GatewayReading(ts_utc="t", tag_name=tag, value=val, source="t", site="s", area="a", equipment="e")
    cyc_on = [_rd("Run", 1.0), _rd("Flow", 5.0)]
    cyc_off = [_rd("Run", 0.0), _rd("Flow", 5.0)]
    kept_on = pm.GatewayWorker._filter_readings_for_sink(_W(), dict(sink, tag_filters=["Flow"]), cyc_on)
    kept_off = pm.GatewayWorker._filter_readings_for_sink(_W(), dict(sink, tag_filters=["Flow"]), cyc_off)
    check("the CSV engine writes a cycle while Run = 1 (Flow only, by its tag filter) and nothing while Run = 0",
          [r.tag_name for r in kept_on] == ["Flow"] and kept_off == [], repr(([r.tag_name for r in kept_on], kept_off)))
    pm._LIVE_MANAGER = type("M", (), {"global_live_values": {"gw-B::Recipe": {"value": 9.0, "ts_epoch": 0}}})()
    sink_b = pm._saved_sink({"id": "c2", "engine": "csv_file", "file_path": "x.csv", "data_filter": recipe9})
    check("  a condition on another gateway reads the manager's live value",
          len(pm.GatewayWorker._filter_readings_for_sink(_W(), sink_b, cyc_on)) == 2)
    pm._LIVE_MANAGER = None
    frows = [{"gateway_id": "gw-A", "ts_utc": "t1", "tag_name": "Run", "value": 1}, {"gateway_id": "gw-A", "ts_utc": "t1", "tag_name": "Flow", "value": 5},
             {"gateway_id": "gw-A", "ts_utc": "t2", "tag_name": "Run", "value": 0}, {"gateway_id": "gw-A", "ts_utc": "t2", "tag_name": "Flow", "value": 6}]
    check("  the meter file sinks keep the cycle that passes", [r["ts_utc"] for r in file_sinks.filter_rows({"data_filter": hf.parse_filter(run_on), "tag_filters": ["Flow"]}, frows)] == ["t1"])

    # ---- a report section --------------------------------------------------------------------
    from app.services.report_renderer import _fetch_multi_series, _DATA_FILTER
    sec = {"type": "chart", "time_range": {"preset": "custom", "from_utc": "2026-11-16 10:00:00", "to_utc": "2026-11-16 10:00:20"},
           "series": [{"gateway_id": "gw-A", "tag_name": "Flow"}], "readings_count": 500, "data_filter": run_on}
    _DATA_FILTER.set(sec["data_filter"])
    _meta, aligned = _fetch_multi_series(sec)
    _DATA_FILTER.set(None)
    pts = [v for (_t, v, _x) in (aligned[0] if aligned else [])]
    check("a report chart section under its data filter plots the 10 readings while Run = 1", sorted(pts) == [105.0 + i for i in range(10)], repr(pts)[:120])
    _meta, aligned = _fetch_multi_series(dict(sec, data_filter=None))
    check("  and all 20 without it", len(aligned[0]) == 20, str(len(aligned[0]) if aligned else None))

    # ---- the pages carry the same editor -------------------------------------------------------------
    fe = os.path.join(ROOT, "frontend", "src")
    rd = lambda *p: open(os.path.join(fe, *p), encoding="utf-8").read()
    api = rd("api.js"); appjs = rd("App.jsx"); dd = rd("components", "Dashboard", "DashboardDesigner.jsx"); dw = rd("components", "Dashboard", "DashboardWidgets.jsx")
    lu = rd("components", "Dashboard", "layoutUtils.js"); rtd = rd("components", "Reports", "ReportTemplateDesigner.jsx"); dx = rd("components", "DataExport", "DataExportPage.jsx")
    check("api.js sends data_filter on range / agg / stats and in the rule-stats body",
          api.count('params.set("data_filter"') == 3 and "data_filter: dataFilter || null" in api)
    check("App.jsx forwards the widget's filter on all six historian calls", appjs.count("cloudEdge: edgeFilter, dataFilter,") == 6)
    check("every widget historian request carries it (rows x5, series x4, stats x2, rules x1)",
          dw.count("fetchWidgetRowsRef.current({ dataFilter: _dfOf(cfg),") == 5 and dw.count("await fetcher({ dataFilter: _dfOf(cfg),") == 4
          and dw.count("fetchWidgetStatsRef.current({ dataFilter: _dfOf(cfg),") == 2 and dw.count("fetchWidgetRuleStatsRef.current({ dataFilter: _dfOf(cfg),") == 1)
    check("the widget editor opens the Data Filter window and the option survives the allowlist",
          "Open Data Filter" in dd and "<DataFilterEditor" in dd and "data_filter: cleanDataFilter(raw?.config?.data_filter)" in lu)
    check("every report section with a trigger offers the Data Filter", "<SectionDataFilter" in rtd and "<DataFilterEditor" in rtd)
    check("the export connection dialog and the Data Export page offer it",
          "setDbDataFilterOpen(true)" in appjs and "data_filter: cleanDataFilter(dbForm.data_filter)" in appjs and "data_filter: cleanDataFilter(conn.data_filter)" in appjs
          and "data_filter: null" in dx and "<DataFilterEditor" in dx)

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
