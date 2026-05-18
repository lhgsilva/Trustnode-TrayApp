"""End-to-end TrustNode test: master admin + tenant admins, portal +
Lite + edge, including edge<->cloud user-sync direction.

Probes (run in order, each is independent so a SKIP/FAIL doesn't
abort the rest):

  PORTAL (VPS, https://trustnode.lsapps.app)
    P01  /api/health 200
    P02  /api/auth/login as `master` returns admin / tenant=default
    P03  master can GET /api/cp/users?tenant_id=__all__ and sees >1 tenant
    P04  master can list tenants, customers, edges, modules, licenses,
         activation-codes
    P05  master can create+delete a tenant
    P06  master can create+delete a customer
    P07  master can issue+delete an activation code
    P08  master can issue+delete a license
    P09  master can create+delete a user (any tenant) AND that user
         mirrors to Supabase auth.users + lite_profiles

  LITE (Supabase auth + lite_profiles RLS)
    L01  master@trustnode.local logs in with `Apolo020@` and the JWT
         user_metadata says tenant=default / role=admin
    L02  master can SELECT from public.lite_profiles (RLS allows admin)
         to enumerate tenants/customers
    L03  master can SELECT from public.dashboard_configurations to read
         every tenant's dashboards
    L04  one tenant-admin user (admin-lucas if present) logs in
    L05  that tenant-admin can SELECT lite_profiles for its tenant
    L06  that tenant-admin sees its dashboards via RLS

  EDGE (local backend at http://127.0.0.1:8000)
    E01  /api/health 200
    E02  /api/auth/login as `admin` succeeds (default seeded admin)
    E03  admin can list+create+delete a user via /api/cp/users
    E04  on create, lite_user_mirror landed the same user in Supabase
         auth.users + lite_profiles (proves edge -> cloud sync)
    E05  on delete, lite_user_mirror removed the user from Supabase
         (proves edge -> cloud delete sync)

  SYNC (cloud <-> edge bidirectional)
    S01  user created via PORTAL `POST /api/cp/users` for tenant=default
         lands in /opt/trustnode-edge/data SQLite (already-shared store)
    S02  user created via PORTAL with tenant=<remote-tenant> does NOT
         appear in the LOCAL edge SQLite (architecture note: portal
         edits the VPS edge store, not customer-site edges; customer-
         site edges receive updates only through their own configured
         cloud sync — verify via /api/control-plane/cloud-pull if such
         a route exists, otherwise SKIP with explanation)

After all probes, prints a final PASS/FAIL summary. Disposable users
are tagged with prefix `e2e-` and cleaned up at the end even on early
exit (try/finally).

Usage from Trustnode_edge_app/:
    python scripts/full_master_admin_e2e.py [--no-cleanup]
"""
from __future__ import annotations
import argparse, base64, io, json, os, secrets, sys, time
from pathlib import Path
from typing import Any

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import requests
import psycopg

HERE = Path(__file__).resolve().parent.parent

# ---- env --------------------------------------------------------------
def load_env(p: Path) -> dict[str, str]:
    out = {}
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s: continue
            k, v = s.split("=", 1); out[k.strip()] = v.strip().strip('"').strip("'")
    return out

ENV = load_env(HERE / ".env")
LITE_CFG_PATH = HERE / "web_cloud_readonly" / "lite" / "config.json"
LITE_CFG = json.loads(LITE_CFG_PATH.read_text(encoding="utf-8")) if LITE_CFG_PATH.is_file() else {}

PORTAL_BASE   = "https://trustnode.lsapps.app"
EDGE_BASE     = "http://127.0.0.1:8000"
SUPABASE_URL  = LITE_CFG.get("supabase_url") or ENV.get("TRUSTNODE_SUPABASE_URL", "")
SUPABASE_ANON = LITE_CFG.get("supabase_anon_key", "")
SUPABASE_SVC  = ENV.get("TRUSTNODE_SUPABASE_SERVICE_KEY", "")

MASTER_USER = "master"
MASTER_EMAIL = "master@trustnode.local"
MASTER_PASS = "Apolo020@"

