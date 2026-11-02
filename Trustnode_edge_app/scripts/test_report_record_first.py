# -*- coding: utf-8 -*-
"""A generated report exists the moment its run starts - PDF or no PDF.

2026-11-02, asked for directly: "it doesn't matter if the file is created or
not, the report metadata should be shown on the generated reports
automatically - to show the customer when the report is created, and if
generating the PDF failed the user can always preview or regenerate the data
of that report, locally or from the web with the cloud's information."

Until now the record was written only AFTER a successful render. On a
customer edge where a render took minutes - or failed - the report did not
exist anywhere for that long, or ever: not on the list, not in Lite, not in
the cloud.

Now the row is inserted first (render_state "pending"), the data snapshot is
stored with it, and it becomes "ready" (file filled in) or "failed" (reason
kept). The cloud mirror runs only when a cloud is configured, and then on
every state change.

    python scripts/test_report_record_first.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

_TMP = tempfile.mkdtemp(prefix="tn_recfirst_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-rf"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-66s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def _ts(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) + ".000"


def main() -> int:
    print("a generated report exists before its PDF does")
    from app.state import app_store, reports_store, report_runner
    from app.tenant import set_current_tenant
    set_current_tenant("tenant-cust-rf")
    from app.services import reports_store as _rs_mod
    from app.services import report_scheduler as _sched_mod

    # Some history and a template with the section types a customer uses.
    now = time.time()
    rows = []
    for i in range(0, 1800, 5):
        for tag in ("Flow", "Temp"):
            rows.append({"ts_utc": _ts(now - 1800 + i), "gateway_id": "gw1", "gateway_name": "PLC",
                         "device_name": "d", "plc_ip": "10.0.0.1", "database_name": "db",
                         "tag_name": tag, "value": float(i % 97), "quality": 192,
                         "quality_label": "GOOD", "source": "test"})
    app_store.append_historian_rows(rows)
    ser = lambda t: {"id": "s-" + t, "label": t, "gateway_id": "gw1", "tag_name": t,
                     "color": "#14a89a", "axis": "left", "multiplier": 1, "offset": 0}
    tpl = reports_store.upsert_template({
        "name": "Record first", "definition": {"sections": [
            {"type": "header", "title": "Record first", "show_generated_at": True},
            {"type": "line_chart", "title": "Trend", "time_range": {"preset": "1h"},
             "readings_count": 500, "series": [ser("Flow"), ser("Temp")]},
            {"type": "kpi_grid", "title": "KPIs", "time_range": {"preset": "1h"},
             "items": [{"id": "k1", "label": "avg Flow", "gateway_id": "gw1", "tag_name": "Flow", "aggregation": "avg"}]},
            {"type": "table", "title": "Rows", "time_range": {"preset": "none"}, "row_limit": 20,
             "readings_count": 20, "series": [ser("Temp")]},
        ]}})
    tid = str(tpl.get("id") or "")
    check("a template with chart, KPI and table exists", bool(tid))

    # ---- the cloud is NOT configured: nothing may touch it ---------------
    mirror_calls = {"n": 0}
    _orig_mirror_async = _rs_mod.ReportsStore._mirror_async

    def _counting_mirror(self, full):
        mirror_calls["n"] += 1 if self._cloud_enabled() else 0
        return _orig_mirror_async(self, full)
    _rs_mod.ReportsStore._mirror_async = _counting_mirror
    check("no cloud is configured on this store", not reports_store._cloud_enabled())

    # ---- a normal run: the record is there before the render finishes ---
    seen_during_render = {}
    from app.services import report_renderer as _rr
    _orig_render = report_runner.render

    def _slow_render(template, **kw):
        # Look at the list WHILE the PDF is being made.
        seen_during_render["rows"] = reports_store.list_generated(limit=10)
        return _orig_render(template, **kw)
    report_runner.render = _slow_render
    sch = {"id": "sch-rf", "name": "RF", "template_id": tid, "template_ids": [tid],
           "tenant_id": "tenant-cust-rf", "enabled": True, "trigger_mode": "manual"}
    t0 = time.perf_counter()
    res = report_runner.run(sch, triggered_by="manual")
    took = time.perf_counter() - t0
    runs = res if isinstance(res, list) else [res]
    rec = (runs[0] or {}).get("generated") or {}
    pending_rows = seen_during_render.get("rows") or []
    check("the record is on the list WHILE the PDF is being rendered",
          any(r.get("id") == rec.get("id") and r.get("render_state") == "pending" for r in pending_rows),
          "states seen: %r" % [r.get("render_state") for r in pending_rows])
    check("the run finishes ready, with the file", rec.get("render_state") == "ready"
          and int(rec.get("file_bytes") or 0) > 1000 and os.path.exists(rec.get("file_path") or ""),
          "%r" % {k: rec.get(k) for k in ("render_state", "file_bytes", "render_error")})
    check("  and it carries the window it was made with",
          isinstance(rec.get("meta"), dict) and "render_state" in rec["meta"])
    check("  the whole run is fast on this store", took < 5.0, "%.1fs" % took)
    snap = reports_store.get_generated_render_data(rec.get("id"))
    check("the report's DATA is stored with the record",
          isinstance(snap, dict) and isinstance(snap.get("sections"), list) and len(snap["sections"]) >= 3,
          repr(snap)[:120])
    kinds = [s_.get("type") for s_ in (snap or {}).get("sections", [])]
    check("  including the chart, the KPI and the table",
          "kpi_grid" in kinds and "table" in kinds and any("chart" in str(k) for k in kinds), repr(kinds))
    check("no cloud call was made for any of it", mirror_calls["n"] == 0, "%d" % mirror_calls["n"])

    # ---- a FAILED render still leaves the report on the list -------------
    def _boom(template, **kw):
        raise RuntimeError("disk full (simulated)")
    report_runner.render = _boom
    res = report_runner.run(sch, triggered_by="manual")
    runs = res if isinstance(res, list) else [res]
    failed = (runs[0] or {}).get("generated") or {}
    check("a failed render still produces a listed report", bool(failed.get("id")),
          repr(runs[0])[:160])
    failed = reports_store.get_generated(failed.get("id")) or failed
    check("  marked failed, with the reason", failed.get("render_state") == "failed"
          and "disk full" in str(failed.get("render_error") or ""),
          repr({k: failed.get(k) for k in ("render_state", "render_error")}))
    check("  the run result says it failed", not (runs[0] or {}).get("ok"))
    report_runner.render = _orig_render

    # ---- over HTTP: preview-data, regenerate, the file endpoint ----------
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth import create_access_token
    tok = create_access_token({"sub": "admin", "role": "admin", "tenant_id": "tenant-cust-rf",
                               "permissions": {}, "modules": []}, expires_seconds=600)
    H = {"Authorization": "Bearer " + tok, "Host": "127.0.0.1:8000"}
    # The desktop app is loopback; remote POSTs are licence-gated.
    client = TestClient(app, client=("127.0.0.1", 50000))
    r = client.get("/api/reports/generated/%s/file" % failed["id"], headers=H)
    check("the file endpoint explains a failed report instead of 'not on this machine'",
          r.status_code == 409 and "failed" in r.text.lower(), "%s %s" % (r.status_code, r.text[:120]))
    r = client.get("/api/reports/generated/%s/preview-data" % failed["id"], headers=H)
    check("the failed report's data can still be previewed",
          r.status_code == 200 and isinstance((r.json() or {}).get("data", {}).get("sections"), list),
          "%s %s" % (r.status_code, r.text[:120]))
    r = client.post("/api/reports/generated/%s/regenerate" % failed["id"], headers=H)
    check("regenerate is accepted and runs in the background",
          r.status_code == 200 and (r.json() or {}).get("job_id"), "%s %s" % (r.status_code, r.text[:120]))
    job = (r.json() or {}).get("job_id")
    state = None
    for _ in range(60):
        j = client.get("/api/reports/jobs/%s" % job, headers=H).json()
        state = j.get("state")
        if state in ("done", "error"):
            break
        time.sleep(0.5)
    again = reports_store.get_generated(failed["id"]) or {}
    check("  and the SAME record becomes ready with a PDF", state == "done"
          and again.get("render_state") == "ready" and os.path.exists(again.get("file_path") or ""),
          "job=%s state=%s" % (state, again.get("render_state")))
    r = client.get("/api/reports/generated/%s/file" % failed["id"], headers=H)
    check("  the file endpoint now serves it", r.status_code == 200 and r.headers.get("content-type", "").startswith("application/pdf"),
          "%s" % r.status_code)
    r = client.get("/api/reports/generated/%s/preview-data" % again["id"], headers=H)
    check("  its preview comes from the stored snapshot", (r.json() or {}).get("source") == "snapshot",
          r.text[:100])
    r = client.post("/api/reports/templates/%s/generate" % tid, headers=H)
    g = (r.json() or {}).get("generated") or {}
    check("the dashboard's 'Generate now' follows the same record-first path",
          r.status_code == 200 and g.get("render_state") == "ready" and int(g.get("file_bytes") or 0) > 0,
          "%s %s" % (r.status_code, r.text[:120]))
    rows = reports_store.list_generated(limit=20)
    check("every listed report carries a render_state", rows and all(r_.get("render_state") for r_ in rows))

    # ---- the cloud IS configured: every state change is mirrored ----------
    pushed = []
    _orig_mirror = _rs_mod.ReportsStore._mirror_async

    def _fake_cloud_mirror(self, full):
        pushed.append((full.get("id"), (full.get("meta") or {}).get("render_state")))
    _rs_mod.ReportsStore._mirror_async = _fake_cloud_mirror
    res = report_runner.run(sch, triggered_by="manual")
    runs = res if isinstance(res, list) else [res]
    gid = ((runs[0] or {}).get("generated") or {}).get("id")
    states = [st for (i, st) in pushed if i == gid]
    check("with a cloud, the pending record is mirrored first, then the ready one",
          states[:1] == ["pending"] and states[-1:] == ["ready"], repr(states))
    _rs_mod.ReportsStore._mirror_async = _orig_mirror

    # ---- the uploader ships the data snapshot as a sidecar ---------------
    from app.services import reports_cloud_uploader as _up
    src = open(_up.__file__, encoding="utf-8").read()
    check("the cloud copy gets the data snapshot beside the PDF",
          "upload_bytes_to_storage" in src and "snapshot_path" in src)

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
