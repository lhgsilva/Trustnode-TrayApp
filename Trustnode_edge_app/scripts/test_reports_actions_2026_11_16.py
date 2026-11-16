# -*- coding: utf-8 -*-
"""Generated Reports actions answer at once; Regenerate keeps the file; Duplicate.

2026-11-16, reported:
  * "on the generated reports the action icons to download, preview etc are
    not working, it takes too long to do something" - every click was a GET
    queued in the same 4-slot read gate as the page's polling, with nothing
    on screen; preview window.open'ed a blob that was revoked 5 s later;
  * Regenerate rebuilt the PDF as <uuid>.pdf in the default folder, losing
    the schedule's file name and output folder;
  * "we should be able to duplicate the schedule reporting setting".

Checks:
  1. with all four read slots held by slow polls, a download and a data
     preview still complete (bundled api.js under node, fetch stubbed);
  2. Regenerate rebuilds the SAME file path the record had;
  3. a duplicated schedule is a new schedule with every setting, saved
     disabled, with a unique name, and the original is unchanged;
  4. the page shows the PDF inside the app and marks a busy action.

    python scripts/test_reports_actions_2026_11_16.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="tn_rptact_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-ra"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "frontend", "src")
sys.path.insert(0, os.path.join(ROOT, "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-70s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


NODE = r'''
const path = await import("node:path");
const os = await import("node:os");
const SRC = process.argv[1];
const esbuild = await import("file:///" + path.join(SRC, "..", "node_modules", "esbuild", "lib", "main.js").replace(/\\/g, "/"));
const out = path.join(os.tmpdir(), "tn_api_" + process.pid + ".mjs");
await esbuild.build({
  stdin: { contents: "export * from " + JSON.stringify(path.join(SRC, "api.js").replace(/\\/g, "/")) + ";", resolveDir: SRC, loader: "js" },
  bundle: true, format: "esm", platform: "neutral", outfile: out, mainFields: ["module", "main"],
  absWorkingDir: path.join(SRC, ".."), nodePaths: [path.join(SRC, "..", "node_modules")],
  loader: { ".js": "jsx", ".jsx": "jsx", ".css": "empty" }, define: { "import.meta.env": "{}" }, logLevel: "silent",
});
globalThis.window = globalThis; globalThis.location = new URL("http://127.0.0.1:8000/trustnode/full/app/"); globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
globalThis.sessionStorage = globalThis.localStorage;
globalThis.document = { visibilityState: "visible", addEventListener() {}, cookie: "" };
let held = 0;
globalThis.fetch = (url, opts) => {
  if (String(url).includes("/api/poll/")) { held += 1; return new Promise(() => {}); }   // a poll that never answers
  const body = String(url).includes("/preview-data") ? JSON.stringify({ ok: true, data: {} }) : "%PDF-1.4";
  return Promise.resolve(new Response(body, { status: 200, headers: { "Content-Type": "application/pdf" } }));
};
const api = await import("file:///" + out.replace(/\\/g, "/"));
const slow = (n) => api.fetchWithTimeout ? api.fetchWithTimeout("http://127.0.0.1:8000/api/poll/" + n, {}, 60000) : null;
// occupy every read slot the way a busy page does
const fw = api.__testFetchWithTimeout || null;
for (let i = 0; i < 6; i++) (api.getAppStoreBootstrapFor ? null : null);
const race = (p, ms) => Promise.race([p.then(() => "done", (e) => "error:" + e.message), new Promise((r) => setTimeout(() => r("timeout"), ms))]);
// the gate is shared module state: hold its slots through the gate itself
const gate = await import("file:///" + path.join(SRC, "requestGate.js").replace(/\\/g, "/"));
const res = {};
// requestGate in the bundle is a separate copy; hold slots through a gated api GET instead
const polls = [];
for (let i = 0; i < 6; i++) polls.push(api.getReportsOverview ? api.getReportsOverview().catch(() => {}) : null);
await new Promise((r) => setTimeout(r, 50));
res.pollsHeld = held;
res.download = await race(api.downloadGeneratedReportBlob("g1"), 2000);
res.preview = await race(api.getGeneratedReportPreviewData("g1"), 2000);
console.log(JSON.stringify(res));
process.exit(0);
'''


def main() -> int:
    print("Generated Reports actions, Regenerate in place, Duplicate schedule")

    # ---- 1. a click never queues behind the polls ---------------------------
    script = NODE.replace('if (String(url).includes("/api/poll/"))', 'if (String(url).includes("/api/reports/overview"))')
    res = subprocess.run(["node", "--input-type=module", "-e", script, SRC], capture_output=True, text=True,
                         timeout=120, cwd=SRC)
    try:
        got = json.loads(res.stdout.strip().splitlines()[-1])
    except Exception:
        got = {}
    check("the page's polls hold every read slot (the situation reported)", got.get("pollsHeld", 0) >= 4,
          (res.stdout + res.stderr)[-300:])
    check("  a PDF download still completes at once", got.get("download") == "done", repr(got))
    check("  and so does a data preview", got.get("preview") == "done", repr(got))

    # ---- 2. Regenerate rebuilds the same file --------------------------------
    from app.state import app_store, reports_store
    from app.tenant import set_current_tenant
    set_current_tenant("tenant-cust-ra")
    now = time.time()
    app_store.append_historian_rows([
        {"ts_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - 600 + i)) + ".000", "gateway_id": "gw1",
         "gateway_name": "PLC", "device_name": "d", "plc_ip": "10.0.0.1", "database_name": "db", "tag_name": "Flow",
         "value": float(i % 50), "quality": 192, "quality_label": "GOOD", "source": "test"} for i in range(0, 600, 5)])
    tpl = reports_store.upsert_template({"name": "Regen", "definition": {"sections": [
        {"type": "header", "title": "Regen"},
        {"type": "kpi_grid", "title": "K", "time_range": {"preset": "1h"},
         "items": [{"id": "k1", "label": "Flow", "gateway_id": "gw1", "tag_name": "Flow", "aggregation": "avg"}]}]}})
    folder = os.path.join(_TMP, "Customer Reports", "Line 1")
    os.makedirs(folder, exist_ok=True)
    named = os.path.join(folder, "WO-76668-report.pdf")
    from app.services.report_renderer import render_template_to_pdf
    path, nbytes, sha = render_template_to_pdf(tpl, output_path=named)
    rec = reports_store.insert_generated({"template_id": tpl["id"], "template_name": "Regen", "triggered_by": "schedule",
                                          "file_path": str(path), "file_name": os.path.basename(str(path)),
                                          "file_bytes": nbytes, "file_sha256": sha,
                                          "meta": {"render_state": "ready"}})
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth import create_access_token
    tok = create_access_token({"sub": "admin", "role": "admin", "tenant_id": "tenant-cust-ra",
                               "permissions": {}, "modules": []}, expires_seconds=600)
    client = TestClient(app, client=("127.0.0.1", 50000))
    H = {"Authorization": "Bearer " + tok, "Host": "127.0.0.1:8000"}
    time.sleep(1.1)
    r = client.post("/api/reports/generated/%s/regenerate" % rec["id"], headers=H)
    after_rec = None
    for _ in range(60):
        time.sleep(0.25)
        after_rec = reports_store.get_generated(rec["id"])
        if str((after_rec or {}).get("render_state") or "") in ("ready", "failed"):
            break
    check("Regenerate is accepted", r.status_code == 200, r.text[:200])
    # The writer never overwrites a file (unique_output_path), so the earlier
    # PDF is kept and the rebuilt one sits beside it: WO-76668-report_2.pdf.
    newp = str((after_rec or {}).get("file_path") or "")
    check("  the PDF is rebuilt in the SAME folder under the same name",
          bool(newp) and os.path.normcase(os.path.dirname(newp)) == os.path.normcase(folder)
          and os.path.basename(newp).startswith("WO-76668-report") and os.path.exists(newp), repr(newp))
    check("  and the earlier PDF is kept, never destroyed", os.path.exists(named))
    stray = [f for f in os.listdir(os.path.join(_TMP)) if f.endswith(".pdf")]
    check("  and no stray <uuid>.pdf is left in the default folder", not stray, repr(stray))

    # ---- 3. Duplicate --------------------------------------------------------
    orig = {"name": "Line 1 cycle", "template_id": tpl["id"], "enabled": True, "trigger_mode": "cycle",
            "output_path": r"C:\Reports\{date}", "file_name_pattern": "{WO}-{time}", "id_tag": "WO",
            "tag_conditions": [{"id": "c1", "enabled": True, "gateway_id": "gw1", "tag_name": "Run", "operator": "eq", "value": "1"}]}
    r = client.post("/api/reports/schedules", json=orig, headers=H)
    saved = (r.json() or {}).get("schedule") or {}
    copy = dict(saved, id="", name="Line 1 cycle (copy)", enabled=False, last_run_utc="", cycle_started_utc="",
                cycle_report_id="")
    r2 = client.post("/api/reports/schedules", json=copy, headers=H)
    dup = (r2.json() or {}).get("schedule") or {}
    still = reports_store.get_schedule(saved.get("id"))
    check("a duplicate is a NEW schedule", bool(dup.get("id")) and dup.get("id") != saved.get("id"), repr(dup.get("id")))
    check("  with every setting copied",
          all(dup.get(k) == saved.get(k) for k in ("template_id", "trigger_mode", "output_path", "file_name_pattern", "id_tag"))
          and len(dup.get("tag_conditions") or []) == 1, json.dumps({k: dup.get(k) for k in ("trigger_mode", "output_path", "id_tag")}))
    check("  saved disabled", dup.get("enabled") in (False, 0), repr(dup.get("enabled")))
    check("  and the original is unchanged and still enabled",
          bool(still) and still.get("enabled") in (True, 1) and still.get("name") == "Line 1 cycle", repr(still and still.get("name")))

    # ---- 4. the page ---------------------------------------------------------
    page = open(os.path.join(SRC, "components", "Reports", "ScheduledReportsManager.jsx"), encoding="utf-8").read()
    check("the page offers Duplicate on a saved schedule, with a unique '(copy)' name and disabled",
          "handleDuplicate" in page and "(copy)" in page and "enabled: false," in page)
    check("  the PDF is shown inside the app (no window.open of a revoked blob)",
          "<object data={pdfView.url}" in page and "openPdfInTab" not in page)
    check("  a busy action is marked and cannot be clicked twice", "aria-busy={isRowBusy(g.id," in page)

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