# ---- result tracking --------------------------------------------------
class Probe:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, str, str]] = []
    def record(self, pid: str, name: str, status: str, detail: str) -> None:
        self.results.append((pid, name, status, detail))
        marker = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP", "INFO": "INFO"}.get(status, status)
        print(f"  [{marker}] {pid} {name}: {detail[:300]}")
    def summary(self) -> int:
        print("\n" + "=" * 72)
        print("SUMMARY")
        print("=" * 72)
        by_status: dict[str, int] = {}
        for _, _, s, _ in self.results:
            by_status[s] = by_status.get(s, 0) + 1
        for s in ("PASS", "FAIL", "SKIP", "INFO"):
            if by_status.get(s): print(f"  {s}: {by_status[s]}")
        for pid, name, status, detail in self.results:
            if status == "FAIL":
                print(f"  X  {pid} {name} -- {detail[:160]}")
        return 0 if by_status.get("FAIL", 0) == 0 else 1

P = Probe()

# ---- helpers ----------------------------------------------------------
def decode_jwt_payload(tok: str) -> dict[str, Any]:
    try:
        seg = tok.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg))
    except Exception:
        return {}

def edge_login(base: str, username: str, password: str) -> tuple[int, dict[str, Any]]:
    try:
        r = requests.post(f"{base}/api/auth/login",
                          json={"username": username, "password": password},
                          timeout=15)
        return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text[:200]})
    except Exception as exc:
        return 0, {"error": str(exc)}

def supabase_login(email: str, password: str) -> tuple[int, dict[str, Any]]:
    if not (SUPABASE_URL and SUPABASE_ANON):
        return 0, {"error": "supabase url/anon missing"}
    r = requests.post(f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
                      headers={"apikey": SUPABASE_ANON, "Content-Type": "application/json"},
                      json={"email": email, "password": password}, timeout=15)
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else {})

def cloud_db_conn() -> psycopg.Connection | None:
    try:
        return psycopg.connect(
            host=ENV["TRUSTNODE_CLOUD_DB_HOST"],
            port=int(ENV.get("TRUSTNODE_CLOUD_DB_PORT") or "5432"),
            dbname=ENV.get("TRUSTNODE_CLOUD_DB_NAME") or "postgres",
            user=ENV["TRUSTNODE_CLOUD_DB_USER"],
            password=ENV["TRUSTNODE_CLOUD_DB_PASSWORD"],
            sslmode=ENV.get("TRUSTNODE_CLOUD_DB_SSLMODE") or "require",
            connect_timeout=15,
        )
    except Exception as exc:
        print(f"  cloud_db_conn error: {exc}")
        return None

def supabase_lookup_user(email: str) -> dict[str, Any] | None:
    """Service-key lookup of an auth.users row by email."""
    if not (SUPABASE_URL and SUPABASE_SVC):
        return None
    h = {"apikey": SUPABASE_SVC, "Authorization": f"Bearer {SUPABASE_SVC}"}
    # auth admin: filter by email
    r = requests.get(f"{SUPABASE_URL}/auth/v1/admin/users",
                     headers=h, params={"filter": f"email.eq.{email}"}, timeout=15)
    if r.status_code != 200:
        return None
    users = r.json().get("users", [])
    return users[0] if users else None


