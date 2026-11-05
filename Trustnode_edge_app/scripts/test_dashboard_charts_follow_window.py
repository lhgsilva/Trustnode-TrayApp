# -*- coding: utf-8 -*-
"""Every series of a dashboard chart shows the chosen window, live.

2026-11-04, reported with a screenshot: a 3-series chart set to "Last 6
hours", 100 points, "Every reading" drew 30 seconds. The first series was
fetched over 6 h; the other series were fetched as the newest 100 raw
readings; live raw rows were appended to both; and the merge kept the newest
100 timestamps - the last seconds. The trigger chart, likewise, loaded the
newest 5000 readings instead of the run from its start signal.

  * one bucket governs every series (explicit "Aggregate every" wins, else
    derived from the window and Reading points) - checked under node;
  * the live tail is folded into that bucket (avg/min/max/last) so the chart
    stays live without crowding out history;
  * a long window is thinned evenly, not cut to its newest end;
  * three series over 6 h of 0.5 s data come back from the API as <= 100
    points each that span the window - the same request the widget makes;
  * the chart code uses all of this, and the trigger chart loads its history
    from the trigger start;
  * (2026-11-05, reported at a customer: "only the last few items" on every
    preset) the window is fetched on a slow tick, one fetch per widget at a
    time, a failed read keeps the series, and live rows at the gateway
    interval cause no window fetch - test_dashboard_charts_live_light.mjs
    renders the real widget in jsdom and counts the fetches.

    python scripts/test_dashboard_charts_follow_window.py
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "frontend", "src")
DW = os.path.join(SRC, "components", "Dashboard", "DashboardWidgets.jsx")
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-70s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def _fn(src, name):
    m = re.search(r"(?:export )?function %s\(.*?\n\}\n" % re.escape(name), src, re.S)
    return m.group(0) if m else ""


def main() -> int:
    print("every series of a dashboard chart shows the chosen window")
    src = open(DW, encoding="utf-8").read()
    consts = re.search(r"export const BUCKET_INTERVAL_SECONDS = \{.*?\};", src, re.S)
    steps = re.search(r"const AUTO_BUCKET_STEPS = \[[^\]]*\];", src)
    body = "\n".join([
        consts.group(0) if consts else "", steps.group(0) if steps else "",
        _fn(src, "bucketSecondsFor"), _fn(src, "autoBucketSecondsFor"), _fn(src, "windowSpanSeconds"),
        _fn(src, "effectiveBucketSeconds"), _fn(src, "effectiveBucketSeconds_"),
        _fn(src, "bucketLiveTail"), _fn(src, "thinTimestamps"),
    ])
    ana = os.path.join(SRC, "components", "Dashboard", "dashboardAnalytics.js").replace("\\", "/")
    script = 'import { toTsMs } from "file:///%s";\n%s\n' % (ana, body) + r'''
const h6 = { fromUtc: "2026-11-04T06:00:00.000Z", toUtc: "2026-11-04T12:00:00.000Z" };
const t0 = Date.parse("2026-11-04T12:00:00Z");
const tail = [];
for (let i = 0; i < 20; i++) tail.push({ ts: new Date(t0 + i * 500).toISOString(), gateway_id: "gw", tag: "A", value: i });
const b = bucketLiveTail(tail, 5, "avg"), bmax = bucketLiveTail(tail, 5, "max"), blast = bucketLiveTail(tail, 5, "last");
const ts = Array.from({ length: 10000 }, (_, i) => i);
const thin = thinTimestamps(ts, 1500);
console.log(JSON.stringify({
  auto6h: effectiveBucketSeconds({}, h6, 100, 500),
  explicit: effectiveBucketSeconds({ bucket_interval: "1m" }, h6, 100, 500),
  rolling: effectiveBucketSeconds({}, null, 100, 500),
  short: effectiveBucketSeconds({}, { fromUtc: "2026-11-04T11:59:30Z", toUtc: "2026-11-04T12:00:00Z" }, 100, 500),
  tailN: b.length, tailAvg: b.map((r) => r.value), tailMax: bmax.map((r) => r.value), tailLast: blast.map((r) => r.value),
  thinN: thin.length, thinEnds: [thin[0], thin[thin.length - 1]],
}));
'''
    res = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60, cwd=SRC)
    try:
        got = json.loads(res.stdout.strip())
    except Exception:
        got = {}
        check("the chart helpers run under node", False, (res.stdout + res.stderr)[:300])
    check("6 h, 100 points, 0.5 s readings -> one 300 s bucket for every series", got.get("auto6h") == 300, repr(got.get("auto6h")))
    check("  an explicit 'Aggregate every 1 min' wins", got.get("explicit") == 60, repr(got.get("explicit")))
    check("  rolling with no time range stays raw (live)", got.get("rolling") == 0, repr(got.get("rolling")))
    check("  a window that already fits stays raw", got.get("short") == 0, repr(got.get("short")))
    check("the live tail folds into buckets: 20 readings -> 2 buckets of 5 s",
          got.get("tailN") == 2 and got.get("tailAvg") == [4.5, 14.5], repr(got.get("tailAvg")))
    check("  with max and last as chosen", got.get("tailMax") == [9, 19] and got.get("tailLast") == [9, 19],
          repr((got.get("tailMax"), got.get("tailLast"))))
    check("a long window is thinned evenly and keeps both ends",
          got.get("thinN") == 1500 and got.get("thinEnds") == [0, 9999], repr((got.get("thinN"), got.get("thinEnds"))))

    # ---- the request the widget makes, against a real store ---------------
    tmp = tempfile.mkdtemp(prefix="tn_chartwin_")
    atexit.register(lambda: shutil.rmtree(tmp, ignore_errors=True))
    os.environ.update({"TRUSTNODE_APP_STORE_PATH": os.path.join(tmp, "s.db"), "TRUSTNODE_DATA_DIR": tmp,
                       "TRUSTNODE_SKIP_DOTENV": "1", "TRUSTNODE_BOOT_INTEGRITY_CHECK": "never",
                       "TRUSTNODE_TENANT_ID": "tenant-cust-chart"})
    sys.path.insert(0, os.path.join(ROOT, "backend"))
    from app.state import app_store  # noqa: F401
    from app.tenant import set_current_tenant
    set_current_tenant("tenant-cust-chart")
    now = int(time.time()) - 30

    def _ts(t):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) + ".%03d" % int((t % 1) * 1000)
    con = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
    con.execute("PRAGMA journal_mode=WAL")
    rows = []
    t = now - 6 * 3600
    while t < now:
        for k, tag in enumerate(("Flow", "Temp", "Press")):
            rows.append(("tenant-cust-chart", _ts(t), "test", "gw1", "PLC", "d", "10.0.0.1", "db", tag,
                         float(k * 10 + (t % 60)), None, "REAL", 192, "GOOD", _ts(t)))
        t += 0.5
    con.executemany("INSERT INTO historian_readings(tenant_id,ts_utc,source,gateway_id,gateway_name,device_name,"
                    "plc_ip,database_name,tag_name,value,value_text,data_type,quality,quality_label,created_utc) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth import create_access_token
    tok = create_access_token({"sub": "admin", "role": "admin", "tenant_id": "tenant-cust-chart",
                               "permissions": {}, "modules": []}, expires_seconds=600)
    client = TestClient(app, client=("127.0.0.1", 50000))
    frm = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 6 * 3600))
    to = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now))
    spans = {}
    for tag in ("Flow", "Temp", "Press"):
        t0 = time.perf_counter()
        r = client.get("/api/app-store/historian/agg", headers={"Authorization": "Bearer " + tok, "Host": "127.0.0.1:8000"},
                       params={"bucket_seconds": 300, "from_utc": frm, "to_utc": to, "gateway": "gw1", "tag": tag,
                               "limit": int(6 * 3600 / 300) + 20})
        dt = time.perf_counter() - t0
        got_rows = (r.json() or {}).get("rows") or [] if r.status_code == 200 else []
        stamps = sorted(str(x.get("ts") or "") for x in got_rows)
        spans[tag] = (len(got_rows), stamps[0][:16] if stamps else "", stamps[-1][:16] if stamps else "", round(dt, 2))
    ok_all = all(2 <= v[0] <= 100 for v in spans.values())
    first = min(v[1] for v in spans.values()) if spans else ""
    check("3 series over 6 h of 0.5 s data: each <= 100 bucketed points", ok_all, repr(spans))
    check("  and each spans the whole window, not its last seconds",
          all(v[1] and v[1] <= time.strftime("%Y-%m-%d %H:%M", time.gmtime(now - 6 * 3600 + 600)) for v in spans.values()),
          "first points: %r" % {k: v[1] for k, v in spans.items()})

    # ---- the chart code uses it ------------------------------------------
    check("the extra series are fetched with the widget's bucket over the window",
          "bucketSeconds: extraBucket" in src and "limit: extraLimit" in src)
    check("the primary's live tail joins as buckets", "bucketLiveTail(append, serverQueryBucket" in src)
    check("  and the extras' live tail too", "bucketLiveTail(filteredLive.filter(" in src)
    check("a time window keeps the whole window (no newest-N cut)",
          "if (hasExplicitWindow) {\n      sortedTs = thinTimestamps(" in src.replace("\r\n", "\n"))
    check("the trigger chart loads its history from the trigger start",
          "fromUtc: trigStart > 0 ? new Date(trigStart).toISOString()" in src
          and "trigWin?.start, trigWin?.end]" in src)

    check("a trend chart fetches from the server on the slow tick, not every gateway tick",
          "const windowQueryTick = trendChart ? windowRefreshTick : refreshTickFast;" in src
          and ("    windowQueryTick," + chr(10) + "  ]);") in src.replace(chr(13) + chr(10), chr(10)))
    check("  a failed extra-series read keeps what the chart already has", "next[def.id] = null;" in src)
    live = subprocess.run(["node", os.path.join(ROOT, "scripts", "test_dashboard_charts_live_light.mjs")],
                          capture_output=True, text=True, timeout=180, cwd=ROOT)
    check("the real widget, rendered: live every 200 ms, the window fetched once per series",
          live.returncode == 0 and "RESULT: PASS" in live.stdout,
          (live.stdout.strip().splitlines() or [""])[-1] + " " + live.stderr.strip()[-200:])

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
