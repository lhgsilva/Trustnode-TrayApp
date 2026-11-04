# -*- coding: utf-8 -*-
"""An account still filed under `default` sees this edge's data, on the desktop and over LAN.

2026-11-04, reported: "when the LAN connection is opened by other users the
reports generated are not showing on the LAN view". Accounts created before
the 2026-11-02 tenant fix carry tenant `default` in their token, and every
request of theirs ran as the developer tenant - 0 reports, where an account
under the edge's tenant saw them all. On an activated edge such a session
now runs as the edge's tenant; a token naming a real customer is unchanged.

    python scripts/test_default_account_sees_edge.py
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="tn_defacct_")
atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-lan"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-70s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def main() -> int:
    print("an account filed under `default` sees this edge's data")
    from app.tenant import session_tenant
    check("a `default` token on an activated edge runs as the edge's tenant",
          session_tenant("default", "tenant-cust-lan") == "tenant-cust-lan")
    check("  a token naming a real customer tenant is never changed",
          session_tenant("tenant-cust-other", "tenant-cust-lan") == "tenant-cust-other")
    check("  an edge with no identity (the portal) keeps `default`",
          session_tenant("default", "default") == "default" and session_tenant("default", "") == "default")

    from app.state import reports_store
    from app.tenant import set_current_tenant
    set_current_tenant("tenant-cust-lan")
    reports_store.insert_generated({"template_id": "x", "template_name": "T", "triggered_by": "manual",
                                    "file_path": "", "file_name": "a.pdf", "file_bytes": 1, "file_sha256": "",
                                    "meta": {"render_state": "ready"}})
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth import create_access_token
    for who, tenant in (("an account under the edge's tenant", "tenant-cust-lan"),
                        ("an account still filed under default", "default")):
        tok = create_access_token({"sub": "op", "role": "admin", "tenant_id": tenant,
                                   "permissions": {}, "modules": []}, expires_seconds=600)
        for where, ip, host in (("desktop", "127.0.0.1", "127.0.0.1:8000"),
                                ("LAN", "192.168.0.50", "192.168.0.161:8000")):
            c = TestClient(app, client=(ip, 50000))
            r = c.get("/api/reports/overview", headers={"Authorization": "Bearer " + tok, "Host": host})
            b = r.json() if r.status_code == 200 else {}
            check("%s, %s: sees this edge's generated report" % (who, where),
                  r.status_code == 200 and b.get("tenant_id") == "tenant-cust-lan" and len(b.get("generated") or []) == 1,
                  "status=%s tenant=%s generated=%s" % (r.status_code, b.get("tenant_id"), len(b.get("generated") or [])))
    other = create_access_token({"sub": "x", "role": "admin", "tenant_id": "tenant-cust-other",
                                 "permissions": {}, "modules": []}, expires_seconds=600)
    r = TestClient(app, client=("192.168.0.50", 50000)).get(
        "/api/reports/overview", headers={"Authorization": "Bearer " + other, "Host": "192.168.0.161:8000"})
    b = r.json() if r.status_code == 200 else {}
    check("another customer's account still sees only its own tenant (no cross-scope read)",
          b.get("tenant_id") == "tenant-cust-other" and len(b.get("generated") or []) == 0,
          "tenant=%s generated=%s" % (b.get("tenant_id"), len(b.get("generated") or [])))

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
