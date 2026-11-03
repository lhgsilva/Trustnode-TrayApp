# -*- coding: utf-8 -*-
"""The Reports module never fails to load and never makes the page wait.

2026-11-03. What the Reports pages do now:
  * ONE read (`/api/reports/overview`) for templates, schedules, generated
    reports and the scheduler state, on a read-only handle - measured here
    while an outside connection holds SQLite's write lock and a config save
    is queued behind it;
  * a reports-store write that cannot get the writer in time is refused
    with a 503 and the reason, never a hang and never a 500;
  * every completed reports-store write announces `reports_changed` on the
    live stream, so the page refreshes on change instead of every 15 s;
  * a scheduled trigger reads its tag from the latest-value cache - one
    dictionary lookup, no store.

    python scripts/test_reports_pages_never_wait.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import time

_TMP = tempfile.mkdtemp(prefix="tn_rep_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-rep"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-70s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def _ts(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) + ".000"


def main() -> int:
    print("the Reports module never fails to load and never waits")
    from app.state import app_store, plc_manager, reports_store, _scheduler_live_lookup
    from app.tenant import set_current_tenant
    set_current_tenant("tenant-cust-rep")
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth import create_access_token
    tok = create_access_token({"sub": "admin", "role": "admin", "tenant_id": "tenant-cust-rep",
                               "permissions": {}, "modules": []}, expires_seconds=600)
    H = {"Authorization": "Bearer " + tok, "Host": "127.0.0.1:8000"}
    client = TestClient(app, client=("127.0.0.1", 50000))

    # ---- the announcement: capture what the store fans out -----------------
    seen = []
    orig_fanout = plc_manager.fanout_threadsafe
    plc_manager.fanout_threadsafe = lambda msg: seen.append(dict(msg or {}))
    try:
        r = client.post("/api/reports/templates", headers=H,
                        json={"name": "T1", "description": "", "definition": {"sections": []}})
        check("a template saves on a free store", r.status_code == 200, "status=%s %s" % (r.status_code, r.text[:160]))
        check("  and the store announced reports_changed on the live stream",
              any(m.get("type") == "reports_changed" for m in seen), repr(seen)[:160])
    finally:
        plc_manager.fanout_threadsafe = orig_fanout

    # ---- one read for the whole page ---------------------------------------
    r = client.get("/api/reports/overview?limit=50", headers=H)
    body = r.json() if r.status_code == 200 else {}
    check("GET /api/reports/overview answers 200", r.status_code == 200, "status=%s" % r.status_code)
    check("  with templates, schedules, generated and the scheduler state",
          all(k in body for k in ("templates", "schedules", "generated", "scheduler"))
          and any(t.get("name") == "T1" for t in body.get("templates") or [])
          and "any_gateway_running" in (body.get("scheduler") or {}), repr(body)[:200])

    # ---- while the writer is held and a config save is queued ---------------
    held = threading.Event()
    release = threading.Event()

    def holder():
        c = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"], timeout=10)
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT INTO app_logs(tenant_id, ts_utc, level, category, message, created_utc) "
                  "VALUES('t', ?, 'info', 'x', 'holder', ?)", (_ts(time.time()), _ts(time.time())))
        held.set()
        release.wait(timeout=90)
        c.commit()
        c.close()
    threading.Thread(target=holder, daemon=True).start()
    held.wait(5)

    def blocked():
        try:
            app_store.upsert_domain_scoped("tenant-cust-rep|-|e", "alarms_setup", [{"id": "a"}], actor="bg")
        except Exception:
            pass
    threading.Thread(target=blocked, daemon=True).start()
    time.sleep(0.5)

    t0 = time.perf_counter()
    r = client.get("/api/reports/overview?limit=200", headers=H)
    dt = time.perf_counter() - t0
    check("the overview answers in under 2 s while the writer is held and a save is queued",
          r.status_code == 200 and dt < 2.0, "status=%s %.2fs" % (r.status_code, dt))

    t0 = time.perf_counter()
    r = client.post("/api/reports/templates", headers=H,
                    json={"name": "T2", "description": "", "definition": {"sections": []}})
    dt = time.perf_counter() - t0
    check("a reports write while the writer is held is refused within 16 s", dt < 16.0, "%.1fs" % dt)
    check("  as a 503 that says the store is busy, not a 500",
          r.status_code == 503 and "busy" in r.text.lower() and "lock-watchdog" in r.text, "status=%s %s" % (r.status_code, r.text[:160]))

    release.set()
    time.sleep(1.0)
    r = client.post("/api/reports/templates", headers=H,
                    json={"name": "T2", "description": "", "definition": {"sections": []}})
    check("once the writer is free the same write lands", r.status_code == 200, "status=%s %s" % (r.status_code, r.text[:120]))

    # ---- a trigger reads its tag from the cache, not the store --------------
    app_store.append_historian_rows([{"ts_utc": _ts(time.time()), "gateway_id": "gw9", "gateway_name": "PLC",
                                      "device_name": "d", "tag_name": "Level", "value": 42.5, "quality": 192,
                                      "quality_label": "GOOD", "source": "test"}])
    orig_live = app_store.get_live_rows
    app_store.get_live_rows = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("store touched"))
    try:
        v = _scheduler_live_lookup("gw9", "level")
    finally:
        app_store.get_live_rows = orig_live
    check("a trigger lookup finds the latest value without touching the store", v == 42.5, repr(v))

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
