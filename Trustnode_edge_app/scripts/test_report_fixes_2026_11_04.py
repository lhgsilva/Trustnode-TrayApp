# -*- coding: utf-8 -*-
"""Five things reported on 2026-11-04, each reproduced before it was fixed.

  1. A header pill set to "fit the text" wrapped and cut the text. Measured:
     at 28 pt "Daily Production Summary" fitted the page yet came out on two
     lines (zero slack: the inner width equalled the text width, a rounding
     hair wrapped it), and a long title wider than the page wrapped instead
     of shrinking.
  2. Destination rules (another folder or name when a tag says so) - run
     through the real scheduler against a temp folder.
  3. The builder's preview timed out at 60 s. The render is fast here
     (0.4 s), so the request now returns the PENDING record at once and
     renders in the background; the page polls. A slow render can no
     longer hit the request timeout.
  4. Dashboard widgets showed a few points whatever the window: a time
     window was applied as a filter but the request still asked for the
     newest N rows. Now a time window auto-buckets to the reading count.
  5. Trigger windows never resolved on the server: the endpoint read a
     `ts_utc` key the rows do not carry (they carry `ts`), so it walked
     blank timestamps and always answered "not found".

    python scripts/test_report_fixes_2026_11_04.py
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

_TMP = tempfile.mkdtemp(prefix="tn_fix_")
atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-fix"

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "backend"))
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-70s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def _ts(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) + ".000"


def _text_runs(pdf_path):
    from pypdf import PdfReader
    runs = []

    def visitor(text, cm, tm, fd, fs):
        t = text.strip()
        if t:
            runs.append((round(cm[5] + tm[5], 1), t))
    PdfReader(str(pdf_path)).pages[0].extract_text(visitor_text=visitor)
    return [t for (_y, t) in runs if t != "Page 1" and not t.startswith("Generated")]


def main() -> int:
    print("the five things reported on 2026-11-04")
    from app.state import app_store, reports_store, report_runner, plc_manager
    from app.tenant import set_current_tenant
    set_current_tenant("tenant-cust-fix")
    from app.services.report_renderer import render_template_to_pdf
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth import create_access_token
    tok = create_access_token({"sub": "admin", "role": "admin", "tenant_id": "tenant-cust-fix",
                               "permissions": {}, "modules": []}, expires_seconds=600)
    H = {"Authorization": "Bearer " + tok, "Host": "127.0.0.1:8000"}
    client = TestClient(app, client=("127.0.0.1", 50000))

    # ---------------- 1. the pill fits its text ---------------------------
    title = "Daily Production Summary"
    tpl = {"id": "t", "name": "Pill", "definition": {"sections": [
        {"type": "header", "title": title, "as_pill": True, "pill_fit": "content", "pill_align": "right",
         "title_font_size": 28},
        {"type": "pills", "title": "", "as_header": True, "pill_fit": "content", "pill_align": "right",
         "pills": [{"label": title, "text": ""}], "font_size": 28},
    ]}}
    path, _n, _sha = render_template_to_pdf(tpl, output_path=os.path.join(_TMP, "pill1.pdf"))
    runs = _text_runs(path)
    check("a 28 pt header pill that fits the page prints its text on ONE line (band and row)",
          runs.count(title) == 2 and len(runs) == 2, repr(runs))
    long_title = "Line 3 Shift Report - Night Shift - Packaging Area - Week 45"
    tpl["definition"]["sections"][0]["title"] = long_title
    tpl["definition"]["sections"][1]["pills"][0]["label"] = long_title
    path, _n, _sha = render_template_to_pdf(tpl, output_path=os.path.join(_TMP, "pill2.pdf"))
    runs = _text_runs(path)
    check("a title wider than the page shrinks to one line instead of wrapping",
          runs.count(long_title) == 2 and len(runs) == 2, repr(runs))

    # ---------------- 2. destination rules through the real scheduler ----
    now = time.time()
    app_store.append_historian_rows([
        {"ts_utc": _ts(now - 30), "gateway_id": "gw1", "gateway_name": "PLC", "device_name": "d",
         "tag_name": "Result", "value": None, "value_text": "PASS", "quality": 192, "quality_label": "GOOD", "source": "test"},
        {"ts_utc": _ts(now - 30), "gateway_id": "gw1", "gateway_name": "PLC", "device_name": "d",
         "tag_name": "Stage", "value": 2.0, "quality": 192, "quality_label": "GOOD", "source": "test"},
        {"ts_utc": _ts(now - 20), "gateway_id": "gw1", "gateway_name": "PLC", "device_name": "d",
         "tag_name": "Flow", "value": 1.0, "quality": 192, "quality_label": "GOOD", "source": "test"},
    ])
    saved_tpl = reports_store.upsert_template({"id": "", "name": "Rules", "description": "",
                                               "definition": {"sections": [{"type": "header", "title": "R"}]}})
    passed_dir = os.path.join(_TMP, "out", "Passed")
    stage_dir = os.path.join(_TMP, "out", "Stage2")
    sch = reports_store.upsert_schedule({
        "id": "", "name": "S", "template_id": saved_tpl["id"], "enabled": True, "trigger_mode": "time",
        "recurrence": "daily", "hour": 8, "minute": 0, "output_path": os.path.join(_TMP, "out", "Default"),
        "file_name_pattern": "default_{date}",
        "destination_rules": [
            {"id": "r1", "enabled": True, "logic": "all", "output_path": passed_dir,
             "file_name_pattern": "PASS_{Result}_{date}",
             "conditions": [{"gateway_id": "", "tag_name": "Result", "operator": "==", "value": "PASS"}]},
            {"id": "r2", "enabled": True, "logic": "all", "output_path": stage_dir, "file_name_pattern": "",
             "conditions": [{"gateway_id": "", "tag_name": "Stage", "operator": "==", "value": "2"}]},
        ],
    })
    back = reports_store.get_schedule(sch["id"])
    check("the rules survive the store round trip", len(back.get("destination_rules") or []) == 2,
          repr(back.get("destination_rules"))[:160])
    sch["tenant_id"] = "tenant-cust-fix"
    res = report_runner.run(sch, triggered_by="manual")
    gen = (res or {}).get("generated") or {}
    fp = str(gen.get("file_path") or "")
    check("a time schedule with a matching text rule writes into the rule's folder and name",
          fp.replace("\\", "/").startswith(passed_dir.replace("\\", "/")) and os.path.basename(fp).startswith("PASS_PASS_"),
          "file_path=%r ok=%s err=%s" % (fp, res.get("ok"), gen.get("render_error")))
    check("  and the file exists there", bool(fp) and os.path.exists(fp), fp)
    # make the first rule miss: the second (numeric) rule must then win
    app_store.append_historian_rows([{"ts_utc": _ts(now - 5), "gateway_id": "gw1", "gateway_name": "PLC",
                                      "device_name": "d", "tag_name": "Result", "value": None, "value_text": "FAIL",
                                      "quality": 192, "quality_label": "GOOD", "source": "test"}])
    res2 = report_runner.run(sch, triggered_by="manual")
    fp2 = str(((res2 or {}).get("generated") or {}).get("file_path") or "")
    check("when the text rule no longer matches, the numeric rule (2 == '2') places the file",
          fp2.replace("\\", "/").startswith(stage_dir.replace("\\", "/")), "file_path=%r" % fp2)

    # ---------------- 3. the preview never blocks the request ---------------
    import app.routers.reports as rmod
    import app.services.report_renderer as rr
    orig = rmod.render_template_to_pdf if hasattr(rmod, "render_template_to_pdf") else None
    real = rr.render_template_to_pdf

    def slow(*a, **k):
        time.sleep(4.0)
        return real(*a, **k)
    rr.render_template_to_pdf = slow
    try:
        t0 = time.perf_counter()
        r = client.post("/api/reports/render", headers=H, json={"template": saved_tpl})
        dt = time.perf_counter() - t0
        body = r.json() if r.status_code == 200 else {}
        g = body.get("generated") or {}
        check("POST /render answers at once with a PENDING record while the PDF renders behind",
              r.status_code == 200 and dt < 2.0 and g.get("render_state") == "pending" and bool(g.get("id")),
              "status=%s %.1fs state=%s" % (r.status_code, dt, g.get("render_state")))
        gid = str(g.get("id") or "")
        deadline = time.time() + 30
        state = ""
        while time.time() < deadline:
            rec = client.get("/api/reports/generated/%s" % gid, headers=H).json().get("generated") or {}
            state = str(rec.get("render_state") or "")
            if state in ("ready", "failed"):
                break
            time.sleep(0.5)
        check("  and the record becomes ready on its own", state == "ready", "state=%s" % state)
        rf = client.get("/api/reports/generated/%s/file" % gid, headers=H)
        check("  and the PDF can then be fetched", rf.status_code == 200 and rf.headers.get("content-type", "").startswith("application/pdf"),
              "status=%s" % rf.status_code)
    finally:
        rr.render_template_to_pdf = real
    designer = open(os.path.join(ROOT, "frontend", "src", "components", "Reports", "ReportTemplateDesigner.jsx"), encoding="utf-8").read()
    api_src = open(os.path.join(ROOT, "frontend", "src", "api.js"), encoding="utf-8").read()
    check("the designer polls the record instead of waiting on the request",
          designer.count("waitForGeneratedReady(") >= 2 and "export async function getGeneratedReport(" in api_src)

    # ---------------- 4. a time window auto-buckets to the reading count ---
    dw = open(os.path.join(ROOT, "frontend", "src", "components", "Dashboard", "DashboardWidgets.jsx"), encoding="utf-8").read()
    check("the widget derives a bucket from the window and the reading count",
          "export function autoBucketSecondsFor(" in dw and dw.count("bucketSeconds: effectiveBucketSeconds") >= 2)
    m = re.search(r"const AUTO_BUCKET_STEPS = .*?\n(?:export function autoBucketSecondsFor\(.*?\n\}\n)", dw, re.S)
    out = subprocess.run(["node", "--input-type=module", "-e", (m.group(0) if m else "") + """
