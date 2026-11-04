# -*- coding: utf-8 -*-
"""An activated edge never files anything under the developer tenant.

2026-11-02, from the owner: "`default` is where we have our development
users to control the licences in the portal. None of customer or new
installations should have a default-tenant user. Every new installation
is always linked to a tenant customer id from the portal."

What the code did: the desktop app always arrives from 127.0.0.1, and the
host rule maps loopback to `default`. So every request that ran before a
token existed - the login itself - or with a token carrying no tenant, ran
as `default`. Measured on the live store of an edge activated into
tenant-cust-11e164d2:

    historian_readings.tenant_id : default, tenant-cust-11e164d2, ...
    cp_users                     : (default, admin), (tenant-cust-11e164d2, admin)

And `migrate_from_app_store_payload` homed every user the Users page
created under `default`, because that page never sends a tenant.

The rule now (app/tenant.py::effective_tenant): token tenant, else a host
that NAMES a tenant, else the tenant this edge was activated into. The
strict token-vs-host check keys on the host alone, so a user whose row
still says `default` keeps signing in - a lock-out is not a fix.

    python scripts/test_edge_tenant_is_never_default.py
"""
from __future__ import annotations

import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="tn_tenant_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ.pop("TRUSTNODE_TENANT_ID", None)
os.environ.pop("TRUSTNODE_MASTER_ADMIN_PASSWORD", None)
os.environ.pop("TRUSTNODE_MASTER_ADMIN_USER", None)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

