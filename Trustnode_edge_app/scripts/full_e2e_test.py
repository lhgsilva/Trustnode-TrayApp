"""Full end-to-end test for customer-reported issues 2026-06-18.

Run after launching backend on port 8010 against customer DB copy.
Verifies all four areas the customer reported broken:
  - device test connection (PLC1, PLC2, Meter)
  - bootstrap returns gateways + databases
  - gateway start + UI status correctness
  - collection writes historian
  - charts (historian/range), live rows (footer) work
  - instant stop
"""
import json
import sys
import time
import urllib.error
import urllib.request

import os
BASE = os.environ.get("TN_TEST_BASE_URL", "http://127.0.0.1:8010")


def call(method, path, body=None, token=None, timeout=120):
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


def mark(ok):
    return "OK  " if ok else "FAIL"


print("=" * 70)
print("COMPREHENSIVE END-TO-END TEST")
print("=" * 70)

code, body, ms = call("POST", "/api/auth/login", {"username": "admin", "password": "admin"})
token = body["token"]
print(f"[Login] OK [{ms*1000:.0f}ms]")

print()
print("--- AREA: Device test connection ---")
for desc, gw_type, ip, opc_url, timeout_ms in [
    ("PLC1 Allen-Bradley", "allen_bradley", "192.168.10.240", "", 2500),
    ("PLC2 Siemens OPC-UA", "siemens_opcua", "192.168.10.242", "opc.tcp://192.168.10.242:4840", 20000),
]:
    code, body, ms = call(
        "POST",
        "/api/plc/test-connection",
        {
            "gateway_type": gw_type,
            "plc_ip": ip,
            "opc_url": opc_url,
            "opc_node_id": "",
            "opc_node_ids": [],
            "timeout_ms": timeout_ms,
        },
        token=token,
        timeout=30,
    )
    ok = isinstance(body, dict) and body.get("ok")
    extras = ""
    if isinstance(body, dict):
        extras = f" ping={body.get('ping_ok')} port={body.get('port_ok')} opc={body.get('opc_session_ok')}"
    print(f"  [{mark(ok)}] {desc:25} [{ms*1000:5.0f}ms]{extras}")

# Meter test
code, body, ms = call(
    "POST",
    "/api/power/test-connection",
    {
        "device": {
            "id": "M2",
            "name": "Meter 2",
            "enabled": True,
            "type": "modbus_tcp",
            "protocol": "modbus_tcp",
            "ip": "192.168.10.117",
            "port": 502,
            "unit_id": 1,
            "poll_interval_ms": 1000,
            "electrical_mode": "single_phase",
            "register_profile": "weidmuller_em525_single_phase",
            "use_custom_registers": False,
            "wiring_type": "single_phase",
            "voltage_connected": True,
            "ct_connected": True,
            "ct_primary": 80.0,
            "ct_secondary": 5.0,
            "vt_primary": 230.0,
            "vt_secondary": 230.0,
            "registers": {"voltage_v": 19000, "current_a": 19012, "active_power_w": 19020, "power_factor": 19044, "frequency_hz": 19050, "energy_wh": 19054},
            "register_scales": {"voltage_v": 1, "current_a": 1, "active_power_w": 1, "power_factor": 1, "frequency_hz": 1, "energy_wh": 1},
        },
        "timeout_ms": 4000,
    },
    token=token,
    timeout=30,
)
ok = isinstance(body, dict) and body.get("ok")
print(f"  [{mark(ok)}] {'Meter 2 modbus_tcp':25} [{ms*1000:5.0f}ms]")

print()
print("--- AREA: Database / config persistence ---")
code, body, ms = call("GET", "/api/app-store/bootstrap", token=token)
data = body.get("data", {})
gws = data.get("gateway_configurations") or []
dbs = data.get("database_configurations") or []
print(f"  [{mark(len(gws) >= 2 and len(dbs) >= 1)}] Bootstrap [{ms*1000:.0f}ms]")
print(f"         scope={body.get('shared_scope_key')}")
print(f"         gateways={len(gws)}")
for g in gws:
    print(f"           - {g.get('name')} ({g.get('gateway_type')} @ {g.get('plc_ip')}) database_id={g.get('database_id')!r}")
print(f"         databases={len(dbs)}")
for d in dbs:
    print(f"           - {d.get('name')} ({d.get('engine')}) id={d.get('id')!r}")

