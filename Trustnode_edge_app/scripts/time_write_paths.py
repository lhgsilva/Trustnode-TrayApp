"""Measure latency of each write path the gateway worker uses,
on the LIVE customer app_store.db. Reveals which step burns 13 seconds
per cycle.
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

# Force the live customer DB path so we measure the real bottleneck
os.environ["TRUSTNODE_APP_STORE_DB"] = r"C:\Users\User\.trustnode_edge\data\trustnode_app_store.db"
os.environ["TRUSTNODE_TELEMETRY_DB"] = r"C:\Users\User\.trustnode_edge\data\trustnode_telemetry.db"

from datetime import datetime, timezone

from app.services.app_store import AppStore
from app.services.telemetry_service import TelemetryService
from app.models import GatewayConfig, GatewayReading

print("=" * 72)
print("WRITE PATH LATENCY MEASUREMENT")
print("=" * 72)

print("\n[Init AppStore]")
t0 = time.monotonic()
store = AppStore()
print(f"   AppStore init: {(time.monotonic()-t0)*1000:.0f}ms")

print("\n[Init TelemetryService]")
t0 = time.monotonic()
tel = TelemetryService()
print(f"   TelemetryService init: {(time.monotonic()-t0)*1000:.0f}ms")

ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
readings = [
    GatewayReading(
        ts_utc=ts,
        tag_name=f"BenchTag{i}",
        value=float(i),
        quality=192,
        quality_label="GOOD",
        source="allen_bradley",
        site="bench",
        area="bench",
        equipment="bench",
    )
    for i in range(20)
]
cfg = GatewayConfig(
    gateway_type="allen_bradley",
    plc_ip="192.168.10.240",
    interval_ms=1000,
    tags=[r.tag_name for r in readings],
    site="bench",
    area="bench",
    equipment="bench",
)

print("\n[Cycle measurements] 5 successive runs of the live worker's persist path:")
for i in range(5):
    # 1) Telemetry record_collection_cycle (local persist - mandatory)
    t0 = time.monotonic()
    ok, err, edge_record_id = tel.record_collection_cycle(
        gateway_id="gw-bench",
        config=cfg,
        readings=readings,
        collection_status="ok",
    )
    tel_ms = (time.monotonic() - t0) * 1000

    # 2) AppStore append_historian_rows (the chart-facing write path)
    rows = [
        {
            "tenant_id": "default",
            "gateway_id": "gw-bench",
            "gateway_name": "PLC Bench",
            "device_name": "device",
            "plc_ip": "192.168.10.240",
            "database_name": "Local SQLite",
            "tag_name": r.tag_name,
            "value": r.value,
            "value_text": None,
            "quality": r.quality,
            "quality_label": r.quality_label,
            "ts_utc": r.ts_utc,
            "source": r.source,
        }
        for r in readings
    ]
    t0 = time.monotonic()
    n = store.append_historian_rows(rows) if hasattr(store, "append_historian_rows") else 0
    appstore_ms = (time.monotonic() - t0) * 1000

    # 3) AppStore get_bootstrap (called every 10s from worker)
    t0 = time.monotonic()
    boot = store.get_bootstrap(prefer_cloud_reads=False)
    boot_ms = (time.monotonic() - t0) * 1000

    print(f"   cycle {i}  ok={ok}  telemetry={tel_ms:6.0f}ms  appstore_historian={appstore_ms:6.0f}ms  bootstrap={boot_ms:5.0f}ms  err={err}")

print("\n[Done]")
