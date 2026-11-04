# -*- coding: utf-8 -*-
"""A chart can aggregate every N seconds, and the choice is honoured end to end.

2026-11-02, asked: "a chart that prints temp, pressure and flow for 2-3
hours could be aggregated every 2 seconds with the average of the two
readings - for the reports and for the live dashboard widgets."

What existed: a report section had a Bucket (1 m and up) and a per-series
aggregation, but both were applied AFTER a fetch that was capped at 5 000
raw rows and silently switched to MINUTE buckets past two hours - so a
3-hour test at 1 Hz drew 180 points whatever the section said. Dashboard
charts had no aggregation at all and dropped to minute buckets above their
reading limit.

Now: `bucket_series_rows` groups in SQL on the epoch for any N (avg, min,
max, last, sum, count per bucket); the report section's bucket is the FETCH
resolution; a raw window too dense for the cap takes the finest step that
fits (2 s for 3 h) and the caption says so; /historian/agg takes
bucket_seconds for the dashboard.

    python scripts/test_chart_bucket_seconds.py
"""
from __future__ import annotations

import inspect
import os
import sqlite3
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="tn_bucket_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-bkt"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-66s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def _ts(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) + ".000"


def main() -> int:
    print("a chart aggregates every N seconds, honoured from the store to the page")
    from app.state import app_store
    from app.tenant import set_current_tenant
    set_current_tenant("tenant-cust-bkt")
    from app.services import report_renderer as rr

    # 3 hours at 1 Hz, two tags: value = second index, so bucket maths is exact.
    now = int(time.time()) - 60
    start = now - 3 * 3600
    con = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
    batch = []
    for i in range(3 * 3600):
        for tag in ("Temp", "Flow"):
            batch.append(("tenant-cust-bkt", _ts(start + i), "test", "gw1", "PLC", "d", "10.0.0.1", "db",
                          tag, float(i), None, "REAL", 192, "GOOD", _ts(start + i)))
    con.executemany("INSERT INTO historian_readings(tenant_id,ts_utc,source,gateway_id,gateway_name,"
                    "device_name,plc_ip,database_name,tag_name,value,value_text,data_type,quality,"
                    "quality_label,created_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
    con.commit()
    con.close()
    a, b = _ts(start), _ts(start + 3 * 3600 - 1)

    # ---- the store: any N, exact ----------------------------------------
    t0 = time.perf_counter()
    rows = app_store.bucket_series_rows(from_utc=a, to_utc=b, gateway="gw1", tag="Temp", bucket_seconds=2)
    took = time.perf_counter() - t0
    check("3 h at 1 Hz bucketed every 2 s gives ~5 400 rows", 5390 <= len(rows) <= 5401, "%d rows" % len(rows))
    check("  in well under a second", took < 1.0, "%.3fs" % took)
    full = [r for r in rows if r["sample_count"] == 2]
    r0 = full[len(full) // 2]
    i0 = float(r0["value_min"])
    check("  each bucket holds two readings and the average of both",
          r0["sample_count"] == 2 and abs(float(r0["value"]) - (i0 + 0.5)) < 1e-9
          and float(r0["value_max"]) == i0 + 1 and float(r0["value_last"]) == i0 + 1
          and abs(float(r0["value_sum"]) - (2 * i0 + 1)) < 1e-9, repr(r0))
    check("  bucket stamps are the same text the other readers use",
          all(len(r["ts"]) == 19 and " " in r["ts"] for r in rows[:50]))
    rows5 = app_store.bucket_series_rows(from_utc=a, to_utc=b, gateway="gw1", tag="Temp", bucket_seconds=5)
    check("a 5 s bucket gives ~2 160 rows", 2150 <= len(rows5) <= 2161, "%d" % len(rows5))
    both = app_store.bucket_series_rows(from_utc=a, to_utc=b, gateway="gw1", tag="", bucket_seconds=60)
    check("with no tag, every tag is bucketed separately", 2 * 180 <= len(both) <= 2 * 181, "%d" % len(both))
    other = app_store.bucket_series_rows(from_utc=a, to_utc=b, gateway="gw1", tag="TEMP", bucket_seconds=2)
    check("the tag's case does not hide the series here either", len(other) == len(rows))

    # ---- the endpoint ----------------------------------------------------
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth import create_access_token
    tok = create_access_token({"sub": "admin", "role": "admin", "tenant_id": "tenant-cust-bkt",
                               "permissions": {}, "modules": []}, expires_seconds=600)
    client = TestClient(app, client=("127.0.0.1", 50000))
    r = client.get("/api/app-store/historian/agg", params={"bucket_seconds": 2, "from_utc": a, "to_utc": b,
                                                           "gateway": "gw1", "tag": "Temp", "limit": 50000},
                   headers={"Authorization": "Bearer " + tok, "Host": "127.0.0.1:8000"})
    body = r.json() if r.status_code == 200 else {}
    check("/historian/agg?bucket_seconds=2 serves the dashboard the same rows",
          r.status_code == 200 and body.get("bucket") == "2s" and 5390 <= len(body.get("rows") or []) <= 5401,
          "%s %s" % (r.status_code, r.text[:120]))

    # ---- the report section: its bucket is the fetch resolution ----------
    def ser(tag, agg=""):
        d = {"id": "s-" + tag, "label": tag, "gateway_id": "gw1", "tag_name": tag, "color": "#14a89a",
             "axis": "left", "multiplier": 1, "offset": 0}
        if agg:
            d["aggregation"] = agg
        return d
    sec2 = {"type": "line_chart", "title": "every 2 s", "bucket": "2s", "readings_count": 5000,
            "time_range": {"preset": "custom", "from_utc": a, "to_utc": b},
            "series": [ser("Temp"), ser("Flow", "max")]}
    meta, aligned = rr._fetch_multi_series(sec2)
    check("a section set to 'every 2 s' over 3 h carries ~5 400 points per series",
          all(5390 <= len(pts) <= 5401 for pts in aligned), "%r" % [len(p) for p in aligned])
    mid = aligned[0][len(aligned[0]) // 2]
    check("  average of the pair for a series with no aggregation",
          mid[1] is not None and abs(mid[1] - (int(mid[1]) + 0.5)) < 1e-9, repr(mid))
    midmax = aligned[1][len(aligned[1]) // 2]
    # Both series hold identical values, so at the same bucket the Maximum
    # of the pair is the Average plus a half.
    check("  the maximum of the pair for a series set to Maximum",
          midmax[1] is not None and mid[1] is not None and abs(float(midmax[1]) - (float(mid[1]) + 0.5)) < 1e-9,
          "%r vs avg %r" % (midmax, mid))
    check("  2 s / 5 s / 10 s / 30 s are offered in the designer",
          all(('value: "%s"' % v) in open(os.path.join(os.path.dirname(__file__), "..", "frontend", "src",
                                                         "components", "Reports", "ReportTemplateDesigner.jsx"),
                                            encoding="utf-8").read() for v in ("2s", "5s", "10s", "30s")))

    # ---- raw sections: the finest step that fits, and it says so ----------
    raw3h = {"type": "line_chart", "title": "raw 3 h", "readings_count": 5000,
             "time_range": {"preset": "custom", "from_utc": a, "to_utc": b}, "series": [ser("Temp")]}
    meta, aligned = rr._fetch_multi_series(raw3h)
    check("a raw section over 3 h is bucketed at 2 s, not at 1 minute",
          5390 <= len(aligned[0]) <= 5401, "%d points" % len(aligned[0]))
    check("  and the section carries a caption saying so",
          "averaged every 2s" in str(raw3h.get("_auto_bucket_note") or ""), repr(raw3h.get("_auto_bucket_note")))
    check("  which the chart prints", '_fetch_section.get("_auto_bucket_note")' in inspect.getsource(rr._section_chart))
    raw1h = {"type": "line_chart", "title": "raw 1 h", "readings_count": 5000,
             "time_range": {"preset": "custom", "from_utc": _ts(start), "to_utc": _ts(start + 3599)},
             "series": [ser("Temp")]}
    meta, aligned = rr._fetch_multi_series(raw1h)
    check("a raw section over 1 h stays raw (3 600 readings)", len(aligned[0]) == 3600 and not raw1h.get("_auto_bucket_note"),
          "%d points, note=%r" % (len(aligned[0]), raw1h.get("_auto_bucket_note")))

    # ---- the dashboard side ----------------------------------------------
    fe = os.path.join(os.path.dirname(__file__), "..", "frontend", "src")
    with open(os.path.join(fe, "components", "Dashboard", "DashboardWidgets.jsx"), encoding="utf-8") as fh:
        w = fh.read()
    with open(os.path.join(fe, "components", "Dashboard", "layoutUtils.js"), encoding="utf-8") as fh:
        lu = fh.read()
    with open(os.path.join(fe, "App.jsx"), encoding="utf-8") as fh:
        appjs = fh.read()
    # 2026-11-04: the server-query fetches send the EFFECTIVE bucket (the
    # explicit one, or the window-derived one when none is set).
    _sites = (w.count("bucketSeconds: bucketSecondsFor(cfg), bucketAgg: bucketAggFor(cfg)")
              + w.count("bucketSeconds: effectiveBucketSeconds, bucketAgg: bucketAggFor(cfg)"))
    check("chart widgets send their bucket on every series fetch", _sites >= 6, "%d sites" % _sites)
    check("  a bucketed widget is served from the store, not the 1 Hz stream",
          "bucketSecondsFor(cfg) <= 0" in w)
    check("  the two keys survive the widget allowlist (the normalise trap)",
          "bucket_interval" in lu and "bucket_aggregation" in lu)
    check("  the page fetcher maps avg/min/max/last onto the row value",
          'min: "value_min", max: "value_max", last: "value_last"' in appjs)

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
