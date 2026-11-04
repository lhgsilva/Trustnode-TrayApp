# -*- coding: utf-8 -*-
"""No developer tenant on a customer edge.

2026-11-04, from the owner: "we never should use the developer tenant on
local edges; the edge is linked to a customer and licences and the LAN
should only see this; the developer tenant should never be linked to a
local edge customer" - and admin accounts are NOT moved into the customer.

On an edge activated into a customer tenant:
  * this edge's accounts sign in and see its data, desktop and LAN;
  * a NON-admin account still filed under `default` (created before the
    2026-11-02 fix) signs in as the edge's tenant and is moved into it at
    boot;
  * an ADMIN account under `default` (a developer account) is refused and
    stays where it is; an old token of one is never given this edge's data;
  * another customer's account is refused;
  * the local recovery login runs as the edge's tenant;
  * content seeded under `default` before activation moves into the edge
    (a template whose name the edge already has is skipped), and the
    control-plane tables are left alone.

    python scripts/test_default_account_sees_edge.py
"""
from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="tn_defacct_")
atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-lan"
os.environ.pop("TRUSTNODE_MASTER_ADMIN_PASSWORD", None)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
PASS, FAIL = [], []
EDGE = "tenant-cust-lan"


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-70s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def main() -> int:
    print("no developer tenant on a customer edge")
    from app.tenant import edge_session_decision
    check("the rule: this edge's account -> as it is",
          edge_session_decision(EDGE, "admin", EDGE) == (True, EDGE, ""))
    check("  a default NON-admin -> runs as the edge",
          edge_session_decision("default", "operator", EDGE) == (True, EDGE, ""))
    check("  a default ADMIN -> refused (developer account)",
          edge_session_decision("default", "admin", EDGE)[0] is False)
    check("  another customer's account -> refused",
          edge_session_decision("tenant-cust-other", "admin", EDGE)[0] is False)
    check("  the recovery login -> runs as the edge",
          edge_session_decision("default", "admin", EDGE, recovery=True) == (True, EDGE, ""))
    check("  an edge with no identity decides nothing",
          edge_session_decision("default", "admin", "default") == (True, "default", ""))

    from app.state import reports_store, auth_store, app_store
    from app.tenant import set_current_tenant
    from app.auth import hash_password, create_access_token
    set_current_tenant(EDGE)
    reports_store.insert_generated({"template_id": "x", "template_name": "T", "triggered_by": "manual",
                                    "file_path": "", "file_name": "a.pdf", "file_bytes": 1, "file_sha256": "",
                                    "meta": {"render_state": "ready"}})
    for user, role, tenant in (("edge-admin", "admin", EDGE), ("old-operator", "operator", "default"),
                               ("dev-admin", "admin", "default"), ("other-admin", "admin", "tenant-cust-other")):
        auth_store.upsert_user(username=user, password_hash=hash_password("Secret-2026!"), role=role,
                               tenant_id=tenant, status="active")

    from fastapi.testclient import TestClient
    from app.main import app
    import base64
    import json

    def claims(tok):
        return json.loads(base64.urlsafe_b64decode(tok.split(".")[1] + "=="))
    for where, ip, host in (("desktop", "127.0.0.1", "127.0.0.1:8000"), ("LAN", "192.168.0.50", "192.168.0.161:8000")):
        c = TestClient(app, client=(ip, 50000))
        H = {"Host": host}
        r = c.post("/api/auth/login", headers=H, json={"username": "edge-admin", "password": "Secret-2026!"})
        tok = (r.json() or {}).get("token", "") if r.status_code == 200 else ""
        ov = c.get("/api/reports/overview", headers={**H, "Authorization": "Bearer " + tok}).json() if tok else {}
        check("%s: this edge's admin signs in and sees its report" % where,
              bool(tok) and len(ov.get("generated") or []) == 1, "status=%s" % r.status_code)
        r = c.post("/api/auth/login", headers=H, json={"username": "old-operator", "password": "Secret-2026!"})
        tok = (r.json() or {}).get("token", "") if r.status_code == 200 else ""
        ov = c.get("/api/reports/overview", headers={**H, "Authorization": "Bearer " + tok}).json() if tok else {}
        check("%s: a default non-admin signs in as the edge and sees its report" % where,
              bool(tok) and claims(tok).get("tenant_id") == EDGE and len(ov.get("generated") or []) == 1,
              "status=%s %s" % (r.status_code, r.text[:100]))
        r = c.post("/api/auth/login", headers=H, json={"username": "dev-admin", "password": "Secret-2026!"})
        check("%s: a developer admin account is refused" % where,
              r.status_code == 403 and "developer" in r.text.lower(), "status=%s %s" % (r.status_code, r.text[:100]))
        r = c.post("/api/auth/login", headers=H, json={"username": "other-admin", "password": "Secret-2026!"})
        check("%s: another customer's admin is refused" % where,
              r.status_code == 403 and "another customer" in r.text.lower(), "status=%s %s" % (r.status_code, r.text[:100]))

    old_dev = create_access_token({"sub": "dev-admin", "role": "admin", "tenant_id": "default",
                                   "permissions": {}, "modules": []}, expires_seconds=600)
    ov = TestClient(app, client=("192.168.0.50", 50000)).get(
        "/api/reports/overview", headers={"Authorization": "Bearer " + old_dev, "Host": "192.168.0.161:8000"}).json()
    check("an old developer-admin token is never given this edge's data",
          ov.get("tenant_id") == "default" and len(ov.get("generated") or []) == 0, repr(ov)[:120])

    # ---- the boot move ----------------------------------------------------
    con = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
    con.execute("INSERT INTO report_templates(id, tenant_id, name, description, definition_json, created_utc, updated_utc, created_by) "
                "VALUES('seeded-1','default','Seeded Before Activation','', '{}', '2026-11-01', '2026-11-01', 'seed')")
    con.execute("INSERT INTO report_templates(id, tenant_id, name, description, definition_json, created_utc, updated_utc, created_by) "
                "VALUES('seeded-2','default','Already Here','', '{}', '2026-11-01', '2026-11-01', 'seed')")
    con.execute("INSERT INTO report_templates(id, tenant_id, name, description, definition_json, created_utc, updated_utc, created_by) "
                "VALUES('mine-1',?,'Already Here','', '{}', '2026-11-01', '2026-11-01', 'op')", (EDGE,))
    con.execute("INSERT INTO cp_tenants(tenant_id, name, status, created_utc, updated_utc) "
                "VALUES('default','Developer','active','2026-11-01','2026-11-01') ON CONFLICT DO NOTHING")
    con.commit()
    cp_before = con.execute("SELECT COUNT(*) FROM cp_tenants WHERE tenant_id='default'").fetchone()[0]
    con.close()
    moved_users = auth_store.rehome_default_non_admins(EDGE)
    check("boot: the default non-admin account moves into the edge", moved_users == ["old-operator"], repr(moved_users))
    check("  the developer admin account is NOT moved",
          (auth_store.get_user("dev-admin") or {}).get("tenant_id") == "default")
    con = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
    con.execute("INSERT INTO historian_rollup(resolution_s, tenant_id, gateway_id, tag_name, bucket_ms, n, sum_v) "
                "VALUES(60, 'default', 'gw1', 'T', 1000, 1, 1.0)")
    con.commit()
    con.close()
    import time as _time
    _t0 = _time.time()
    moved = app_store.rehome_default_content(EDGE, batch=1)
    check("boot: a WITHOUT ROWID table (historian_rollup) moves too, at once",
          moved.get("historian_rollup") == 1 and _time.time() - _t0 < 30, "%r in %.0fs" % (moved, _time.time() - _t0))
    con = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
    t1 = con.execute("SELECT tenant_id FROM report_templates WHERE id='seeded-1'").fetchone()[0]
    t2 = con.execute("SELECT tenant_id FROM report_templates WHERE id='seeded-2'").fetchone()[0]
    n_same = con.execute("SELECT COUNT(*) FROM report_templates WHERE tenant_id=? AND name='Already Here'", (EDGE,)).fetchone()[0]
    cp_after = con.execute("SELECT COUNT(*) FROM cp_tenants WHERE tenant_id='default'").fetchone()[0]
    con.close()
    check("boot: a seeded template moves into the edge", t1 == EDGE, "%s %r" % (t1, moved))
    check("  one whose name the edge already has is skipped (no duplicate)", t2 == "default" and n_same == 1)
    check("  control-plane rows are left alone", cp_before == cp_after and cp_after >= 1, "%s -> %s" % (cp_before, cp_after))
    again = app_store.rehome_default_content(EDGE)
    check("  and a second pass has nothing more to move", not again.get("report_templates"), repr(again))

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