print()
print("--- AREA: Gateway start + UI status ---")
plc1 = next((g for g in gws if g.get("id") == "gw-1779098315351"), None)
assert plc1 is not None
sink_id = plc1["database_id"]
sink_cfg = next((d for d in dbs if d.get("id") == sink_id), None)
assert sink_cfg is not None, f"database_id {sink_id!r} not found"


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


primary = to_sink(sink_cfg)
code, body, ms = call(
    "POST",
    "/api/plc/gateways/start",
    {
        "gateway_id": "gw-1779098315351",
        "config": {
            "gateway_type": plc1["gateway_type"],
            "plc_ip": plc1["plc_ip"],
            "opc_url": plc1.get("opc_url") or "",
            "tags": plc1.get("tags") or [],
            "collection_trigger_mode": "any",
            "collection_triggers": [],
            "interval_ms": int(plc1.get("interval_ms") or 1000),
            "equipment": "",
            "site": "",
            "area": "",
        },
        "db_sink": primary,
        "db_sinks": [primary],
    },
    token=token,
    timeout=30,
)
ok = isinstance(body, dict) and body.get("started")
msg = body.get("message", "")[:80] if isinstance(body, dict) else ""
print(f"  [{mark(ok)}] Start PLC 1 [{ms*1000:.0f}ms]: {msg}")

time.sleep(2)
code, body, ms = call("GET", "/api/plc/gateways/status", token=token)
plc1_st = next((s for s in (body or []) if s.get("gateway_id") == "gw-1779098315351"), None)
running = plc1_st and plc1_st.get("running")
wc = plc1_st.get("db_write_count") if plc1_st else "n/a"
print(f"  [{mark(running)}] /gateways/status [{ms*1000:.0f}ms]: PLC 1 running={running}, db_write_count={wc}")

print()
print("--- AREA: Collection + historian + chart endpoints ---")
print("  Waiting 6s for collection...")
time.sleep(6)

code, body, ms = call("GET", "/api/app-store/historian/range?limit=20&gateway=gw-1779098315351&tag=SimREAL%5B3%5D", token=token)
rows = body.get("rows") if isinstance(body, dict) else body
print(f"  [{mark(rows)}] /historian/range [{ms*1000:.0f}ms]: {len(rows or [])} rows")
if rows:
    latest = rows[0]
    print(f"         Latest: {latest.get('ts')} = {latest.get('value'):.2f} ({latest.get('quality_label')})")

code, body, ms = call("GET", "/api/app-store/live?limit=100", token=token)
live = body.get("rows", []) if isinstance(body, dict) else (body if isinstance(body, list) else [])
plc1_live = [r for r in live if r.get("gateway_id") == "gw-1779098315351"]
print(f"  [{mark(plc1_live)}] /live [{ms*1000:.0f}ms]: PLC 1 live tags={len(plc1_live)}")
for r in plc1_live[:3]:
    print(f"         {(r.get('tag') or r.get('tag_name', '')):15} = {r.get('value')}")

# Final status check
code, body, ms = call("GET", "/api/plc/gateways/status", token=token)
plc1_st = next((s for s in (body or []) if s.get("gateway_id") == "gw-1779098315351"), None)
if plc1_st:
    print(f"  [OK  ] Final status: db_write_count={plc1_st.get('db_write_count')}, "
          f"last_check={(plc1_st.get('last_check_utc') or '-')[:19]}, error={plc1_st.get('last_error')}")

print()
print("--- AREA: Instant stop ---")
code, body, ms = call("POST", "/api/plc/gateways/stop", {"gateway_id": "gw-1779098315351"}, token=token)
ok = isinstance(body, dict) and body.get("stopped")
print(f"  [{mark(ok)}] Stop PLC 1 [{ms*1000:.0f}ms]")

print()
print("--- AREA: Scanner ---")
code, body, ms = call(
    "POST",
    "/api/plc/discover-network",
    {
        "gateway_type": "allen_bradley",
        "timeout_ms": 4000,
        "include_tcp_probe": True,
        "scan_any_tcp": False,  # gateway-type-specific
        "scan_range": "192.168.10.0/24",
    },
    token=token,
    timeout=120,
)
devices = body.get("devices", []) if isinstance(body, dict) else []
plant_devices = [d for d in devices if str(d.get("ip", "")).startswith("192.168.10.")]
print(f"  [{mark(plant_devices)}] /discover-network [{ms*1000:.0f}ms]: {len(devices)} total, {len(plant_devices)} on plant subnet")
for d in plant_devices[:5]:
    print(f"         {d.get('ip'):18} {d.get('device_type'):15} {d.get('product_name', '')}")

print()
print("=" * 70)
print("TEST COMPLETE")
print("=" * 70)
