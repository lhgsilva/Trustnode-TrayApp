"""Verify the auto-resume code path against the customer's DB.

Doesn't start a PLC — just checks that:
  1. The migration adds the last_running column to gateway_runtime_state
  2. mark_gateway_running persists the flag
  3. list_running_gateways returns the right ids
  4. The bootstrap-scoped lookup finds the new gateway config
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

os.environ["TRUSTNODE_APP_STORE_PATH"] = r"C:\Users\User\.trustnode_edge\data\trustnode_app_store.db"
os.environ["TRUSTNODE_DATA_DIR"] = r"C:\Users\User\.trustnode_edge\data"

from app.services.app_store import AppStore
from app.services.telemetry_service import TelemetryService

print("=" * 72)
print("AUTO-RESUME VERIFICATION")
print("=" * 72)

store = AppStore()
tel = TelemetryService()

print("\n[1] Migration check: gateway_runtime_state columns")
import sqlite3
P = r"C:\Users\User\.trustnode_edge\data\trustnode_telemetry.db"
con = sqlite3.connect(P, timeout=10.0)
cols = [r[1] for r in con.execute("PRAGMA table_info(gateway_runtime_state)").fetchall()]
con.close()
print(f"   columns: {cols}")
if "last_running" in cols:
    print("   [PASS] last_running column present")
else:
    print("   [FAIL] last_running column missing")

print("\n[2] mark_gateway_running(gw-1781903248499, True)")
tel.mark_gateway_running("gw-1781903248499", True)
ids = tel.list_running_gateways()
print(f"   list_running_gateways() -> {ids}")
if "gw-1781903248499" in ids:
    print("   [PASS] flag persisted")
else:
    print("   [FAIL] flag NOT persisted")

print("\n[3] mark_gateway_running(gw-1781903248499, False)")
tel.mark_gateway_running("gw-1781903248499", False)
ids = tel.list_running_gateways()
print(f"   list_running_gateways() -> {ids}")
if "gw-1781903248499" not in ids:
    print("   [PASS] flag cleared")
else:
    print("   [FAIL] flag still set")

print("\n[4] Re-set flag and look up gateway config in SCOPED bootstrap")
tel.mark_gateway_running("gw-1781903248499", True)

# Reproduce what _deferred_startup does
bootstrap = store.get_bootstrap(prefer_cloud_reads=False) or {}
app_settings = bootstrap.get("app_settings") if isinstance(bootstrap.get("app_settings"), dict) else {}
tenant_seg = str(app_settings.get("tenant_id") or "").strip().lower() or "default"
customer_seg = str(app_settings.get("customer_id") or "").strip().lower() or "-"
edge_seg = str(app_settings.get("edge_id") or "").strip().lower()
print(f"   app_settings: tenant={tenant_seg!r} customer={customer_seg!r} edge={edge_seg!r}")
if edge_seg:
    scope_key = f"{tenant_seg}|{customer_seg}|{edge_seg}"
    print(f"   scope_key = {scope_key!r}")
    scoped = store.get_bootstrap_scoped(scope_key, False)
    gw_rows = scoped.get("gateway_configurations") or []
    db_rows = scoped.get("database_configurations") or []
    print(f"   scoped gateways: {len(gw_rows)}  databases: {len(db_rows)}")
    target = next((g for g in gw_rows if g.get("id") == "gw-1781903248499"), None)
    if target:
        print(f"   [PASS] found gateway: name={target.get('name')!r} ip={target.get('plc_ip')} db_id={target.get('database_id')!r}")
    else:
        print(f"   [FAIL] gateway gw-1781903248499 not in scoped config")
        # Try cross-tenant fallback to default — that's what scoped lookup does internally
        print("   gateways in scoped bootstrap:")
        for g in gw_rows:
            print(f"     - {g.get('id')} {g.get('name')!r}")
else:
    print("   [SKIP] no edge_id in app_settings")

print("\n[5] Cleanup: mark stopped so the next real boot doesn't auto-resume")
tel.mark_gateway_running("gw-1781903248499", False)
print(f"   final list_running_gateways(): {tel.list_running_gateways()}")

print()
print("=" * 72)
print("VERIFICATION DONE")
print("=" * 72)