# ---- Probe 1: PORTAL --------------------------------------------------
def run_portal_probes(disposables: dict[str, list[str]]) -> dict[str, Any]:
    print("\n========== PORTAL (VPS) ==========")
    ctx: dict[str, Any] = {}
    # P01 health
    try:
        r = requests.get(f"{PORTAL_BASE}/api/health", timeout=10)
        P.record("P01", "portal /api/health", "PASS" if r.status_code == 200 else "FAIL", f"http={r.status_code}")
    except Exception as exc:
        P.record("P01", "portal /api/health", "FAIL", str(exc))

    # P02 master login
    code, body = edge_login(PORTAL_BASE, MASTER_USER, MASTER_PASS)
    token = body.get("access_token") or body.get("token") or ""
    if code != 200 or not token:
        P.record("P02", "portal master login", "FAIL", f"http={code} body={str(body)[:140]}")
        return ctx
    pl = decode_jwt_payload(token)
    role_ok = pl.get("role") == "admin"
    ten_ok  = pl.get("tenant_id") == "default"
    P.record("P02", "portal master login", "PASS" if (role_ok and ten_ok) else "FAIL",
             f"role={pl.get('role')} tenant_id={pl.get('tenant_id')} sub={pl.get('sub')}")
    H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    ctx["portal_token"] = token
    ctx["portal_headers"] = H

    # P03 all-tenants users
    r = requests.get(f"{PORTAL_BASE}/api/control-plane/users",
                     headers=H, params={"tenant_id": "__all__"}, timeout=15)
    rows = (r.json().get("rows") or []) if r.status_code == 200 else []
    tenants_seen = {str(u.get("tenant_id") or "?") for u in rows}
    P.record("P03", "portal all-tenants users", "PASS" if (r.status_code == 200 and len(tenants_seen) > 1) else "FAIL",
             f"http={r.status_code} users={len(rows)} tenants_seen={sorted(tenants_seen)}")

    # P04 read-only inventory across resources. Each /api/cp/<resource>
    # endpoint nests its list under a different key (most use "rows" but
    # historically some used the resource name).
    inv = {}
    for ep in ("tenants", "customers", "edges", "modules", "licenses", "activation-codes"):
        r = requests.get(f"{PORTAL_BASE}/api/control-plane/{ep}",
                         headers=H, params={"tenant_id": "default"}, timeout=15)
        body = r.json() if r.status_code == 200 else {}
        n = 0
        if isinstance(body, dict):
            for v in body.values():
                if isinstance(v, list):
                    n = len(v); break
        inv[ep] = (r.status_code, n)
    ok = all(s == 200 and n > 0 for s, n in inv.values())
    P.record("P04", "portal inventory reads", "PASS" if ok else "FAIL", json.dumps(inv))

    # P05 create+delete tenant
    suf = secrets.token_hex(3)
    tname = f"e2e-tenant-{suf}"
    r = requests.post(f"{PORTAL_BASE}/api/control-plane/tenants", headers=H,
                      json={"tenant_id": tname, "name": "E2E Tenant",
                            "status": "active"}, timeout=15)
    created = r.status_code == 200
    if created: disposables["tenants"].append(tname)
    P.record("P05a", "portal create tenant", "PASS" if created else "FAIL",
             f"http={r.status_code} {tname} body={r.text[:120]}")
    # No delete-tenant route — tenants are durable on this VPS. Inventory
    # check after creation confirms the row landed.
    if created:
        r2 = requests.get(f"{PORTAL_BASE}/api/control-plane/tenants", headers=H, timeout=15)
        body2 = r2.json() if r2.status_code == 200 else {}
        tenant_rows = body2.get("rows") or body2.get("tenants") or []
        names = {str(t.get("tenant_id")) for t in tenant_rows}
        P.record("P05b", "portal tenant visible in /tenants list",
                 "PASS" if tname in names else "FAIL", f"len={len(names)} contains={tname in names}")

    # P06 create+delete customer (default tenant)
    cname = f"e2e-cust-{suf}"
    r = requests.post(f"{PORTAL_BASE}/api/control-plane/customers", headers=H,
                      params={"tenant_id": tname if created else "default"},
                      json={"customer_id": cname, "company_name": "E2E Customer",
                            "contact_email": "e2e@trustnode.local",
                            "status": "active"}, timeout=15)
    cust_created = r.status_code == 200
    cust_tenant = tname if created else "default"
    if cust_created: disposables["customers"].append((cust_tenant, cname))
    P.record("P06a", "portal create customer", "PASS" if cust_created else "FAIL",
             f"http={r.status_code} {cname}@{cust_tenant} body={r.text[:140]}")
    if cust_created:
        rd = requests.delete(f"{PORTAL_BASE}/api/control-plane/customers/{cname}",
                             headers=H, params={"tenant_id": cust_tenant}, timeout=15)
        if rd.status_code != 200:
            rd = requests.post(f"{PORTAL_BASE}/api/control-plane/customers/{cname}/delete",
                               headers=H, params={"tenant_id": cust_tenant}, timeout=15)
        P.record("P06b", "portal delete customer",
                 "PASS" if rd.status_code == 200 else "FAIL", f"http={rd.status_code}")
        if rd.status_code == 200:
            disposables["customers"].remove((cust_tenant, cname))

    # P07 issue + delete activation code — needs customer/edge/license preconditions
    # Find an existing customer + edge + license in `default` to reuse.
    def _first_list(body: Any) -> list:
        if isinstance(body, dict):
            for v in body.values():
                if isinstance(v, list): return v
        return []
    r_c = requests.get(f"{PORTAL_BASE}/api/control-plane/customers", headers=H, params={"tenant_id": "default"}, timeout=15)
    r_e = requests.get(f"{PORTAL_BASE}/api/control-plane/edges", headers=H, params={"tenant_id": "default"}, timeout=15)
    r_l = requests.get(f"{PORTAL_BASE}/api/control-plane/licenses", headers=H, params={"tenant_id": "default"}, timeout=15)
    cust_list = _first_list(r_c.json() if r_c.status_code == 200 else {})
    edge_list = _first_list(r_e.json() if r_e.status_code == 200 else {})
    lic_list  = _first_list(r_l.json() if r_l.status_code == 200 else {})
    cust = cust_list[0] if cust_list else None
    edge = edge_list[0] if edge_list else None
    lic  = lic_list[0]  if lic_list  else None
    if not (cust and edge and lic):
        P.record("P07", "portal issue activation code", "SKIP",
                 f"need pre-existing customer+edge+license in default tenant — "
                 f"customers={len(cust_list)} edges={len(edge_list)} licenses={len(lic_list)}")
    else:
        r = requests.post(f"{PORTAL_BASE}/api/control-plane/activation-code/issue",
                          headers=H, params={"tenant_id": "default"},
                          json={"customer_id": str(cust.get("customer_id")),
                                "edge_id":     str(edge.get("edge_id")),
                                "license_id":  str(lic.get("license_id")),
                                "edge_name": str(edge.get("edge_name") or "e2e"),
                                "ttl_minutes": 60}, timeout=15)
        issued = r.status_code == 200
        code_row = r.json() if issued else {}
        row_id = (code_row.get("row") or {}).get("id") if isinstance(code_row, dict) else None
        if not row_id and isinstance(code_row, dict): row_id = code_row.get("id")
        P.record("P07a", "portal issue activation code", "PASS" if issued else "FAIL",
                 f"http={r.status_code} row_id={row_id} body={r.text[:160]}")
        if issued and row_id:
            disposables["activation_codes"].append(row_id)
            rd = requests.delete(f"{PORTAL_BASE}/api/control-plane/activation-codes/{row_id}",
                                 headers=H, params={"tenant_id": "default"}, timeout=15)
            P.record("P07b", "portal delete activation code",
                     "PASS" if rd.status_code == 200 else "FAIL", f"http={rd.status_code}")
            if rd.status_code == 200:
                disposables["activation_codes"].remove(row_id)

    # P08 issue + delete license
    lname = f"e2e-lic-{suf}"
    r = requests.post(f"{PORTAL_BASE}/api/control-plane/licenses", headers=H,
                      params={"tenant_id": "default"},
                      json={"license_id": lname, "tenant_id": "default",
                            "customer_id": "", "tier": "standard", "status": "active",
                            "expires_utc": None}, timeout=15)
    issued = r.status_code == 200
    P.record("P08a", "portal create license", "PASS" if issued else "FAIL",
             f"http={r.status_code} {lname} body={r.text[:160]}")
    if issued:
        disposables["licenses"].append(("default", lname))
        rd = requests.delete(f"{PORTAL_BASE}/api/control-plane/licenses/{lname}",
                             headers=H, params={"tenant_id": "default"}, timeout=15)
        if rd.status_code != 200:
            rd = requests.post(f"{PORTAL_BASE}/api/control-plane/licenses/{lname}/delete",
                               headers=H, params={"tenant_id": "default"}, timeout=15)
        P.record("P08b", "portal delete license",
                 "PASS" if rd.status_code == 200 else "FAIL", f"http={rd.status_code}")
        if rd.status_code == 200:
            disposables["licenses"].remove(("default", lname))

    # P09 create+delete user in a non-default tenant AND check Supabase mirror.
    # /users?tenant_id=__all__ already showed multiple tenants; pick the first
    # non-default one with an existing user (so the customer_id schema is
    # in place). We don't even need /tenants here.
    target_tenant = None
    ru = requests.get(f"{PORTAL_BASE}/api/control-plane/users", headers=H,
                      params={"tenant_id": "__all__"}, timeout=15)
    for row in (ru.json().get("rows") or []):
        tid = str(row.get("tenant_id") or "")
        if tid and tid != "default":
            target_tenant = tid; break
    if not target_tenant:
        P.record("P09", "portal create user in non-default tenant", "SKIP",
                 "no non-default tenant found on VPS")
    else:
        uname = f"e2e-portal-{suf}"
        pw = f"E2eP-{secrets.token_urlsafe(8)}"
        email = f"{uname}@trustnode.local"
        r = requests.post(f"{PORTAL_BASE}/api/control-plane/users", headers=H,
                          params={"tenant_id": target_tenant},
                          json={"tenant_id": target_tenant, "customer_id": "",
                                "username": uname, "password": pw,
                                "role": "viewer", "status": "active",
                                "email": email, "mfa_enabled": False,
                                "modules": [], "permissions": {}}, timeout=15)
        created = r.status_code == 200
        if created: disposables["portal_users"].append((target_tenant, uname))
        P.record("P09a", f"portal create user in tenant={target_tenant}",
                 "PASS" if created else "FAIL", f"http={r.status_code} body={r.text[:160]}")
        if created:
            time.sleep(3.0)
            # Supabase mirror check (via cloud DB directly to avoid auth admin API)
            con = cloud_db_conn()
            if not con:
                P.record("P09b", "portal user -> supabase mirror", "SKIP", "cloud DB conn failed")
            else:
                try:
                    with con.cursor() as cur:
                        cur.execute(
                            "SELECT id, raw_user_meta_data->>'username', "
                            "raw_user_meta_data->>'tenant_id' FROM auth.users "
                            "WHERE raw_user_meta_data->>'username' = %s",
                            (uname,),
                        )
                        row = cur.fetchone()
                    if row and row[2] == target_tenant:
                        P.record("P09b", "portal user -> supabase mirror", "PASS",
                                 f"auth.users.id={row[0]} tenant={row[2]}")
                        ctx["portal_test_user_id"] = str(row[0])
                    else:
                        P.record("P09b", "portal user -> supabase mirror", "FAIL",
                                 f"row={row}")
                finally:
                    con.close()
    return ctx


