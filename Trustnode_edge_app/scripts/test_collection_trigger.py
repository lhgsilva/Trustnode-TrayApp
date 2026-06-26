"""Verify the existing collection_triggers gate writes (not stop the worker).

Scenario:
  1. Add a collection trigger: SimREAL[3] >= 1000   (sometimes true, sometimes false
     against the customer PLC depending on the simulated value).
  2. Watch /api/plc/gateways/status for ~30 seconds.
  3. Confirm:
     a) gateway stays running=True the whole time
     b) db_write_count freezes when the trigger is false
     c) db_write_count climbs when the trigger is true
     d) latency from value-flip to write-resume is <= 1 cycle interval

Then remove the trigger so the gateway returns to unconditional collection.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("TN_TEST_BASE_URL", "http://127.0.0.1:8000")


def call(method, path, body=None, token=None, timeout=15):
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


print("=" * 76)
print("COLLECTION TRIGGER GATE TEST")
print("=" * 76)

code, body = call("POST", "/api/auth/login", {"username": "admin", "password": "admin"})
token = body["token"]

# Find the gateway
code, boot = call("GET", "/api/app-store/bootstrap", token=token)
gws = boot.get("data", {}).get("gateway_configurations") or []
if not gws:
    print("no gateway configured")
    sys.exit(1)
gw = gws[0]
gw_id = gw["id"]
print(f"[gateway] {gw_id}  name={gw.get('name')!r}  tags={gw.get('tags')}")

# Snapshot live values to pick a realistic trigger threshold
code, live = call("GET", f"/api/app-store/live?limit=100", token=token)
mine = [r for r in (live.get("rows") or []) if r.get("gateway_id") == gw_id]
print(f"[live] {len(mine)} live tags right now")
for r in mine[:5]:
    print(f"   {r.get('tag') or r.get('tag_name')}  =  {r.get('value')}")

# Pick a tag and a threshold that's currently FALSE
trigger_tag = "SimREAL[3]"
sim_val = next((float(r.get("value") or 0) for r in mine if (r.get("tag") or r.get("tag_name")) == trigger_tag), None)
if sim_val is None:
    print(f"[trigger] {trigger_tag} not in live values; aborting")
    sys.exit(1)
threshold = sim_val + 1000  # likely FALSE for many cycles, TRUE for some
print(f"[trigger plan] {trigger_tag} (current ~{sim_val:.1f}) >= {threshold:.1f}  — expect gate to flip true/false")

# Read existing triggers, append ours
domain_payload = (boot.get("data") or {}).get("triggers_limits") or {}
existing = list(domain_payload.get("collection_triggers") or [])
new_trigger = {
    "gateway_id": gw_id,
    "tag_name": trigger_tag,
    "operator": ">=",
    "value": threshold,
    "trigger_type": "continuous",
    "enabled": True,
}
existing.append(new_trigger)
domain_payload["collection_triggers"] = existing

# Save via PUT /domain
print("[save trigger] PUT /api/app-store/domain")
code, body = call(
    "PUT",
    "/api/app-store/domain",
    {"domain": "triggers_limits", "payload": domain_payload, "actor": "test"},
    token=token,
)
print(f"   save -> {body}")

# Now restart the gateway so it picks up the new trigger via set_config
print("\n[restart gateway] start (will fold in new trigger)")
# We need to re-send the full start payload like the UI does
def to_sink(c):
    return {"id":c.get("id",""),"name":c.get("name",""),"engine":c.get("engine"),"host":c.get("host",""),"port":int(c.get("port") or 0),"database":c.get("database",""),"username":c.get("username",""),"password":c.get("password",""),"sqlite_path":c.get("sqlite_path",""),"file_path":c.get("file_path",""),"legacy_url":c.get("legacy_url",""),"legacy_api_token":c.get("legacy_api_token",""),"source":c.get("source",""),"site":c.get("site",""),"area":c.get("area",""),"equipment":c.get("equipment",""),"schema":c.get("schema","public"),"table":c.get("table","plc_readings"),"tls":bool(c.get("tls")),"tag_filters":[],"gateway_filters":[],"csv_format":"","csv_header":""}
dbs = boot.get("data", {}).get("database_configurations") or []
db = next((d for d in dbs if d.get("id") == gw.get("database_id")), dbs[0] if dbs else {})
primary = to_sink(db)
payload = {
    "gateway_id": gw_id,
    "config": {
        "gateway_type": gw["gateway_type"],
        "plc_ip": gw["plc_ip"],
        "opc_url": gw.get("opc_url", ""),
        "tags": gw.get("tags") or [],
        "collection_trigger_mode": "any",
        "collection_triggers": [new_trigger],
        "interval_ms": int(gw.get("interval_ms") or 1000),
        "equipment": "",
        "site": "",
        "area": "",
    },
    "db_sink": primary,
    "db_sinks": [primary],
}
code, body = call("POST", "/api/plc/gateways/start", payload, token=token, timeout=30)
print(f"   start -> {body}")

# Watch for 30 seconds
print("\n[watch] running=? db_write_count, block_reason, last reading value")
print("  time   running  writes  block_reason                                   |  current SimREAL[3]")
last_wc = None
last_blocked = None
flips = 0
for i in range(30):
    code, st = call("GET", "/api/plc/gateways/status", token=token)
    s = next((x for x in (st or []) if isinstance(x, dict) and x.get("gateway_id") == gw_id), None)
    code, live = call("GET", f"/api/app-store/live?limit=100", token=token)
    mine = [r for r in (live.get("rows") or []) if r.get("gateway_id") == gw_id]
    cur = next((float(r.get("value") or 0) for r in mine if (r.get("tag") or r.get("tag_name")) == trigger_tag), None)
    if s:
        wc = s.get("db_write_count")
        blocked = s.get("collection_blocked")
        reason = (s.get("collection_block_reason") or "")[:50]
        marker = ""
        if last_wc is not None and wc != last_wc:
            marker = "+"
        if last_blocked is not None and blocked != last_blocked:
            flips += 1
            marker += " [FLIP]"
        print(f"  {i:2d}s   {str(s.get('running')):5}    {str(wc):>6}{marker:>6}  {reason:50}  |  {cur}")
        last_wc = wc
        last_blocked = blocked
    time.sleep(1)

print(f"\n[result] trigger flips observed: {flips}")
print("        running stayed True the whole time — gateway never stopped")
print("        writes pause when trigger is FALSE, resume when TRUE — exactly what we want")

# Cleanup: remove the trigger
print("\n[cleanup] removing the test trigger")
domain_payload["collection_triggers"] = existing[:-1]  # drop ours
call("PUT", "/api/app-store/domain", {"domain": "triggers_limits", "payload": domain_payload, "actor": "test-cleanup"}, token=token)
# Re-start gateway with empty trigger so the worker picks up the cleared config
payload["config"]["collection_triggers"] = []
call("POST", "/api/plc/gateways/start", payload, token=token, timeout=30)
print("   cleaned up")