console.log(JSON.stringify([
  autoBucketSecondsFor(8*3600, 120, 1),   // 8 h into 120 points -> 240 s -> 300 s step
  autoBucketSecondsFor(60, 120, 1),       // 60 s of 1 Hz into 120 points -> raw (0)
  autoBucketSecondsFor(3600, 5000, 0.5),  // 7200 samples into 5000 -> 2 s
  autoBucketSecondsFor(0, 120, 1),        // no window -> raw
]));"""], capture_output=True, text=True, timeout=30)
    check("  8 h/120 pts -> 300 s, 60 s/120 pts -> raw, 1 h at 0.5 s/5000 pts -> 1 s, no window -> raw",
          out.stdout.strip() == "[300,0,1,0]", out.stdout.strip() + out.stderr.strip()[:120])

    # ---------------- 5. the trigger window resolves on the server --------
    base = now - 300
    rows = []
    for s in range(0, 120):
        rows.append({"ts_utc": _ts(base + s), "gateway_id": "gw1", "gateway_name": "PLC", "device_name": "d",
                     "tag_name": "Start", "value": 1.0 if 40 <= s < 90 else 0.0, "quality": 192,
                     "quality_label": "GOOD", "source": "test"})
    app_store.append_historian_rows(rows)
    r = client.get("/api/app-store/historian/trigger-window", headers=H,
                   params={"gateway": "gw1", "tag": "Start", "operator": "==", "value": "1",
                           "stop_tag": "Start", "stop_operator": "==", "stop_value": "0"})
    body = r.json() if r.status_code == 200 else {}
    check("the trigger window is found at the rising edge",
          body.get("found") is True and str(body.get("from_utc") or "")[:19] == _ts(base + 40)[:19],
          repr(body))
    check("  and closes at the stop condition",
          str(body.get("to_utc") or "")[:19] == _ts(base + 90)[:19], repr(body))

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