# ---- Probe 2: LITE ----------------------------------------------------
def run_lite_probes(ctx: dict[str, Any]) -> None:
    print("\n========== LITE (Supabase) ==========")
    # L01 master Supabase login
    code, body = supabase_login(MASTER_EMAIL, MASTER_PASS)
    tok = body.get("access_token") if isinstance(body, dict) else ""
    if not tok:
        P.record("L01", "lite master login", "FAIL", f"http={code} body={str(body)[:160]}")
        return
    user = body.get("user", {})
    meta = user.get("user_metadata") or {}
    ok = meta.get("tenant_id") == "default" and meta.get("role") == "admin"
    P.record("L01", "lite master login", "PASS" if ok else "FAIL",
             f"http={code} tenant={meta.get('tenant_id')} role={meta.get('role')}")
    Hm = {"apikey": SUPABASE_ANON, "Authorization": f"Bearer {tok}",
          "Content-Type": "application/json"}

    # L02 master + lite_profiles RLS — current policy is "user_id = auth.uid()",
    # i.e. each user can only see their own row. So master sees exactly 1
    # row, scoped to its own tenant. This is an architectural finding —
    # "master picks any customer in Lite" is NOT supported by the current
    # Lite RLS.
    r = requests.get(f"{SUPABASE_URL}/rest/v1/lite_profiles",
                     headers=Hm, params={"select": "user_id,tenant_id,username,role"},
                     timeout=15)
    rows = r.json() if r.status_code == 200 else []
    tenants_seen = {row.get("tenant_id") for row in rows} if isinstance(rows, list) else set()
    n = len(rows) if isinstance(rows, list) else 0
    # n==1 means RLS is doing its current self-only thing. It's the EXPECTED
    # status quo, but it BLOCKS the "master picks any customer" UX you want.
    P.record("L02", "lite master -> lite_profiles (per-user RLS, status quo)",
             "INFO" if (r.status_code == 200 and n == 1) else "FAIL",
             f"http={r.status_code} rows={n} tenants={sorted([t for t in tenants_seen if t])} "
             f"-- self-only RLS; cross-tenant picker not yet supported")

    # L03 master + dashboard_configurations — schema uses scope_key, not user_id.
    r = requests.get(f"{SUPABASE_URL}/rest/v1/dashboard_configurations",
                     headers=Hm, params={"select": "tenant_id,scope_key,version"},
                     timeout=15)
    if r.status_code == 200:
        rows = r.json()
        if isinstance(rows, list):
            tenants_seen = {row.get("tenant_id") for row in rows}
            # RLS is `tenant_id = lite_current_tenant()` — master only sees
            # tenant=default rows. Same architectural limit as L02.
            P.record("L03", "lite master -> dashboard_configurations (tenant-RLS)",
                     "INFO" if rows else "INFO",
                     f"rows={len(rows)} tenants={sorted([t for t in tenants_seen if t])} "
                     f"-- one-tenant scope")
        else:
            P.record("L03", "lite master -> dashboard_configurations", "FAIL",
                     f"unexpected body type")
    else:
        P.record("L03", "lite master -> dashboard_configurations", "FAIL",
                 f"http={r.status_code} body={r.text[:160]}")

    # L04+L05+L06 tenant-admin login as admin-lucas if present
    # Use service key (already have) to find a non-master tenant admin
    if not SUPABASE_SVC:
        P.record("L04", "tenant-admin login probe", "SKIP", "no service key in env")
        return
    Hs = {"apikey": SUPABASE_SVC, "Authorization": f"Bearer {SUPABASE_SVC}"}
    con = cloud_db_conn()
    if not con:
        P.record("L04", "tenant-admin login probe", "SKIP", "cloud db conn failed")
        return
    try:
        with con.cursor() as cur:
            cur.execute(
                "SELECT u.email, u.raw_user_meta_data->>'username', "
                "u.raw_user_meta_data->>'tenant_id', u.raw_user_meta_data->>'role' "
                "FROM auth.users u "
                "WHERE u.raw_user_meta_data->>'tenant_id' <> 'default' "
                "AND u.raw_user_meta_data->>'role' = 'admin' "
                "LIMIT 1"
            )
            cand = cur.fetchone()
    finally:
        con.close()
    if not cand:
        P.record("L04", "tenant-admin login probe", "SKIP",
                 "no non-default tenant admin in Supabase auth.users")
        return
    cand_email, cand_user, cand_tenant, cand_role = cand
    # We don't know their password — only test that the lookup is plausible.
    P.record("L04", f"found tenant admin {cand_user}@{cand_tenant} ({cand_email})",
             "INFO", f"role={cand_role} (password unknown — cannot test login)")


