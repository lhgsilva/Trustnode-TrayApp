"""Validate cadence AFTER the single-DB fix + DB compaction.

Builds a GatewayWorker exactly the way the live backend builds one,
points it at the compacted customer DB, runs 8 cycles against the
real AB PLC, and reports cycle deltas.
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

os.environ["TRUSTNODE_APP_STORE_PATH"] = r"C:\Users\User\.trustnode_edge\data\trustnode_app_store.db"
os.environ["TRUSTNODE_DATA_DIR"] = r"C:\Users\User\.trustnode_edge\data"

from app.services.plc_manager import GatewayWorker
from app.services.app_store import AppStore
from app.services.telemetry_service import TelemetryService
from app.models import GatewayConfig

cfg = GatewayConfig(
    gateway_type="allen_bradley",
    plc_ip="192.168.10.240",
    interval_ms=1000,
    tags=[f"SimREAL[{i}]" for i in range(10)] + [f"SimDINT[{i}]" for i in range(10)],
)
sink = {
    "id": "local-sqlite-default",
    "name": "Local SQLite",
    "engine": "sqlite",
    "sqlite_path": "./data/trustnode_edge.db",
    "table": "plc_readings",
}

store = AppStore()
tel = TelemetryService()

w = GatewayWorker("gw-1779098315351", cfg, db_sink=sink, db_sinks=[sink])

print("=" * 72)
print("POST-FIX CYCLE TIMING")
print("=" * 72)

# Simulate the full per-cycle pipeline the live worker runs
write_count_before = 0
prev_read_ts = None
deltas = []
for i in range(10):
    t0 = time.monotonic()
    # 1) Read
    t_read = time.monotonic()
    readings = w._read_from_gateway()
    read_ms = (time.monotonic() - t_read) * 1000

    # 2) telemetry record_collection_cycle (mandatory local persist)
    t_tel = time.monotonic()
    ok, err, edge_id = tel.record_collection_cycle(
        gateway_id="gw-1779098315351", config=cfg, readings=readings, collection_status="ok"
    )
    tel_ms = (time.monotonic() - t_tel) * 1000

    # 3) app_store.append_historian_rows (the canonical chart-feeding write)
    rows = []
    for r in readings:
        rows.append({
            "ts_utc": r.ts_utc, "source": r.source, "gateway_id": "gw-1779098315351",
            "gateway_name": "PLC 1", "device_name": "", "plc_ip": cfg.plc_ip,
            "database_name": "Local SQLite", "tag_name": r.tag_name,
            "value": r.value, "value_text": getattr(r, "value_text", None),
            "quality": r.quality, "quality_label": r.quality_label,
        })
    t_app = time.monotonic()
    n = store.append_historian_rows(rows)
    app_ms = (time.monotonic() - t_app) * 1000

    # 4) _persist_readings (the FIX: this now skips _persist_sqlite for engine=sqlite)
    t_per = time.monotonic()
    w._persist_readings(readings)
    per_ms = (time.monotonic() - t_per) * 1000

    total_ms = (time.monotonic() - t0) * 1000
    cur_ts = readings[0].ts_utc
    delta_ms = None
    if prev_read_ts:
        from datetime import datetime, timezone
        a = datetime.strptime(prev_read_ts, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
        b = datetime.strptime(cur_ts, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
        delta_ms = (b - a).total_seconds() * 1000
        deltas.append(delta_ms)
    prev_read_ts = cur_ts

    print(f"  {i:2d}  read={read_ms:6.0f}  telemetry={tel_ms:6.0f}  app_store={app_ms:6.0f}  persist={per_ms:6.0f}  TOTAL={total_ms:6.0f}ms  inter-read={delta_ms or '-':>6}  rows={n}")
    # Pace to 1s like the run loop
    target_s = max(cfg.interval_ms / 1000.0, 0.1)
    elapsed = time.monotonic() - t0
    sleep_for = max(0.01, target_s - elapsed)
    time.sleep(sleep_for)

if deltas:
    avg = sum(deltas) / len(deltas)
    print(f"\n  avg inter-read = {avg:.0f}ms   target=1000ms")
    if avg < 1300:
        print(f"  *** PASS *** cadence is at target")
    else:
        print(f"  *** FAIL *** cadence is still slow")

w._close_ab_pycomm3_client()
print("\n[Done]")
