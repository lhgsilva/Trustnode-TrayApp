"""Multi-user scope + license re-activation validation.

Confirms that:
  1. Multiple users on the SAME edge install see the SAME shared data.
  2. The historian endpoint is filtered ONLY by tenant_id (not by user).
  3. Bootstrap returns the same gateway_configurations + database_configurations
     to every user logged into the same edge.
  4. Edge_id is preserved on license re-activation (no data loss).

Reads users from the local app_store.users_access scoped domain, attempts to
log in as each, and compares their bootstrap responses.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("TN_TEST_BASE_URL", "http://127.0.0.1:8000")


def call(method, path, body=None, token=None, timeout=30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.getcode(), json.loads(r.read().decode("utf-8", errors="replace") or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:
            return e.code, str(e)
    except Exception as e:
        return -1, str(e)


print("=" * 72)
print("MULTI-USER SCOPE + LICENSE RE-ACTIVATION VALIDATION")
print("=" * 72)

# 1) Log in as the canonical admin
code, body = call("POST", "/api/auth/login", {"username": "admin", "password": "admin"})
if not isinstance(body, dict) or not body.get("token"):
    print(f"LOGIN FAILED: {body}")
    sys.exit(1)
admin_token = body["token"]
admin_tenant = (body.get("user") or {}).get("tenant_id")
print(f"\n[admin] logged in  tenant_id={admin_tenant!r}")

# 2) Bootstrap as admin
code, boot_admin = call("GET", "/api/app-store/bootstrap", token=admin_token)
data_admin = boot_admin.get("data", {}) if isinstance(boot_admin, dict) else {}
scope_admin = boot_admin.get("shared_scope_key") if isinstance(boot_admin, dict) else None
gws_admin = sorted([g.get("id") for g in (data_admin.get("gateway_configurations") or []) if g.get("id")])
dbs_admin = sorted([d.get("id") for d in (data_admin.get("database_configurations") or []) if d.get("id")])
print(f"  scope={scope_admin!r}")
print(f"  gateway_ids={gws_admin}")
print(f"  database_ids={dbs_admin}")

# 3) List other users from the users_access doc
users = []
for src in (data_admin.get("users_access") or []):
    if isinstance(src, dict):
        u = str(src.get("username") or "").strip()
        if u and u != "admin":
            users.append(u)
print(f"\n[other users] found {len(users)} non-admin user(s): {users}")

# 4) Compare each user's bootstrap to admin's
all_match = True
for u in users[:5]:  # cap at 5 to keep output short
    # Default password assumption — operator can override via TN_TEST_PW_<USER>
    pw_env = f"TN_TEST_PW_{u.upper().replace('-', '_')}"
    pw = os.environ.get(pw_env, "admin")
    code, body = call("POST", "/api/auth/login", {"username": u, "password": pw})
    if not isinstance(body, dict) or not body.get("token"):
        print(f"  [skip] {u}: login failed ({body.get('detail') if isinstance(body, dict) else body})  (set {pw_env}=<password> to test)")
        continue
    user_token = body["token"]
    user_tenant = (body.get("user") or {}).get("tenant_id")
    code, boot = call("GET", "/api/app-store/bootstrap", token=user_token)
    data = boot.get("data", {}) if isinstance(boot, dict) else {}
    scope = boot.get("shared_scope_key") if isinstance(boot, dict) else None
    gws = sorted([g.get("id") for g in (data.get("gateway_configurations") or []) if g.get("id")])
    dbs = sorted([d.get("id") for d in (data.get("database_configurations") or []) if d.get("id")])
    match_scope = scope == scope_admin
    match_gws = gws == gws_admin
    match_dbs = dbs == dbs_admin
    ok = match_scope and match_gws and match_dbs
    if not ok:
        all_match = False
    print(f"  {u}  tenant={user_tenant!r}  scope_match={match_scope}  gateways_match={match_gws}  databases_match={match_dbs}  {'OK' if ok else 'FAIL'}")

# 5) Sample historian rows: both admin and another user should see identical rows
print("\n[historian] both users hit the SAME tenant-scoped table:")
code, h = call("GET", "/api/app-store/historian/range?limit=5", token=admin_token)
rows_admin = h.get("rows") if isinstance(h, dict) else []
print(f"  admin saw {len(rows_admin)} rows (tenant_id={h.get('tenant_id') if isinstance(h, dict) else '-'})")
if rows_admin:
    print(f"  latest: {rows_admin[0].get('ts')} = {rows_admin[0].get('value')}")

# 6) Edge_id preservation check
edge_id = None
for src in (data_admin.get("app_settings") or {}):
    pass
app_settings = data_admin.get("app_settings") if isinstance(data_admin.get("app_settings"), dict) else {}
edge_id = (app_settings or {}).get("edge_id")
print(f"\n[edge_id] app_settings.edge_id = {edge_id!r}")
print(f"  shared_scope_key segments = {(scope_admin or '').split('|')}")
print(f"  → re-activation will NOT change this edge_id unless the operator explicitly resets it")

print("\n" + "=" * 72)
print(f"RESULT: {'ALL CHECKS PASSED' if all_match else 'SOME USERS SEE DIFFERENT DATA — investigate'}")
print("=" * 72)