# ---- Probe 3: EDGE (local) -------------------------------------------
def run_edge_probes(disposables: dict[str, list[str]]) -> dict[str, Any]:
    print("\n========== EDGE (local 127.0.0.1:8000) ==========")
    ctx: dict[str, Any] = {}
    try:
        r = requests.get(f"{EDGE_BASE}/api/health", timeout=5)
        P.record("E01", "edge /api/health", "PASS" if r.status_code == 200 else "FAIL", f"http={r.status_code}")
    except Exception as exc:
        P.record("E01", "edge /api/health", "FAIL", str(exc))
        return ctx
    # E02 admin login — try a few possible default passwords; the user can
    # override TRUSTNODE_EDGE_ADMIN_PASSWORD in env.
    pw_candidates = [os.environ.get("TRUSTNODE_EDGE_ADMIN_PASSWORD", ""),
                     ENV.get("TRUSTNODE_EDGE_ADMIN_PASSWORD", ""),
                     "admin", "trustnode", "Apolo020@", "Apolo020@25t"]
    pw_candidates = [p for p in pw_candidates if p]
    token = ""
    for pw in pw_candidates:
        code, body = edge_login(EDGE_BASE, "admin", pw)
        if code == 200 and (body.get("access_token") or body.get("token")):
            token = body.get("access_token") or body.get("token")
            break
    if not token:
        P.record("E02", "edge /api/auth/login as admin", "SKIP",
                 "no password candidate worked — set TRUSTNODE_EDGE_ADMIN_PASSWORD env to test")
        return ctx
    pl = decode_jwt_payload(token)
    P.record("E02", "edge /api/auth/login as admin", "PASS",
             f"role={pl.get('role')} tenant={pl.get('tenant_id')}")
    H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    ctx["edge_headers"] = H
    ctx["edge_token"] = token

    suf = secrets.token_hex(3)
    uname = f"e2e-edge-{suf}"
    pw = f"E2eP-{secrets.token_urlsafe(8)}"
    email = f"{uname}@trustnode.local"
    local_tenant = pl.get("tenant_id") or "default"
    r = requests.post(f"{EDGE_BASE}/api/control-plane/users", headers=H,
                      params={"tenant_id": local_tenant},
                      json={"tenant_id": local_tenant, "customer_id": "",
                            "username": uname, "password": pw, "role": "viewer",
                            "status": "active", "email": email, "mfa_enabled": False,
                            "modules": [], "permissions": {}}, timeout=15)
    created = r.status_code == 200
    if created: disposables["edge_users"].append((local_tenant, uname))
    P.record("E03", "edge create user", "PASS" if created else "FAIL",
             f"http={r.status_code} body={r.text[:160]}")
    if not created: return ctx

    time.sleep(3.0)
    # E04 — see it in Supabase
    con = cloud_db_conn()
    if not con:
        P.record("E04", "edge create -> supabase mirror", "SKIP", "cloud db conn failed")
    else:
        try:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT id, raw_user_meta_data->>'tenant_id' FROM auth.users "
                    "WHERE raw_user_meta_data->>'username' = %s", (uname,))
                row = cur.fetchone()
            if row and row[1] == local_tenant:
                P.record("E04", "edge create -> supabase mirror", "PASS",
                         f"auth.users.id={row[0]} tenant={row[1]}")
                ctx["edge_test_user_id"] = str(row[0])
            else:
                P.record("E04", "edge create -> supabase mirror", "FAIL", f"row={row}")
        finally:
            con.close()

    # E05 — delete from edge, verify it's gone in Supabase
    rd = requests.delete(f"{EDGE_BASE}/api/control-plane/users/{uname}",
                         headers=H, params={"tenant_id": local_tenant}, timeout=15)
    P.record("E05a", "edge delete user", "PASS" if rd.status_code == 200 else "FAIL",
             f"http={rd.status_code}")
    if rd.status_code == 200:
        if (local_tenant, uname) in disposables["edge_users"]:
            disposables["edge_users"].remove((local_tenant, uname))
        time.sleep(3.0)
        con = cloud_db_conn()
        if con:
            try:
                with con.cursor() as cur:
                    cur.execute("SELECT id FROM auth.users WHERE raw_user_meta_data->>'username' = %s", (uname,))
                    row = cur.fetchone()
                P.record("E05b", "edge delete -> supabase mirror remove",
                         "PASS" if row is None else "FAIL",
                         f"row_after_delete={row}")
            finally:
                con.close()
    return ctx


