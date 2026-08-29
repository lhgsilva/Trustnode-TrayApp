# -*- coding: utf-8 -*-
"""A scoped config read must return the gateways even while the DB is busy.

2026-08-28 REGRESSION. Moving `get_bootstrap_scoped` to the read-only handle
(`timeout=3.0`, `busy_timeout=3000`) to shave a 6 s p95 off
/api/plc/gateways/status made the read time out against a 13.4 GB store at
boot. The callers do:

    try:
        shared = app_store.get_bootstrap_scoped(key, ...) or {}
    except Exception:
        return bootstrap

and `gateway_configurations` / `devices` are _SHARED_EDGE_DOMAINS - so the
failure did not surface as an error. It surfaced as the operator's devices and
gateways being **gone**. Nothing was deleted. Nothing was logged.

This test does what the earlier one did not: it puts the database under write
pressure while reading the config, which is the condition that broke it. A test
that only reads an idle store would have passed throughout the outage.
"""
import io
import os
import sqlite3
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
FAILS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:150]) if detail else ""
    print("  {0:56s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


print("[the read path]")
src = io.open(os.path.join(ROOT, "backend", "app", "services", "app_store.py"),
              encoding="utf-8", errors="replace").read()
body = src[src.index("def get_bootstrap_scoped"):]
body = body[:body.index("\n    def ", 10)]

# The read-only handle's 3 s bound is right for a historian page and wrong for
# a config read. If this ever comes back, so does the outage.
check("the config read does NOT use the 3 s read-only handle",
      "_connect_readonly()" not in body,
      "" if "_connect_readonly()" not in body
      else "its busy_timeout is 3 s; a config read needs the 10 s connection")
check("  it uses the write-capable connection's longer timeout",
      body.count("self._connect()") >= 2, body.count("self._connect()"))
check("  and still does not take the global write mutex",
      "with self._lock:" not in body,
      "the lock was the 6 s p95; the timeout was the outage - they are different things")
check("a failed read can no longer be silent",
      "def get_bootstrap_scoped_or_shout" in src)

for router in ("app_store.py", "plc.py"):
    r = io.open(os.path.join(ROOT, "backend", "app", "routers", router),
                encoding="utf-8", errors="replace").read()
    bare = r.count("get_bootstrap_scoped(")   # the _or_shout form has no "(" here
    check("  routers/%s calls the loud wrapper" % router, bare == 0,
          "%d bare call(s) left" % bare)

# ------------------------------------------------- under real write pressure
print()
print("[a scoped read while the database is being written hard]")
tmp = tempfile.mkdtemp(prefix="tn-scoped-")
db = os.path.join(tmp, "s.db")
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_APP_STORE_PATH"] = db
os.environ["TRUSTNODE_DATA_DIR"] = tmp
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"

from app.services.app_store import AppStore  # noqa: E402

store = AppStore()
SCOPE = "tenant-cust-e5916328|cust-e5916328|edge-74d903ffcd"
GATEWAYS = [
    {"id": "gw-1781903248499", "name": "PLC", "gateway_type": "allen_bradley",
     "plc_ip": "192.168.10.240", "tags": ["A", "B"]},
    {"id": "gw-1787918911149", "name": "IFM", "gateway_type": "ethernet_ip",
     "plc_ip": "192.168.10.251", "eip_input_assembly": 100,
     "tags": ["Port7_Pin2", "Port7_Pin4", "Port8_Pin2", "Port8_Pin4"]},
]
DEVICES = [{"id": "dev-1", "name": "IFM", "plc_ip": "192.168.10.251"}]

store.upsert_domain_scoped(SCOPE, "gateway_configurations", GATEWAYS, actor="test")
store.upsert_domain_scoped(SCOPE, "devices", DEVICES, actor="test")

# Hammer the historian from another thread, as the app does at boot.
stop = threading.Event()
written = {"n": 0}


def writer():
    while not stop.is_set():
        try:
            store.append_historian_rows([
                {"ts_utc": "2026-08-28 20:00:00.000", "gateway_id": "gw-load",
                 "tag_name": "T%d" % i, "value": float(i), "quality": 192,
                 "quality_label": "GOOD", "source": "test"}
                for i in range(200)
            ])
            written["n"] += 200
        except Exception:
            pass


threads = [threading.Thread(target=writer, daemon=True) for _ in range(3)]
for t in threads:
    t.start()
time.sleep(1.0)

worst = 0.0
empties = 0
for _ in range(25):
    t0 = time.perf_counter()
    try:
        data = store.get_bootstrap_scoped(SCOPE, prefer_cloud_reads=False) or {}
    except Exception as exc:
        empties += 1
        data = {"__error__": str(exc)[:80]}
    worst = max(worst, (time.perf_counter() - t0) * 1000)
    got = data.get("gateway_configurations") or []
    if len(got) != len(GATEWAYS):
        empties += 1
    time.sleep(0.05)

stop.set()
for t in threads:
    t.join(timeout=3)

check("the gateways come back on every read, under load",
      empties == 0, "%d of 25 read(s) returned the wrong count" % empties)
check("  (%d rows were being written during it)" % written["n"], written["n"] > 0)
check("  and the read stays bounded", worst < 9000, "worst %.0f ms" % worst)

final = store.get_bootstrap_scoped(SCOPE, prefer_cloud_reads=False) or {}
ids = [g.get("id") for g in (final.get("gateway_configurations") or [])]
check("both gateways are present by id",
      ids == [g["id"] for g in GATEWAYS], ids)
check("  the ifm gateway keeps its assembly and its 4 ticked tags",
      any(g.get("eip_input_assembly") == 100 and len(g.get("tags") or []) == 4
          for g in (final.get("gateway_configurations") or [])))
check("  and devices come back too",
      len(final.get("devices") or []) == 1, final.get("devices"))

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
