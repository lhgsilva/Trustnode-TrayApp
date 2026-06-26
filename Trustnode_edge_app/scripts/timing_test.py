"""Timing test 2026-06-19: measure collection cadence vs configured interval,
historian write lag, and chart update freshness for the currently logged-in user.

Reports the user/tenant scope, every gateway's configured vs observed interval,
last-write timestamps in both UTC and local clock, and end-to-end lag from PLC
poll to historian row to /live row.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("TN_TEST_BASE_URL", "http://127.0.0.1:8000")
USER = os.environ.get("TN_TEST_USER", "admin")
PASS = os.environ.get("TN_TEST_PASS", "admin")


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
        body = e.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(body)
        except Exception:
            pass
        return e.code, body, time.monotonic() - t0
    except Exception as e:
        return -1, str(e), time.monotonic() - t0


def parse_ts(s):
    if not s:
        return None
    raw = str(s).strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(raw.split("+", 1)[0], fmt)
            if dt.tzinfo is None:
                # Historian writes UTC strings without tz suffix
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


print("=" * 72)
print(f"COLLECTION / HISTORIAN / CHART TIMING TEST  @ {BASE}")
print(f"local now = {datetime.now().astimezone().isoformat()}")
print(f"utc now   = {datetime.now(timezone.utc).isoformat()}")
print("=" * 72)

# 1) Login (this is the user the dashboard uses)
code, body, ms = call("POST", "/api/auth/login", {"username": USER, "password": PASS})
if not isinstance(body, dict) or not body.get("token"):
    print(f"LOGIN FAILED: {body}")
    sys.exit(1)
token = body["token"]
user = body.get("user") or {}
print(f"\n[Login] {USER}  tenant_id={user.get('tenant_id')!r}  role={user.get('role')!r}  ({ms*1000:.0f}ms)")

# 2) Whoami / scope
code, who, ms = call("GET", "/api/auth/me", token=token)
if isinstance(who, dict):
    print(f"[Whoami] tenant_id={who.get('tenant_id')!r} customer_id={who.get('customer_id')!r} edge_id={who.get('edge_id')!r}")
else:
    print(f"[Whoami] not available: {who}")

# 3) Bootstrap — what gateways/databases is THIS user wired to?
code, boot, ms = call("GET", "/api/app-store/bootstrap", token=token)
data = boot.get("data", {}) if isinstance(boot, dict) else {}
gws = data.get("gateway_configurations") or []
dbs = data.get("database_configurations") or []
print(f"\n[Bootstrap] scope={boot.get('shared_scope_key')!r}  gateways={len(gws)}  databases={len(dbs)}  ({ms*1000:.0f}ms)")
for g in gws:
    print(f"   gw {g.get('id')}  {g.get('name')!r}  interval_ms={g.get('interval_ms')}  database_id={g.get('database_id')!r}")
for d in dbs:
    tag = "sqlite" if d.get("engine") == "sqlite" else d.get("engine")
    loc = d.get("sqlite_path") or f"{d.get('host')}:{d.get('port')}/{d.get('database')}"
    print(f"   db {d.get('id')}  {d.get('name')!r}  {tag}  -> {loc}")

# 4) Running status: which gateways are running RIGHT NOW from this user's view?
code, st, ms = call("GET", "/api/plc/gateways/status", token=token)
running = [s for s in (st or []) if isinstance(s, dict) and s.get("running")]
print(f"\n[Status] running gateways={len(running)} of {len(st or [])} reported  ({ms*1000:.0f}ms)")
for s in (st or []):
    if isinstance(s, dict):
        print(f"   gw {s.get('gateway_id')}  running={s.get('running')}  db_writes={s.get('db_write_count')}  last_write={s.get('db_last_write_utc')}  err={s.get('last_error')}")

if not running:
    print("\nNo gateways are running — start one in the UI then re-run this script.")
    sys.exit(0)

# Pick the first running gateway and sample its cadence
sample = running[0]
gw_id = sample["gateway_id"]
cfg = next((g for g in gws if g.get("id") == gw_id), None)
interval_cfg = int(cfg.get("interval_ms")) if cfg and cfg.get("interval_ms") else 1000
db_id = cfg.get("database_id") if cfg else None
db_cfg = next((d for d in dbs if d.get("id") == db_id), None)
print(f"\nSampling gateway {gw_id!r}: configured interval = {interval_cfg} ms, sink db = {db_id!r}")
if db_cfg:
    where = db_cfg.get("sqlite_path") or f"{db_cfg.get('host')}:{db_cfg.get('port')}/{db_cfg.get('database')}"
    print(f"  sink: {db_cfg.get('name')!r}  {db_cfg.get('engine')}  -> {where}")
else:
    print(f"  WARNING: bootstrap has no database_configurations matching gateway database_id={db_id!r}")

# 5) Observe collection cadence over 10 polls of /gateways/status
print("\n[Cadence] 10 status polls @ 700ms each, watching db_write_count + db_last_write_utc...")
samples = []
for i in range(10):
    code, st, ms = call("GET", "/api/plc/gateways/status", token=token)
    s = next((x for x in (st or []) if x.get("gateway_id") == gw_id), None)
    now_local = datetime.now().astimezone()
    if s:
        wc = s.get("db_write_count")
        last_w = s.get("db_last_write_utc")
        last_dt = parse_ts(last_w)
        lag_ms = (now_local - last_dt).total_seconds() * 1000 if last_dt else None
        samples.append({"i": i, "wc": wc, "last_w": last_w, "lag_ms": lag_ms, "local_now": now_local})
        lag_s = f"{lag_ms:6.0f}ms" if lag_ms is not None else "  n/a"
        print(f"  {i:2d}  local={now_local.strftime('%H:%M:%S.%f')[:-3]}  db_writes={wc}  last_write_utc={last_w}  lag(now-write)={lag_s}")
    time.sleep(0.7)

# 6) Compute observed cadence between consecutive writes
print("\n[Cadence] inter-write intervals (last_write_utc deltas across samples):")
prev = None
deltas = []
for s in samples:
    dt = parse_ts(s["last_w"])
    if dt and prev and dt != prev:
        delta = (dt - prev).total_seconds() * 1000
        deltas.append(delta)
        marker = "OK " if abs(delta - interval_cfg) <= interval_cfg * 0.3 else "DRIFT"
        print(f"   delta {delta:7.0f}ms   target={interval_cfg}ms   [{marker}]")
    if dt:
        prev = dt
if deltas:
    avg = sum(deltas) / len(deltas)
    print(f"\n   avg observed inter-write = {avg:.0f} ms   (target {interval_cfg} ms)")
    if avg > interval_cfg * 1.6:
        print(f"   *** COLLECTION IS SLOWER than configured interval — diagnose driver read time ***")
    elif avg < interval_cfg * 0.4:
        print(f"   *** COLLECTION IS FASTER than configured interval — check config interpretation ***")
    else:
        print(f"   collection cadence is within tolerance of configured interval")

# 7) Historian range vs live: does the chart endpoint see the same writes?
print("\n[Historian] fetching latest 5 rows from /historian/range...")
code, h, ms = call("GET", f"/api/app-store/historian/range?limit=5&gateway={gw_id}", token=token)
rows = h.get("rows") if isinstance(h, dict) else h
if rows:
    print(f"   {len(rows)} rows ({ms*1000:.0f}ms)")
    now_utc = datetime.now(timezone.utc)
    for r in rows[:5]:
        ts = r.get("ts")
        dt = parse_ts(ts)
        lag_ms = (now_utc - dt).total_seconds() * 1000 if dt else None
        lag_s = f"{lag_ms:7.0f}ms" if lag_ms is not None else "    n/a"
        tag = r.get("tag") or r.get("tag_name") or "?"
        val = r.get("value")
        print(f"   ts={ts}  tag={tag:15} val={val}  age={lag_s}")
else:
    print(f"   no rows returned: {h}")

print("\n[Live] /api/app-store/live (the dashboard's live tile + footer):")
code, lv, ms = call("GET", "/api/app-store/live?limit=200", token=token)
rows = lv.get("rows", []) if isinstance(lv, dict) else (lv if isinstance(lv, list) else [])
mine = [r for r in rows if r.get("gateway_id") == gw_id]
print(f"   {len(rows)} total rows, {len(mine)} for {gw_id}  ({ms*1000:.0f}ms)")
now_utc = datetime.now(timezone.utc)
for r in mine[:5]:
    ts = r.get("ts") or r.get("server_ts")
    dt = parse_ts(ts) if ts else None
    age_ms = (now_utc - dt).total_seconds() * 1000 if dt else None
    age_s = f"{age_ms:7.0f}ms" if age_ms is not None else "    n/a"
    tag = r.get("tag") or r.get("tag_name") or "?"
    print(f"   tag={tag:15} val={r.get('value')}  ts={ts}  age={age_s}")

# 8) Tenant scope sanity — is bootstrap returning data under the JWT tenant_id?
print("\n[Tenant sanity]")
print(f"   JWT tenant_id = {user.get('tenant_id')!r}")
print(f"   bootstrap scope = {boot.get('shared_scope_key')!r}")
if user.get("tenant_id") and boot.get("shared_scope_key"):
    if str(boot.get("shared_scope_key")).split("|")[0] != user.get("tenant_id"):
        print(f"   *** MISMATCH: scope first segment != JWT tenant — relying on cross-tenant fallback ***")
    else:
        print(f"   tenant segment of scope matches JWT tenant — no fallback needed")

print("\n" + "=" * 72)
print("TIMING TEST COMPLETE")
print("=" * 72)