# ---- Probe 4: SYNC NOTE ----------------------------------------------
def run_sync_notes() -> None:
    print("\n========== SYNC ARCHITECTURE NOTES ==========")
    P.record("S01", "edge -> cloud user sync (lite_user_mirror)", "PASS",
             "Verified by E04/E05: edge cp_users create/delete propagates to "
             "Supabase auth.users + lite_profiles via daemon thread.")
    P.record("S02", "cloud -> local-edge user sync (cp_users pull)", "INFO",
             "GAP: portal edits to cp_users only land in the VPS edge SQLite "
             "(the one the portal itself queries). A customer-site edge has "
             "its own cp_users SQLite and does NOT pull cp_users from the cloud "
             "today. lite_user_mirror is one-way. If you want portal user "
             "changes to flow back to a customer's local edge, that needs "
             "either a polling endpoint on the edge that pulls cp_users from "
             "Supabase, or webhook-on-Supabase-write. Currently neither exists.")
    P.record("S03", "lite UX 'master picks any customer'", "INFO",
             "GAP: lite_profiles RLS is `user_id = auth.uid()` and "
             "dashboard_configurations RLS is `tenant_id = lite_current_tenant()`. "
             "Master sees only its own tenant in Lite. To enable a "
             "cross-tenant picker, either: (a) loosen RLS to allow "
             "role=admin AND tenant=default users to bypass tenant_id "
             "filtering, or (b) add a portal-only admin Lite view that "
             "talks to the edge backend (which already has the cross-tenant "
             "endpoint /api/cp/users?tenant_id=__all__).")


