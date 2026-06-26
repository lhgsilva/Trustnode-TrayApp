"""Time a single PLC poll cycle in isolation against the live PLC.

We instantiate a real GatewayWorker against the customer's AB PLC, but
NOT inside the asyncio loop — we just call _read_from_gateway twice in
a row (first cycle pays connect cost, second cycle is the steady-state
read). Then call _persist_sqlite once to measure the local write cost.

This narrows down whether the 16s gap is:
  - read time
  - SQLite write time
  - asyncio scheduling / to_thread overhead
  - the customer-db mirror path
  - outbox/postgres flush
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

os.environ.setdefault("TRUSTNODE_APP_STORE_DB", str(Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db"))

from app.services.plc_manager import GatewayWorker
from app.models import GatewayConfig

cfg = GatewayConfig(
    gateway_type="allen_bradley",
    plc_ip="192.168.10.240",
    interval_ms=1000,
    tags=["SimREAL[3]", "SimREAL[4]", "SimREAL[5]", "SimREAL[6]",
          "SimDINT[0]", "SimDINT[1]", "SimDINT[2]", "SimDINT[3]"],
)
sink = {
    "id": "local-sqlite-default",
    "engine": "sqlite",
    "sqlite_path": "./data/trustnode_edge.db",
    "table": "plc_readings",
}
w = GatewayWorker("gw-bench", cfg, db_sink=sink, db_sinks=[sink])

print("=" * 72)
print("ISOLATED CYCLE TIMING")
print("=" * 72)

print("\n[Cycle 1] First read (pays connect + tag enumeration)")
t0 = time.monotonic()
r1 = w._read_from_gateway()
print(f"   _read_from_gateway: {(time.monotonic()-t0)*1000:6.0f}ms  ({len(r1)} readings)")

print("\n[Cycle 2-6] Steady-state reads (session cached)")
for i in range(2, 7):
    t0 = time.monotonic()
    r = w._read_from_gateway()
    read_ms = (time.monotonic() - t0) * 1000

    t1 = time.monotonic()
    w._persist_sqlite(r)
    write_ms = (time.monotonic() - t1) * 1000

    t2 = time.monotonic()
    # Mirror to customer DB path — this calls get_bootstrap and may be slow
    try:
        w._mirror_to_customer_db_if_active(r)
    except Exception as exc:
        print(f"   mirror error: {exc}")
    mirror_ms = (time.monotonic() - t2) * 1000

    total = read_ms + write_ms + mirror_ms
    print(f"   {i}  read={read_ms:6.0f}ms  sqlite={write_ms:5.0f}ms  mirror={mirror_ms:6.0f}ms  total={total:6.0f}ms")

print("\n[Cleanup]")
w._close_ab_pycomm3_client()
print("done")
