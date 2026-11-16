# -*- coding: utf-8 -*-
"""The part-test and TCU paths: what blocked them, fixed and held.

2026-11-16, from the owner's two customers (parts tested one by one with a
pass/fail result; TCU test cycles with KPIs). Found and fixed:

  1. a batch stopped by a PLC trigger was never evaluated (stayed PENDING);
  2. the batch dashboard widgets read the legacy table (never a v2 batch);
  3. the Data Export download ignored Pivot (preview pivoted, file did not);
  4. dashboard tables showed "-" for a text value (a part number) and had
     no PASS/FAIL mask;
  5. a text part number as a cycle report's id came back empty, a numeric
     one as "76668.0";
  6. duration / ramp / rate KPIs existed in the engine but not in the menus;
  7. the Excel sink was long-only and blank for strings.

    python scripts/test_part_test_paths.py
"""
from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="tn_parts_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-parts"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "frontend", "src")
sys.path.insert(0, os.path.join(ROOT, "backend"))
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-70s %s%s" % (name, "PASS" if ok else "FAIL", ("  - " + detail) if (not ok and detail) else ""))


def main() -> int:
    print("the part-test and TCU paths")
    from app.state import app_store
    from app.tenant import set_current_tenant
    set_current_tenant("tenant-cust-parts")

    # ---- 1. a PLC-stopped batch is evaluated -------------------------------
    from app.modules.batch_management import triggers_v2 as tv2
    calls = []

    class _Exe:
        _app_store = app_store

        def list_batches(self, status=None, definition_id=None, limit=50):
            return ([{"id": "b1"}] if status == "running" else []), 1

        def stop_batch(self, bid, source=None, reason=None):
            calls.append(("stop", bid))

        def abort_batch(self, bid, source=None, reason=None):
            calls.append(("abort", bid))
    from app.modules.batch_management import calc_v2, reports_v2
    _c, _r = calc_v2.BatchCalcService.compute_batch, reports_v2.ReportIntegrationService.on_batch_terminal
    calc_v2.BatchCalcService.compute_batch = lambda self, bid: calls.append(("compute", bid))
    reports_v2.ReportIntegrationService.on_batch_terminal = lambda self, bid, actor=None: calls.append(("report", bid))
    try:
        tv2._dispatch(_Exe(), None, {"trigger_scope": "BATCH_STOP", "definition_id": "d1"})
    finally:
        calc_v2.BatchCalcService.compute_batch, reports_v2.ReportIntegrationService.on_batch_terminal = _c, _r
    check("a batch stopped by a PLC trigger is computed and auto-reported",
          calls == [("stop", "b1"), ("compute", "b1"), ("report", "b1")], repr(calls))

    # ---- 2. the widgets read v2 ---------------------------------------------
    wsrc = open(os.path.join(SRC, "components", "BatchManagement", "BatchManagementWidgets.jsx"), encoding="utf-8").read()
    check("the batch widgets read the v2 batches and show the v2 result",
          "bmv2ListBatches" in wsrc and "quality_status" in wsrc
          and wsrc.count("listBatches({") == 0, "%d legacy calls left" % wsrc.count("listBatches({"))

    # ---- 3. Data Export pivot in the FILE ------------------------------------
    now = int(time.time()) - 600
    rows = []
    for i in range(0, 60, 10):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now + i)) + ".000"
        for tag, v in (("Flow", 10.0 + i), ("Temp", 20.0 + i)):
            rows.append({"ts_utc": ts, "gateway_id": "gw1", "gateway_name": "PLC", "device_name": "d", "plc_ip": "1.1.1.1",
                         "database_name": "db", "tag_name": tag, "value": v, "quality": 192, "quality_label": "GOOD", "source": "t"})
        rows.append({"ts_utc": ts, "gateway_id": "gw1", "gateway_name": "PLC", "device_name": "d", "plc_ip": "1.1.1.1",
                     "database_name": "db", "tag_name": "WO", "value": None, "value_text": "WO-%d" % i, "quality": 192,
                     "quality_label": "GOOD", "source": "t"})
    app_store.append_historian_rows(rows)
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth import create_access_token
    tok = create_access_token({"sub": "admin", "role": "admin", "tenant_id": "tenant-cust-parts",
                               "permissions": {}, "modules": []}, expires_seconds=600)
    client = TestClient(app, client=("127.0.0.1", 50000))
    H = {"Authorization": "Bearer " + tok, "Host": "127.0.0.1:8000"}
    frm = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 5)) + "Z"
    to = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now + 120)) + "Z"
    spec = {"gateways": ["gw1"], "tags": ["Flow", "Temp", "WO"], "from_utc": frm, "to_utc": to,
            "columns": ["ts_utc", "tag_name", "value", "value_text"], "pivot": True, "order": "asc"}
    r = client.post("/api/data-export/run", json=spec, headers=H)
    body = r.text if r.status_code == 200 else ""
    lines = list(csv.reader(io.StringIO(body)))
    check("the downloaded CSV is pivoted: one row per timestamp, a column per tag",
          r.status_code == 200 and lines and lines[0] == ["ts_utc", "Flow", "Temp", "WO"] and len(lines) == 7,
          "%s %r" % (r.status_code, lines[:2]))
    check("  and a text tag fills its cell", len(lines) > 1 and lines[1][3].startswith("WO-"), repr(lines[1:2]))
    r2 = client.post("/api/data-export/run", json=dict(spec, pivot=False), headers=H)
    l2 = list(csv.reader(io.StringIO(r2.text)))
    check("  without Pivot the file is still long", r2.status_code == 200 and l2 and l2[0][:3] == ["ts_utc", "tag_name", "value"] and len(l2) == 19,
          repr(l2[:1]) + " %d rows" % (len(l2) - 1))

    # ---- 4. dashboard table: text and masks ----------------------------------
    node = subprocess.run(["node", "--input-type=module", "-e", '''
import { parseValueMask } from "file:///%s";
const m = parseValueMask("1=PASS,0=FAIL,*=?");
console.log(JSON.stringify([m(1, null), m(0, null), m(2, null), m(null, "1"), m(76668, "76668")]));
''' % os.path.join(SRC, "components", "Dashboard", "DashboardWidgets.jsx").replace("\\", "/")], capture_output=True, text=True, timeout=60, cwd=SRC)
    try:
        got = json.loads(node.stdout.strip().splitlines()[-1])
    except Exception:
        got = None
        # the widget module pulls React; fall back to a bundled check
    if got is None:
        esb = '''
const path = await import("node:path"); const os = await import("node:os");
const SRC = %s;
const esbuild = await import("file:///" + path.join(SRC, "..", "node_modules", "esbuild", "lib", "main.js").split(String.fromCharCode(92)).join("/"));
const out = path.join(os.tmpdir(), "tn_mask_" + process.pid + ".mjs");
await esbuild.build({ stdin: { contents: "export { parseValueMask } from " + JSON.stringify(path.join(SRC, "components", "Dashboard", "DashboardWidgets.jsx").split(String.fromCharCode(92)).join("/")) + ";", resolveDir: SRC, loader: "js" },
  bundle: true, format: "esm", platform: "browser", outfile: out, absWorkingDir: path.join(SRC, ".."), nodePaths: [path.join(SRC, "..", "node_modules")],
  loader: { ".js": "jsx", ".jsx": "jsx", ".css": "empty", ".png": "empty", ".svg": "empty" }, define: { "import.meta.env": "{}", "process.env.NODE_ENV": '"production"' }, logLevel: "silent" });
const { parseValueMask } = await import("file:///" + out.split(String.fromCharCode(92)).join("/"));
const m = parseValueMask("1=PASS,0=FAIL,*=?");
console.log(JSON.stringify([m(1, null), m(0, null), m(2, null), m(null, "1"), m(76668, "76668")]));
process.exit(0);
''' % json.dumps(SRC)
        node = subprocess.run(["node", "--input-type=module", "-e", esb], capture_output=True, text=True, timeout=180, cwd=SRC)
        try:
            got = json.loads(node.stdout.strip().splitlines()[-1])
        except Exception:
            got = None
    check("a table mask turns 1/0 into PASS/FAIL and * catches the rest", got == ["PASS", "FAIL", "?", "PASS", "?"],
          repr(got) + " " + (node.stderr or "")[-200:])
    wsrc2 = open(os.path.join(SRC, "components", "Dashboard", "DashboardWidgets.jsx"), encoding="utf-8").read()
    check("  a table cell shows a text value (part number) and honours the column mask",
          "cellValues[`${col.id}__text`]" in wsrc2 and "parseValueMask(col.mask)" in wsrc2)
    dsrc = open(os.path.join(SRC, "components", "Dashboard", "DashboardDesigner.jsx"), encoding="utf-8").read()
    check("  the column editor offers the mask", 'placeholder="mask: 1=PASS,0=FAIL"' in dsrc)

    # ---- 5. a text part number as the cycle report id -------------------------
    from app import state as _st
    with app_store._live_cache_lock:
        app_store._local_live_latest_cache[("tenant-cust-parts", "gw1", "WO")] = {"value": None, "value_text": "WO-4471", "ts": "2026-11-16 00:00:00"}
        app_store._local_live_latest_cache[("tenant-cust-parts", "gw1", "Serial")] = {"value": 76668.0, "value_text": "76668", "ts": "2026-11-16 00:00:00"}
        app_store._local_live_latest_cache[("tenant-cust-parts", "gw1", "Num")] = {"value": 12.5, "ts": "2026-11-16 00:00:00"}
    check("a text id_tag gives the text", _st._scheduler_live_text_lookup("gw1", "WO") == "WO-4471", repr(_st._scheduler_live_text_lookup("gw1", "WO")))
    check("  a whole-number id prints without .0", _st._scheduler_live_text_lookup("gw1", "Serial") == "76668", repr(_st._scheduler_live_text_lookup("gw1", "Serial")))
    check("  a decimal keeps its decimals", _st._scheduler_live_text_lookup("gw1", "Num") == "12.5", repr(_st._scheduler_live_text_lookup("gw1", "Num")))
    check("  the scheduler is wired to it", getattr(_st.report_scheduler, "live_text_lookup", None) is _st._scheduler_live_text_lookup)

    # ---- 6. the menus -----------------------------------------------------------
    rsrc = open(os.path.join(SRC, "components", "Reports", "ReportTemplateDesigner.jsx"), encoding="utf-8").read()
    check("duration, ramp time and rate are offered for KPIs and test-summary columns",
          rsrc.count('{ value: "duration_s"') == 2 and rsrc.count('{ value: "rate_max"') == 2 and "ramp_from" in rsrc)

    # ---- 7. Excel: wide, and text -------------------------------------------------
    from app.services import file_sinks as fsk
    from openpyxl import load_workbook
    xrows = [{"ts_utc": "2026-11-16 10:00:00.000", "gateway_id": "gw1", "gateway_name": "PLC", "tag_name": "Flow", "value": 1.5, "quality": 192, "quality_label": "GOOD", "source": "t"},
             {"ts_utc": "2026-11-16 10:00:00.000", "gateway_id": "gw1", "gateway_name": "PLC", "tag_name": "WO", "value": None, "value_text": "WO-4471", "quality": 192, "quality_label": "GOOD", "source": "t"},
             {"ts_utc": "2026-11-16 10:00:01.000", "gateway_id": "gw1", "gateway_name": "PLC", "tag_name": "Flow", "value": 2.5, "quality": 192, "quality_label": "GOOD", "source": "t"}]
    wide_path = os.path.join(_TMP, "wide.xlsx")
    fsk.write_xlsx(wide_path, xrows, "none", lambda s: s, sink={"layout": "wide", "tag_filters": ["Flow", "WO"]})
    ws = load_workbook(wide_path).active
    grid = [[c.value for c in r] for r in ws.iter_rows()]
    check("a wide Excel sheet is one row per timestamp with a column per tag",
          grid[0][:3] == ["ts_local", "ts_utc", "gateway_id"] and grid[0][3:] == ["Flow", "WO"] and len(grid) == 3, repr(grid))
    check("  and a text tag fills its cell", len(grid) > 1 and grid[1][4] == "WO-4471", repr(grid[1:2]))
    long_path = os.path.join(_TMP, "long.xlsx")
    fsk.write_xlsx(long_path, xrows, "none", lambda s: s, sink={"layout": "long"})
    wl = load_workbook(long_path).active
    g2 = [[c.value for c in r] for r in wl.iter_rows()]
    check("  a long sheet keeps its columns and shows the string in Value", g2[0] == fsk.CANONICAL_HEADER and g2[2][5] == "WO-4471", repr(g2[:3]))

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