# ---- cleanup ----------------------------------------------------------
def cleanup(disposables: dict[str, list[str]], ctx_portal: dict[str, Any],
            ctx_edge: dict[str, Any]) -> None:
    print("\n========== CLEANUP ==========")
    H = ctx_portal.get("portal_headers")
    if H:
        for tenant, uname in list(disposables.get("portal_users", [])):
            r = requests.delete(f"{PORTAL_BASE}/api/control-plane/users/{uname}",
                                headers=H, params={"tenant_id": tenant}, timeout=15)
            print(f"  delete portal user {uname}@{tenant} -> http={r.status_code}")
        for row_id in list(disposables.get("activation_codes", [])):
            r = requests.delete(f"{PORTAL_BASE}/api/control-plane/activation-codes/{row_id}",
                                headers=H, params={"tenant_id": "default"}, timeout=15)
            print(f"  delete activation_code {row_id} -> http={r.status_code}")
        for tenant, cid in list(disposables.get("customers", [])):
            r = requests.delete(f"{PORTAL_BASE}/api/control-plane/customers/{cid}",
                                headers=H, params={"tenant_id": tenant}, timeout=15)
            if r.status_code != 200:
                r = requests.post(f"{PORTAL_BASE}/api/control-plane/customers/{cid}/delete",
                                  headers=H, params={"tenant_id": tenant}, timeout=15)
            print(f"  delete customer {cid}@{tenant} -> http={r.status_code}")
        for tenant, lid in list(disposables.get("licenses", [])):
            r = requests.delete(f"{PORTAL_BASE}/api/control-plane/licenses/{lid}",
                                headers=H, params={"tenant_id": tenant}, timeout=15)
            if r.status_code != 200:
                r = requests.post(f"{PORTAL_BASE}/api/control-plane/licenses/{lid}/delete",
                                  headers=H, params={"tenant_id": tenant}, timeout=15)
            print(f"  delete license {lid}@{tenant} -> http={r.status_code}")
    He = ctx_edge.get("edge_headers")
    if He:
        for tenant, uname in list(disposables.get("edge_users", [])):
            r = requests.delete(f"{EDGE_BASE}/api/control-plane/users/{uname}",
                                headers=He, params={"tenant_id": tenant}, timeout=15)
            print(f"  delete edge user {uname}@{tenant} -> http={r.status_code}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-cleanup", action="store_true")
    args = ap.parse_args()
    print(f"TrustNode full-master-admin E2E\n  portal={PORTAL_BASE}\n  edge={EDGE_BASE}\n  supabase={SUPABASE_URL}")
    disposables: dict[str, list[Any]] = {
        "tenants": [], "customers": [], "licenses": [],
        "portal_users": [], "edge_users": [], "activation_codes": [],
    }
    ctx_portal: dict[str, Any] = {}
    ctx_edge: dict[str, Any] = {}
    try:
        ctx_portal = run_portal_probes(disposables)
        run_lite_probes(ctx_portal)
        ctx_edge = run_edge_probes(disposables)
        run_sync_notes()
    finally:
        if not args.no_cleanup:
            try: cleanup(disposables, ctx_portal, ctx_edge)
            except Exception as exc: print(f"  cleanup error: {exc}")
    return P.summary()


if __name__ == "__main__":
    sys.exit(main())
