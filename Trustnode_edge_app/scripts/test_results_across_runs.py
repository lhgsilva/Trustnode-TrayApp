# -*- coding: utf-8 -*-
"""Test results across runs: a day's parts, a month's tests, the last N of a part.

2026-11-16, from the owner's two customers. The generated reports ARE the
test results - each cycle run stores the values read at the time
(meta.columns: Part #, Result ...). Now:

  * the list filters by date, by a column's value and by schedule name;
  * the filtered list exports as CSV and Excel with its result columns;
  * a report section "Past runs table" lists the runs of a template over a
    window with their columns and a count footer (PASS 2 / FAIL 1);
  * a dashboard table mode "Test results" shows the same rows, and a chart
    can label its X axis from a tag (a part number per test).

    python scripts/test_results_across_runs.py
"""
from __future__ import annotations

import csv
import io
import os
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="tn_runs_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-runs"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "frontend", "src")
sys.path.insert(0, os.path.join(ROOT, "backend"))
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-70s %s%s" % (name, "PASS" if ok else "FAIL", ("  - " + detail) if (not ok and detail) else ""))


def main() -> int:
    print("test results across runs")
    from app.state import reports_store
    from app.tenant import set_current_tenant
    set_current_tenant("tenant-cust-runs")
    tpl = reports_store.upsert_template({"name": "Part test", "definition": {"sections": [{"type": "header", "title": "Part"}]}})
    tid = tpl["id"]
    other = reports_store.upsert_template({"name": "Other", "definition": {"sections": []}})
    now = time.time()
    runs = [("WO-1", "PASS", 3 * 3600), ("WO-2", "FAIL", 2 * 3600), ("WO-3", "PASS", 3600), ("WO-2", "PASS", 600)]
    ids = []
    for part, result, ago in runs:
        rec = reports_store.insert_generated({"template_id": tid, "template_name": "Part test", "schedule_id": "sch-parts",
                                              "schedule_name": "Line 1 parts", "triggered_by": "cycle", "file_path": "",
                                              "file_name": part + ".pdf", "file_bytes": 1, "file_sha256": "",
                                              "meta": {"render_state": "ready", "columns": {"Part #": part, "Result": result}}})
        ids.append(rec["id"])
    # back-date them
    import sqlite3
    con = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
    for rid, (_p, _r, ago) in zip(ids, runs):
        con.execute("UPDATE generated_reports SET created_utc = ? WHERE id = ?",
                    (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - ago)) + ".000", rid))
    con.commit(); con.close()
    reports_store.insert_generated({"template_id": other["id"], "template_name": "Other", "schedule_id": "sch-x",
                                    "schedule_name": "Other", "triggered_by": "manual", "file_path": "", "file_name": "o.pdf",
                                    "file_bytes": 1, "file_sha256": "", "meta": {"render_state": "ready"}})

    # ---- filters -------------------------------------------------------------
    got = reports_store.list_generated(column="Part #", value="WO-2")
    check("filter by a column's value: the two WO-2 runs", [g["file_name"] for g in got] == ["WO-2.pdf", "WO-2.pdf"], repr([g["file_name"] for g in got]))
    frm = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 1.5 * 3600))
    got = reports_store.list_generated(from_utc=frm, template_id=tid)
    check("  by date: the runs of the last 90 minutes", sorted(g["file_name"] for g in got) == ["WO-2.pdf", "WO-3.pdf"], repr([g["file_name"] for g in got]))
    got = reports_store.list_generated(schedule_name="line 1")
    check("  by schedule name (case-insensitive, contains)", len(got) == 4, repr(len(got)))
    got = reports_store.list_generated(column="Result", value="FAIL")
    check("  the failed parts", [g["file_name"] for g in got] == ["WO-2.pdf"] and (got[0]["meta"]["columns"]["Part #"] == "WO-2"), repr(got and got[0]["file_name"]))

    # ---- export over HTTP ------------------------------------------------------
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth import create_access_token
    tok = create_access_token({"sub": "admin", "role": "admin", "tenant_id": "tenant-cust-runs", "permissions": {}, "modules": []}, expires_seconds=600)
    client = TestClient(app, client=("127.0.0.1", 50000))
    H = {"Authorization": "Bearer " + tok, "Host": "127.0.0.1:8000"}
    r = client.get("/api/reports/generated/export.csv", params={"template_id": tid}, headers=H)
    lines = list(csv.reader(io.StringIO(r.text))) if r.status_code == 200 else []
    check("the list exports as CSV with its result columns",
          r.status_code == 200 and lines and lines[0][-2:] == ["Part #", "Result"] and len(lines) == 5 and lines[1][-2:] == ["WO-2", "PASS"],
          "%s %r" % (r.status_code, lines[:2]))
    r = client.get("/api/reports/generated/export.xlsx", params={"template_id": tid, "column": "Result", "value": "PASS"}, headers=H)
    ok_x = r.status_code == 200 and r.content[:2] == b"PK"
    if ok_x:
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(r.content)).active
        grid = [[c.value for c in row] for row in ws.iter_rows()]
        ok_x = grid[0][-2:] == ["Part #", "Result"] and len(grid) == 4 and all(g[-1] == "PASS" for g in grid[1:])
    check("  and as Excel, filtered to the passed parts", ok_x, "%s" % r.status_code)
    r = client.get("/api/reports/generated", params={"column": "Part #", "value": "wo-3"}, headers=H)
    check("  the list route takes the same filters", r.status_code == 200 and [g["file_name"] for g in r.json()["generated"]] == ["WO-3.pdf"], r.text[:120])

    # ---- the report section ---------------------------------------------------
    from app.services.report_renderer import build_runs_table, apply_text_tokens, build_template_render_data
    sec = {"type": "runs_table", "title": "Today's parts", "time_range": {"preset": "24h"}, "runs_template_id": tid,
           "count_column": "Result", "count_label": "Count"}
    header, body = build_runs_table(sec)
    check("a 'Past runs' section lists the template's runs over the window", header == ["Run", "Part #", "Result"] and len(body) == 5, repr((header, len(body))))
    check("  oldest first, each with its stored columns", [r_[1] for r_ in body[:4]] == ["WO-1", "WO-2", "WO-3", "WO-2"], repr([r_[1:] for r_ in body[:4]]))
    check("  with a count footer (PASS 3 / FAIL 1)", body[-1][0] == "Count" and body[-1][2] in ("PASS 3 / FAIL 1", "FAIL 1 / PASS 3"), repr(body[-1]))
    sec2 = {"type": "runs_table", "title": "WO-2 history", "time_range": {"preset": "none"}, "runs_template_id": tid,
            "runs_filter_column": "Part #", "runs_filter_value": "WO-2", "runs_columns": ["Result"]}
    h2, b2 = build_runs_table(sec2)
    check("  'the last tests of one part number' - WO-2's two runs, chosen columns", h2 == ["Run", "Result"] and [r_[1] for r_ in b2] == ["FAIL", "PASS"], repr(b2))
    tplo = apply_text_tokens({"id": tid, "name": "Part test", "definition": {"sections": [dict(sec, runs_template_id="")]}})
    data = build_template_render_data(tplo)
    tbl = [s for s in (data.get("sections") or []) if s.get("type") == "table"]
    check("  with no template chosen it reads THIS template's runs, and the data preview carries it",
          bool(tbl) and tbl[0]["row_count"] == 5 and tbl[0]["header"] == ["Run", "Part #", "Result"], repr(tbl[:1])[:200])

    # ---- the pages ---------------------------------------------------------------
    pg = open(os.path.join(SRC, "components", "Reports", "ScheduledReportsManager.jsx"), encoding="utf-8").read()
    check("the Generated Reports page filters by date and column value and exports CSV / Excel",
          'type="datetime-local" value={filterFrom}' in pg and "handleExportList(\"xlsx\")" in pg and "downloadGeneratedListExport" in pg)
    dw = open(os.path.join(SRC, "components", "Dashboard", "DashboardWidgets.jsx"), encoding="utf-8").read()
    dd = open(os.path.join(SRC, "components", "Dashboard", "DashboardDesigner.jsx"), encoding="utf-8").read()
    check("a dashboard table can show test results (one row per generated report)",
          'String(cfg?.table_mode || "rows") === "test_results"' in dw and '<option value="test_results">' in dd)
    check("a chart can label its X axis from a tag", "x_label_tag" in dw and "labelOf: xLabelOf" in dw and 'x_label_tag: e.target.value' in dd)
    rd = open(os.path.join(SRC, "components", "Reports", "ReportTemplateDesigner.jsx"), encoding="utf-8").read()
    check("the report builder offers the Past runs table", '{ type: "runs_table", label: "Past runs table"' in rd and 'case "runs_table":' in rd)

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