PASS, FAIL = [], []
EDGE_TENANT = "tenant-cust-t1"


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-66s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def main() -> int:
    print("an activated edge never files anything under `default`")

    # ------------------------------------------------------------ the rule
    from app.tenant import effective_tenant, get_current_tenant
    check("a token's tenant wins", effective_tenant("tenant-a", "tenant-b", "tenant-c") == "tenant-a")
    check("a host that names a tenant wins over the activated one",
          effective_tenant("", "tenant-b", "tenant-c") == "tenant-b")
    check("loopback (`default` host) on an activated edge is the edge's tenant",
          effective_tenant("", "default", EDGE_TENANT) == EDGE_TENANT,
          "this is the whole fix")
    check("  a `default` token on an activated edge is also the edge's tenant",
          effective_tenant("default", "default", EDGE_TENANT) == EDGE_TENANT)
    check("an edge with no identity is still `default`",
          effective_tenant("", "default", "") == "default")

    # ------------------------------------------- the four request entry points
    with open(os.path.join(HERE, "..", "backend", "app", "main.py"), encoding="utf-8") as fh:
        src = fh.read()
    n_sites = src.count('effective_tenant("", host_tenant, _configured_tenant())')
    check("every request entry point applies it (http, 2x websocket, asgi)", n_sites == 4,
          "found %d of 4" % n_sites)
    check("  the strict token-vs-host check keys on the HOST, not the fallback",
          'if host_tenant != "default" and normalized_token_tenant != host_tenant' in src
          and 'if tenant_id != "default" and normalized_token_tenant != tenant_id' not in src,
          "otherwise a user whose row still says `default` gets 403 on every call")
    check("  the activated tenant is cached, not read per request",
          "_CONFIGURED_TENANT_CACHE" in src)

    # ------------------------------------------------------- the user mirror
    from app.services.auth_store import AuthStore
    from app.auth import hash_password
    store = AuthStore(os.path.join(_TMP, "auth.db"))
    h = hash_password("pw")
    store.migrate_from_app_store_payload(
        {"users": [{"username": "newguy", "password": h, "role": "operator"}]},
        default_tenant=EDGE_TENANT)
    check("a user created on an activated edge is homed in the edge's tenant",
          (store.get_user("newguy") or {}).get("tenant_id") == EDGE_TENANT,
          "got %r" % (store.get_user("newguy") or {}).get("tenant_id"))
    store.migrate_from_app_store_payload(
        {"users": [{"username": "saysdefault", "password": h, "role": "operator",
                    "tenant_id": "default"}]},
        default_tenant=EDGE_TENANT)
    check("  a document that says `default` does not override the edge's tenant",
          (store.get_user("saysdefault") or {}).get("tenant_id") == EDGE_TENANT)
    store.migrate_from_app_store_payload(
        {"users": [{"username": "fromdoc", "password": h, "role": "operator",
                    "tenant_id": "tenant-cust-other"}]},
        default_tenant=EDGE_TENANT)
    check("  a document naming a real tenant is respected",
          (store.get_user("fromdoc") or {}).get("tenant_id") == "tenant-cust-other")
    store.upsert_user(username="olduser", password_hash=h, role="admin",
                      tenant_id="default", status="active")
    store.migrate_from_app_store_payload(
        {"users": [{"username": "olduser", "password": h, "role": "admin"}]},
        default_tenant=EDGE_TENANT)
    check("  an existing account keeps its tenant (no silent re-homing)",
          (store.get_user("olduser") or {}).get("tenant_id") == "default")
    store.migrate_from_app_store_payload(
        {"users": [{"username": "noidentity", "password": h, "role": "operator"}]},
        default_tenant="")
    check("  an edge with no identity still homes a user in `default`",
          (store.get_user("noidentity") or {}).get("tenant_id") == "default")

    # ------------------------------------------- the real app, over loopback
    from fastapi.testclient import TestClient
    from app.state import app_store, auth_store, control_plane_store
    from app.main import app, _main_app
    from app.auth import decode_access_token

    # Activate the edge the way activation does: tenant into app_settings.
    app_store.upsert_domain("app_settings", {"tenant_id": EDGE_TENANT,
                                             "customer_id": "cust-t1",
                                             "edge_id": "edge-t1"})
    check("the store reports the activated tenant",
          app_store._configured_tenant_id() == EDGE_TENANT)

    @_main_app.get("/__tenant_probe")
    def _probe():
        return {"tenant": get_current_tenant()}

    client = TestClient(app)          # no lifespan: the middleware is the subject
    r = client.get("/__tenant_probe", headers={"Host": "127.0.0.1:8000"})
    check("a loopback request with no token runs as the edge's tenant",
          r.status_code == 200 and r.json().get("tenant") == EDGE_TENANT,
          "%s %s" % (r.status_code, r.text[:120]))

    # The customer's admin, created locally; and a leftover `default` row.
    auth_store.upsert_user(username="admin", password_hash=hash_password("cust-pw"),
                           role="admin", tenant_id=EDGE_TENANT, status="active")
    auth_store.upsert_user(username="olduser", password_hash=hash_password("old-pw"),
                           role="admin", tenant_id="default", status="active")

    r = client.post("/api/auth/login", json={"username": "admin", "password": "cust-pw"},
                    headers={"Host": "127.0.0.1:8000"})
    tok = (r.json() or {}).get("token", "") if r.status_code == 200 else ""
    check("the customer's admin signs in", r.status_code == 200, "%s %s" % (r.status_code, r.text[:160]))
    check("  and their token carries the customer's tenant, not `default`",
          bool(tok) and decode_access_token(tok).get("tenant_id") == EDGE_TENANT)
    r = client.get("/api/app-store/bootstrap", headers={"Authorization": "Bearer " + tok,
                                                        "Host": "127.0.0.1:8000"})
    check("  and can read config", r.status_code == 200, "%s %s" % (r.status_code, r.text[:120]))

    r = client.post("/api/auth/login", json={"username": "olduser", "password": "old-pw"},
                    headers={"Host": "127.0.0.1:8000"})
    # 2026-11-04, from the owner: the developer tenant is never linked to a
    # customer edge and admin accounts are not moved into it. An ADMIN row
    # still under `default` is a developer account: refused here, with the
    # reason (non-admin rows sign in as the edge - test_default_account_sees_edge).
    check("an ADMIN whose row still says `default` is refused on a customer edge",
          r.status_code == 403 and "developer" in r.text.lower(),
          "%s %s" % (r.status_code, r.text[:160]))

    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin"},
                    headers={"Host": "127.0.0.1:8000"})
    check("admin/admin does not open the customer's edge", r.status_code != 200,
          "break-glass must never shadow a real account: %s %s" % (r.status_code, r.text[:200]))
    seeded = [u for u in control_plane_store.list_users(all_tenants=True)
              if u.get("tenant_id") == "default" and u.get("username") == "admin"]
    check("the control-plane store no longer seeds admin/admin under `default`", not seeded,
          "that row, plus the any-tenant login fallback, opened every customer edge")
    # A developer-tenant row - the portal's own user - on a customer's edge.
    control_plane_store.upsert_user(tenant_id="default", username="devadmin",
                                    password="dev-pw", role="admin")
    r = client.post("/api/auth/login", json={"username": "devadmin", "password": "dev-pw"},
                    headers={"Host": "127.0.0.1:8000"})
    check("a `default`-tenant control-plane row cannot sign in on an activated edge",
          r.status_code != 200, "%s %s" % (r.status_code, r.text[:160]))

    # A portal-mirrored user (cp_users only) under the customer's tenant.
    try:
        control_plane_store.upsert_user(tenant_id=EDGE_TENANT, username="portaluser",
                                        password="portal-pw", role="operator")
        r = client.post("/api/auth/login", json={"username": "portaluser", "password": "portal-pw"},
                        headers={"Host": "127.0.0.1:8000"})
        check("a portal-synced user signs in locally, with no internet",
              r.status_code == 200, "%s %s" % (r.status_code, r.text[:160]))
    except TypeError as exc:
        check("a portal-synced user signs in locally, with no internet", False, repr(exc))

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
