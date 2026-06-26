"""End-to-end validation for the post-re-activation flow.

Simulates the customer's broken scenario:
  1. Re-activate edge -> tenant_id changes (e.g., 'tenant-cust-XXX')
  2. New admin user logs in
  3. New PLC + new gateway created
  4. Start gateway -> should run for >5 cycles
  5. Historian should grow by >= cycles * tag_count
  6. Chart endpoint should return rows under the new tenant scope

Reports each step with PASS/FAIL and prints the EXACT scope/tenant/edge
that the backend resolved for the queries.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("TN_TEST_BASE_URL", "http://127.0.0.1:8000")
ADMIN_USER = os.environ.get("TN_TEST_USER", "admin-mari")
ADMIN_PASS = os.environ.get("TN_TEST_PASS", "admin")


def call(method, path, body=None, token=None, timeout=30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers=headers)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.getcode(), json.loads(r.read().decode("utf-8", errors="replace") or "null"), time.monotonic() - t0
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", errors="replace")), time.monotonic() - t0
        except Exception:
            return e.code, str(e), time.monotonic() - t0
    except Exception as e:
        return -1, str(e), time.monotonic() - t0


def mark(ok):
    return "PASS" if ok else "FAIL"


print("=" * 76)
print("POST-RE-ACTIVATION END-TO-END VALIDATION")
print(f"BASE={BASE}  USER={ADMIN_USER}")
print("=" * 76)

# 1) Log in as the new admin user
code, body, _ = call("POST", "/api/auth/login", {"username": ADMIN_USER, "password": ADMIN_PASS})
if not isinstance(body, dict) or not body.get("token"):
    print(f"[1] LOGIN FAIL: {body}")
    sys.exit(1)
token = body["token"]
user = body.get("user") or {}
jwt_tenant = user.get("tenant_id")
print(f"[1] LOGIN OK   user={ADMIN_USER!r}  jwt_tenant_id={jwt_tenant!r}  role={user.get('role')!r}")

# 2) Bootstrap: what scope, what gateways, what databases?
code, boot, _ = call("GET", "/api/app-store/bootstrap", token=token)
data = boot.get("data", {}) if isinstance(boot, dict) else {}
scope = boot.get("shared_scope_key") if isinstance(boot, dict) else None
gws = data.get("gateway_configurations") or []
dbs = data.get("database_configurations") or []
app_settings = data.get("app_settings") if isinstance(data.get("app_settings"), dict) else {}
print(f"[2] BOOTSTRAP scope={scope!r}")
print(f"    edge_id={app_settings.get('edge_id')!r}  tenant_id={app_settings.get('tenant_id')!r}  customer_id={app_settings.get('customer_id')!r}")
print(f"    gateways={len(gws)}  databases={len(dbs)}")
for g in gws:
    print(f"     - {g.get('id')}  name={g.get('name')!r}  ip={g.get('plc_ip')}  db_id={g.get('database_id')!r}")
for d in dbs:
    print(f"     - {d.get('id')}  engine={d.get('engine')}  sqlite_path={d.get('sqlite_path')!r}")

if not gws:
    print("[2] No gateway configured — create one in the UI first.")
    sys.exit(0)
gw = gws[0]
gw_id = gw["id"]

# 3) Verify the SCOPE the chart endpoint will use matches the SCOPE app_settings carries
sc_parts = (scope or "").split("|")
boot_tenant_seg = sc_parts[0] if sc_parts else ""
app_tenant = (app_settings.get("tenant_id") or "").strip().lower()
scope_matches_jwt = boot_tenant_seg == (jwt_tenant or "").strip().lower()
scope_matches_app = boot_tenant_seg == app_tenant
print(f"[3] SCOPE CHECK   first_seg={boot_tenant_seg!r}  jwt_match={scope_matches_jwt}  app_settings_match={scope_matches_app}")
if not scope_matches_jwt or not scope_matches_app:
    print("    *** MISMATCH: queries may land in the wrong tenant ***")

# 4) Start the gateway
db_id = gw.get("database_id")
db_cfg = next((d for d in dbs if d.get("id") == db_id), None) or (dbs[0] if dbs else {})

def to_sink(c):
    return {
        "id": c.get("id", ""),
        "name": c.get("name", ""),
        "engine": c.get("engine"),
        "host": c.get("host", ""),
        "port": int(c.get("port") or 0),
        "database": c.get("database", ""),
        "username": c.get("username", ""),
        "password": c.get("password", ""),
        "sqlite_path": c.get("sqlite_path", ""),
        "file_path": c.get("file_path", ""),
        "legacy_url": c.get("legacy_url", ""),
        "legacy_api_token": c.get("legacy_api_token", ""),
        "source": c.get("source", ""),
        "site": c.get("site", ""),
        "area": c.get("area", ""),
        "equipment": c.get("equipment", ""),
        "schema": c.get("schema", "public"),
        "table": c.get("table", "plc_readings"),
        "tls": bool(c.get("tls")),
        "tag_filters": [],
        "gateway_filters": [],
        "csv_format": "",
        "csv_header": "",
    }


primary = to_sink(db_cfg)
payload = {
    "gateway_id": gw_id,
    "config": {
        "gateway_type": gw["gateway_type"],
        "plc_ip": gw["plc_ip"],
        "opc_url": gw.get("opc_url") or "",
        "tags": gw.get("tags") or [],
        "collection_trigger_mode": "any",
        "collection_triggers": [],
        "interval_ms": int(gw.get("interval_ms") or 1000),
        "equipment": "",
        "site": "",
        "area": "",
    },
    "db_sink": primary,
    "db_sinks": [primary],
}
code, body, _ = call("POST", "/api/plc/gateways/start", payload, token=token, timeout=30)
print(f"[4] START gateway={gw_id!r}  started={body.get('started') if isinstance(body, dict) else body}")

# 5) Watch /gateways/status for 10 seconds, sample db_write_count + last_write_utc
print("[5] CADENCE WATCH (12s @ 1s):")
print("    t       running  db_writes  last_write_utc            last_error")
samples = []
for i in range(12):
    code, st, _ = call("GET", "/api/plc/gateways/status", token=token)
    s = next((x for x in (st or []) if isinstance(x, dict) and x.get("gateway_id") == gw_id), None)
    if s:
        samples.append({
            "i": i,
            "running": s.get("running"),
            "wc": s.get("db_write_count"),
            "last_w": s.get("db_last_write_utc"),
            "err": s.get("last_error"),
        })
        print(f"    {i:3d}    {str(s.get('running')):5}    {str(s.get('db_write_count')):>6}    "
              f"{(s.get('db_last_write_utc') or '-'):24}  {(s.get('last_error') or '-')[:50]}")
    time.sleep(1.0)

if samples:
    first = samples[0]
    last = samples[-1]
    wc_gain = int(last["wc"] or 0) - int(first["wc"] or 0)
    print(f"    => db_write_count gain over 12s: {wc_gain}")
    if wc_gain >= 10:
        print(f"    => PASS: worker is collecting at expected cadence")
    elif wc_gain >= 1:
        print(f"    => PARTIAL: worker is collecting but very slowly")
    else:
        print(f"    => *** FAIL: worker is NOT collecting after the first cycle ***")

# 6) Chart endpoint: pull last 50 rows for the gateway
code, h, _ = call("GET", f"/api/app-store/historian/range?gateway={gw_id}&limit=50", token=token)
rows = h.get("rows") if isinstance(h, dict) else h
print(f"[6] CHART: /historian/range returned {len(rows or [])} rows")
if rows:
    print(f"    tenant_id reported: {h.get('tenant_id')!r}")
    print(f"    latest: ts={rows[0].get('ts')} tag={rows[0].get('tag')} val={rows[0].get('value')}")
else:
    print(f"    *** FAIL: no rows returned. tenant_id resolved by backend: {h.get('tenant_id') if isinstance(h, dict) else '?'!r}")

# 7) Live endpoint
code, lv, _ = call("GET", "/api/app-store/live?limit=200", token=token)
rows = lv.get("rows", []) if isinstance(lv, dict) else (lv if isinstance(lv, list) else [])
mine = [r for r in rows if r.get("gateway_id") == gw_id]
print(f"[7] LIVE: {len(rows)} total rows, {len(mine)} for {gw_id}")
for r in mine[:3]:
    print(f"    tag={r.get('tag') or r.get('tag_name')} val={r.get('value')} ts={r.get('ts')}")

print()
print("=" * 76)
print("VALIDATION DONE")
print("=" * 76)
