# -*- coding: utf-8 -*-
"""A STRING tag prints in a report exactly as the PLC sent it.

2026-11-10, reported: "the work details of my report are linked to a string
tag and when generating the report the edge adds .000". The collector stores
a numeric-looking STRING reading twice - value=76668.0 AND value_text='76668'
(plc_manager._coerce_value keeps the text and also the number when it parses)
- and every report path that shows a single reading preferred the number and
formatted it as a measurement: "76668.000" in a text token or data table,
"76668" / "77.00" in a KPI or pill. A work order such as 'WO-4471' has no
number, so it always printed correctly - which is why only some did it.

  * {tag:WO}, {WO}, {last:WO}, {first:WO} print the text;
  * a KPI / pill / segment cell set to latest prints the text;
  * the timestamped data table prints the text;
  * a numeric tag is unchanged (77.500 in a token), and an average over the
    string tag is still a number.

    python scripts/test_string_tag_prints_text.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="tn_strtag_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-str"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-64s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def _ts(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) + ".000"


def main() -> int:
    print("a string tag prints as its text in every report path")
    from app.state import app_store  # noqa: F401  (creates the schema)
    from app.tenant import set_current_tenant
    set_current_tenant("tenant-cust-str")
    from app.services import report_renderer as rr

    now = int(time.time()) - 120
    rows = []
    for i in range(60):
        t = now - 60 + i
        # a numeric-looking work order, stored the way the collector stores it
        rows.append((_ts(t), "WO", 76668.0 if i < 59 else 76669.0, "76668" if i < 59 else "76669", "STRING"))
        rows.append((_ts(t), "WO2", None, "WO-4471", "STRING"))
        rows.append((_ts(t), "Temp", 77.5, None, "REAL"))
    con = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
    con.executemany(
        "INSERT INTO historian_readings(tenant_id,ts_utc,source,gateway_id,gateway_name,device_name,plc_ip,"
        "database_name,tag_name,value,value_text,data_type,quality,quality_label,created_utc) "
        "VALUES('tenant-cust-str',?, 'test','gw1','PLC','d','10.0.0.1','db',?,?,?,?,192,'GOOD',?)",
        [(ts, tag, v, txt, dt, ts) for ts, tag, v, txt, dt in rows])
    con.commit()
    con.close()
    a, b = _ts(now - 61), _ts(now + 1)

    # ---- text tokens ------------------------------------------------------
    out = rr.resolve_text_tokens("{tag:WO}|{WO}|{first:WO}|{last:WO}|{tag:WO2}|{tag:Temp}|{avg:WO}", a, b, "gw1")
    parts = out.split("|")
    check("{tag:WO} prints the work order, no decimals", parts[0:1] == ["76669"], out)
    check("  {WO} (bare token) too", parts[1:2] == ["76669"], out)
    check("  {first:WO} prints the first one", parts[2:3] == ["76668"], out)
    check("  {last:WO} prints the latest one", parts[3:4] == ["76669"], out)
    check("  a non-numeric work order still prints", parts[4:5] == ["WO-4471"], out)
    check("  a numeric tag is unchanged (77.500)", parts[5:6] == ["77.500"], out)
    check("  an average over the string tag is still a number", parts[6:7] and parts[6].startswith("766") and "." in parts[6], out)

    # ---- KPI / pill / segment cells --------------------------------------
    v, n, txt = rr._fetch_kpi_value({"gateway_id": "gw1", "tag_name": "WO", "aggregation": "latest"}, a, b)
    check("a KPI or pill set to latest prints the text", v is None and txt == "76669", repr((v, n, txt)))
    v, n, txt = rr._fetch_kpi_value({"gateway_id": "gw1", "tag_name": "Temp", "aggregation": "latest"}, a, b)
    check("  a numeric KPI is unchanged", txt is None and v == 77.5, repr((v, n, txt)))
    v, n, txt = rr._fetch_kpi_value({"gateway_id": "gw1", "tag_name": "WO", "aggregation": "avg"}, a, b)
    check("  an average over the string tag is still a number", txt is None and v is not None and v > 76668, repr((v, n, txt)))

    # ---- the timestamped data table ---------------------------------------
    section = {"type": "data_table", "title": "work", "readings_count": 100,
               "time_range": {"preset": "custom", "from_utc": a, "to_utc": b},
               "series": [{"id": "s1", "label": "Work order", "gateway_id": "gw1", "tag_name": "WO",
                           "color": "#14a89a", "axis": "left", "multiplier": 1, "offset": 0},
                          {"id": "s2", "label": "Temp", "gateway_id": "gw1", "tag_name": "Temp",
                           "color": "#14a89a", "axis": "left", "multiplier": 1, "offset": 0}]}
    header, body = rr.build_data_table_rows(section)
    iw, it = header.index("Work order"), header.index("Temp")
    wo_col = [r[iw] for r in body]
    check("the data table prints the work order as text", bool(wo_col) and all(c in ("76668", "76669") for c in wo_col),
          repr(wo_col[:3]))
    check("  and the numeric column is unchanged", bool(body) and all(r[it] == "77.500" for r in body), repr([r[it] for r in body[:3]]))
    # A chart section's table view builds its rows separately.
    ch = dict(section, type="line_chart")
    cheader, cbody = rr.build_chart_section_rows(ch)
    cw = cheader.index("Work order")
    check("a chart's table view prints the work order as text",
          bool(cbody) and all(r[cw] in ("76668", "76669") for r in cbody), repr([r[cw] for r in cbody[:3]]))


    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
