# -*- coding: utf-8 -*-
"""A busy store writer never makes the edge look dead.

2026-11-03, from a customer edge: every configuration save "Timed out after
20s", for six domains, every ten to twenty seconds; the gateway flipped 0/1;
the WebSocket kept reconnecting; plain reads took 3.5-5 s. Measured here
with one outside connection holding SQLite's write lock (what an unbounded
maintenance job, a stuck sink or a full disk looks like from inside):

  * app_store's reads and writes all went through ONE Python lock, and a
    write held that lock while it waited up to 10 s for SQLite. Six saves
    queued behind it answered at 10, 20, 30, 40, 50 and 60 s - every one
    past the page's 20 s - and the historian and log writers died with
    "database is locked" in between.
  * the polled reads (live rows, logs, bootstrap, inspector) waited on the
    same lock, so status, Logs and the live view stalled too: "reconnecting".

Now a read never waits for a writer, and a save or log write from the page
answers within its window - saved, or refused with the reason - instead of
queuing behind whatever holds the writer.

    python scripts/test_store_busy_never_hangs.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import time

_TMP = tempfile.mkdtemp(prefix="tn_busy_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-busy"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-70s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def _ts(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) + ".000"


SCOPE = "tenant-cust-busy|-|edge-busy"
DOMAINS = ["gateway_configurations", "devices", "database_configurations",
           "power_management_config", "dashboard_configurations", "alarms_setup"]


def _payload(domain, n):
    return {"n": n} if domain.endswith("config") else [{"id": "a", "n": n}]


def main() -> int:
    print("a busy store writer never makes the edge look dead")
    from app.state import app_store
    from app.tenant import set_current_tenant
    set_current_tenant("tenant-cust-busy")
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth import create_access_token
    tok = create_access_token({"sub": "admin", "role": "admin", "tenant_id": "tenant-cust-busy",
                               "permissions": {}, "modules": []}, expires_seconds=600)
    H = {"Authorization": "Bearer " + tok, "Host": "127.0.0.1:8000"}
    client = TestClient(app, client=("127.0.0.1", 50000))

    # Something to read, and a stored document for each domain.
    app_store.append_historian_rows([{
        "ts_utc": _ts(time.time() - i), "gateway_id": "gw1", "gateway_name": "PLC",
        "device_name": "d", "tag_name": "T%d" % (i % 5), "value": float(i), "quality": 192,
        "quality_label": "GOOD", "source": "test"} for i in range(200)])
    app_store.append_log_rows([{"ts_utc": _ts(time.time()), "level": "info",
                                "category": "test", "message": "hello"}])
    for d in DOMAINS:
        app_store.upsert_domain_scoped(SCOPE, d, _payload(d, 0), actor="test")
    r = client.put("/api/app-store/domain", headers=H,
                   json={"domain": "devices", "payload": _payload("devices", 1), "actor": "ui"})
    check("a save on a free store answers 200", r.status_code == 200,
          "status=%s %s" % (r.status_code, r.text[:120]))

    # ---- an outside connection holds SQLite's write lock for 25 s ----------
    held = threading.Event()
    release = threading.Event()

    def holder():
        c = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"], timeout=10)
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT INTO app_logs(tenant_id, ts_utc, level, category, message, created_utc) "
                  "VALUES('tenant-cust-busy', ?, 'info', 'x', 'holder', ?)", (_ts(time.time()), _ts(time.time())))
        held.set()
        release.wait(timeout=150)
        c.commit()
        c.close()
    threading.Thread(target=holder, daemon=True).start()
    held.wait(5)

    # One store-level save queues behind the holder, as a background mirror
    # does; everything below must still answer while it waits.
    def blocked_save():
        try:
            app_store.upsert_domain_scoped(SCOPE, "alarms_setup", _payload("alarms_setup", 2), actor="bg")
        except Exception:
            pass
    threading.Thread(target=blocked_save, daemon=True).start()
    time.sleep(0.5)

    # ---- six page saves at once, queued behind the holder like the page's ---
    results = {}

    def save(d):
        t0 = time.perf_counter()
        r = client.put("/api/app-store/domain", headers=H,
                       json={"domain": d, "payload": _payload(d, 3), "actor": "ui"})
        results[d] = (r.status_code, time.perf_counter() - t0, r.text[:600])
    ths = [threading.Thread(target=save, args=(d,), daemon=True) for d in DOMAINS]
    for t in ths:
        t.start()
    time.sleep(0.5)

    def timed(fn):
        t0 = time.perf_counter()
        try:
            out = fn()
        except Exception as exc:
            out = exc
        return out, time.perf_counter() - t0

    out, dt = timed(lambda: app_store.get_live_rows(limit=100))
    check("live rows are read in under a second while the writer is held",
          dt < 1.0 and not isinstance(out, Exception),
          "%.1fs %r" % (dt, out if isinstance(out, Exception) else len(out)))
    out, dt = timed(lambda: app_store.get_log_rows(limit=100))
    check("log rows too", dt < 1.0 and not isinstance(out, Exception), ("%.1fs %r" % (dt, out))[:160])
    out, dt = timed(lambda: app_store.get_bootstrap(prefer_cloud_reads=False))
    check("the bootstrap too", dt < 1.0 and not isinstance(out, Exception), ("%.1fs %r" % (dt, out))[:160])
    out, dt = timed(lambda: app_store.get_config_domain("app_settings", {}))
    check("a config domain too", dt < 1.0 and not isinstance(out, Exception), ("%.1fs %r" % (dt, out))[:160])
    out, dt = timed(lambda: app_store.get_inspector_snapshot(preview_limit=0))
    check("the inspector too", dt < 1.5 and not isinstance(out, Exception), ("%.1fs %r" % (dt, out))[:160])
    out, dt = timed(lambda: client.get("/api/app-store/bootstrap", headers=H))
    check("GET /bootstrap over HTTP answers in under two seconds",
          dt < 2.0 and getattr(out, "status_code", 0) == 200,
          "%.1fs %s" % (dt, getattr(out, "status_code", out)))

    out, dt = timed(lambda: client.post("/api/app-store/append/logs", headers=H,
                                        json={"rows": [{"ts_utc": _ts(time.time()), "level": "error",
                                                        "category": "config", "message": "x"}]}))
    check("a page log write answers within 16 s while the writer is held",
          dt < 16.0 and getattr(out, "status_code", 0) in (200, 503),
          "%.1fs %s" % (dt, getattr(out, "status_code", out)))
    for t in ths:
        t.join(timeout=90)
    slowest = max(v[1] for v in results.values()) if results else 999
    print("    six saves answered in: " + ", ".join(
        "%s=%.1fs/%s" % (d[:8], v[1], v[0]) for d, v in sorted(results.items(), key=lambda kv: kv[1][1])))
    check("every one of six concurrent saves answers within 16 s (not 10, 20, 30...)",
          len(results) == 6 and slowest < 16.0, "slowest %.1fs" % slowest)
    codes = sorted(set(v[0] for v in results.values()))
    check("  each is refused as 503 'busy', not a 500 or a hang", codes == [503], "codes=%s" % codes)
    sample = next(iter(results.values()))[2].lower() if results else ""
    check("  and the refusal says what to look at", "lock-watchdog" in sample and "busy" in sample, sample[:160])

    # ---- the holder lets go: the edge is simply back ------------------------
    release.set()
    time.sleep(1.0)
    r = client.put("/api/app-store/domain", headers=H,
                   json={"domain": "devices", "payload": _payload("devices", 4), "actor": "ui"})
    check("once the writer is free, the next save is 200", r.status_code == 200,
          "status=%s %s" % (r.status_code, r.text[:120]))
    got = app_store.get_scoped_domain_payload(str((r.json() or {}).get("scope_key") or SCOPE), "devices")
    check("  and it is stored", isinstance(got, list) and bool(got) and got[0].get("n") == 4, repr(got)[:120])

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
