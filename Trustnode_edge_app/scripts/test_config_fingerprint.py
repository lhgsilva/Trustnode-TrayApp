# -*- coding: utf-8 -*-
"""The distribution thread must stop re-parsing every config document every 10s,
WITHOUT ever missing a real config change. Throwaway store only."""
import json, os, sys, tempfile, time
ROOT = r"D:\Trustnode\Trustnode-AB\Tray_app\Trustnode_edge_app"
sys.path.insert(0, os.path.join(ROOT, "backend"))
tmp = tempfile.mkdtemp(prefix="tn-fp-")
os.environ.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
                  TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never")
from app.state import app_store
FAILS = []
def check(n, ok, d=""):
    print(f"  {n:56s}: {'PASS' if ok else 'FAIL'}{(' - ' + str(d)[:120]) if d else ''}")
    if not ok: FAILS.append(n)

# a realistically sized config: the dashboard document is what makes the
# full read expensive on a real install.
widgets = [{"id": f"w{i}", "type": "line_chart", "title": f"Widget {i}",
            "config": {"tag_name": f"Tag_{i}", "series_extra": [{"id": f"s{j}"} for j in range(20)]}}
           for i in range(60)]
app_store.upsert_domain("dashboard_configurations", {"widgets": widgets}, actor="test")
app_store.upsert_domain("app_settings", {"cloud_url": "https://a.example", "endpoint_mode": "cloud"}, actor="test")

f1 = app_store.config_fingerprint()
check("fingerprint is non-empty", bool(f1), f1)
f2 = app_store.config_fingerprint()
check("fingerprint is stable when nothing changes", f1 == f2, f"{f1} vs {f2}")

app_store.upsert_domain("app_settings", {"cloud_url": "https://b.example", "endpoint_mode": "cloud"}, actor="test")
f3 = app_store.config_fingerprint()
check("fingerprint CHANGES on a real config write", f3 != f2, f"{f2} -> {f3}")

# a scoped write must not be missed either -- but get_bootstrap reads unscoped,
# so the contract is only about unscoped documents. Verify a second domain too.
app_store.upsert_domain("database_configurations", [{"id": "db1"}], actor="test")
f4 = app_store.config_fingerprint()
check("fingerprint changes for any domain", f4 != f3, f"{f3} -> {f4}")

# THE point of the change: the fingerprint must not queue behind the writer.
# On an idle throwaway store both calls are milliseconds; what mattered on the
# live edge was the WAIT for the global store lock while the historian writer
# held it. So hold that lock and measure what each call does.
import threading
held = threading.Event()
release = threading.Event()

def _hog():
    with app_store._lock:
        held.set()
        release.wait(6.0)

t = threading.Thread(target=_hog, daemon=True); t.start()
held.wait(5.0)

t0 = time.perf_counter()
fp_val = app_store.config_fingerprint()
fp_locked_ms = (time.perf_counter() - t0) * 1000.0

t0 = time.perf_counter()
gb = threading.Thread(target=lambda: app_store.get_bootstrap(prefer_cloud_reads=False), daemon=True)
gb.start(); gb.join(timeout=1.0)
full_blocked = gb.is_alive()
release.set(); t.join(timeout=5.0); gb.join(timeout=5.0)

print(f"     while the store lock is HELD: fingerprint {fp_locked_ms:.1f} ms, "
      f"get_bootstrap {'BLOCKED' if full_blocked else 'completed'}")
check("fingerprint answers while the store lock is held", fp_locked_ms < 500 and bool(fp_val),
      f"{fp_locked_ms:.1f}ms")
check("the full read DOES block on that lock (so avoiding it is the win)", full_blocked,
      "get_bootstrap returned while the lock was held")

# --- the behaviour that must NOT break: telemetry still sees every change ---
applied = []
class FakeTelemetry:
    def configure_from_bootstrap(self, bootstrap):
        s = (bootstrap or {}).get("app_settings") or {}
        applied.append(str(s.get("cloud_url") or ""))

tel = FakeTelemetry()
last_fp, config_applied = "", False
def tick():
    """Exactly the logic now in DistributionV2._distribute_one."""
    global last_fp, config_applied
    fp = app_store.config_fingerprint()
    if fp != last_fp or not config_applied:
        bs = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
        tel.configure_from_bootstrap(bs)
        last_fp, config_applied = fp, True
        return True
    return False

did_first = tick()
check("first cycle always configures telemetry", did_first and len(applied) == 1, applied)
did = [tick() for _ in range(10)]
check("10 idle cycles do NO full reads", not any(did), f"{sum(did)} reads")
app_store.upsert_domain("app_settings", {"cloud_url": "https://c.example", "endpoint_mode": "cloud"}, actor="test")
check("a config change IS picked up on the next cycle", tick(), "")
check("telemetry received the NEW value", applied[-1] == "https://c.example", applied[-1])
did = [tick() for _ in range(5)]
check("and it settles again with no further reads", not any(did), f"{sum(did)} reads")
print()
print(f"RESULT: {'PASS' if not FAILS else 'FAIL - ' + ', '.join(FAILS)}")
sys.exit(0 if not FAILS else 2)
