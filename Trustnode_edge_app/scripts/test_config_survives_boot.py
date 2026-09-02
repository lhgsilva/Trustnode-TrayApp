# -*- coding: utf-8 -*-
"""Boot must not scan the historian, and a page that never loaded must not save.

2026-08-30, the operator: "my gateway configuration were gone for no reason,
gateways configuration and names got mixed up. This never should happen to a
customer." Also: "the scan to add a device is taking forever and test
connection too."

One cause underneath all of it. Measured on the live install:

    /api/app-store/bootstrap          16.6 s
    /api/app-store/tenants/inventory  17.3 s
    /api/plc/pointio/scan              0.18 s

get_bootstrap() built the Data Continuity summary synchronously, and that ran

    SELECT tenant_id, COUNT(*), MIN(ts_utc), MAX(ts_utc)
      FROM historian_readings GROUP BY tenant_id

- a full scan of the whole historian, uncached, on the UI's boot path, from
ten call sites. Device scan and test-connection were never slow; they were
queued behind it.

Then the damage. The UI dismisses its splash and force-enables saving after
8 s. Bootstrap had not landed, so the page rendered an EMPTY gateway list, and
the per-domain saver wrote that emptiness-plus-one-new-gateway over a stored
configuration it had never read.

Two independent defences, both tested here:
  * bootstrap no longer scans - it peeks a cached answer;
  * a write whose ids share NOTHING with what is stored is refused, because
    that is the signature of a page that never loaded.

The client-side half (savers gated on a successful read) is checked at source:
it cannot be exercised without a browser, and asserting on a headless render
of the live app is what caused the 2026-08-22 dashboard wipe.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = "8166"
API = "http://127.0.0.1:" + PORT
ROWS = 300_000
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:56s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


# --------------------------------------------------------------- source
print("[the client cannot write a document it never read]")
app_jsx = io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
                  encoding="utf-8", errors="replace").read()
gates = app_jsx.count("if (!appStorePayloadEverHydratedRef.current) return;")
check("every per-domain saver waits for a successful read", gates >= 6,
      "%d saver(s) gated" % gates)
check("a successful read is what unlocks saving, not a non-empty one",
      "if (res?.ok) {" in app_jsx and "appStorePayloadEverHydratedRef.current = true;" in app_jsx,
      "a fresh install has an empty store and must still be able to save")
check("a failed read is on screen, not swallowed",
      "Configuration not loaded." in app_jsx)
check("POINT I/O rack mapping survives Save",
      "point_io_modules: Array.isArray(gatewayForm.point_io_modules)" in app_jsx,
      "the allowlist dropped it, so a scanned rack was discarded by OK")
check("tag lists split on newlines as the UI promises",
      ".split(/[;\\r\\n]+/)" in app_jsx,
      "POINT I/O joined 12 names with newlines and got ONE unmatchable tag")

api_js = io.open(os.path.join(ROOT, "frontend", "src", "api.js"),
                 encoding="utf-8", errors="replace").read()
# Slice generously: this window has already been outgrown once by added
# comments, and a source check that silently stops covering the code it
# names is worse than no check.
save_block = api_js.split("export async function saveAppStoreDomain", 1)[-1][:4000]
check("a config save retries instead of dying on one abort",
      "attempt <= 3" in save_block and "isTransientFetchError" in save_block,
      '"signal is aborted without reason" lost a save with nothing written')

# --------------------------------------------------------------- runtime
print()
print("[boot does not scan the historian]")
tmp = tempfile.mkdtemp(prefix="tn-boot-")
store = os.path.join(tmp, "s.db")

# A historian big enough that a full scan is unmistakably slower than a
# bootstrap that does not perform one.
conn = sqlite3.connect(store)
conn.execute("""CREATE TABLE IF NOT EXISTS historian_readings (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  tenant_id TEXT NOT NULL DEFAULT 'default',
                  ts_utc TEXT NOT NULL, gateway_id TEXT NULL,
                  gateway_name TEXT NULL, device_name TEXT NULL, plc_ip TEXT NULL,
                  database_name TEXT NULL, tag_name TEXT NOT NULL, value REAL NULL,
                  value_text TEXT NULL, data_type TEXT NULL, quality INTEGER NULL,
                  quality_label TEXT NULL, source TEXT NULL, created_utc TEXT NOT NULL)""")
conn.executemany(
    "INSERT INTO historian_readings (tenant_id, ts_utc, tag_name, value, quality, created_utc)"
    " VALUES (?,?,?,?,?,?)",
    (("default", "2026-08-30T00:00:%02dZ" % (i % 60), "T%d" % (i % 40), float(i),
      192, "2026-08-30T00:00:00Z") for i in range(ROWS)))
conn.commit()
conn.close()
print("  seeded %d historian rows" % ROWS)

env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=store,
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
log = open(os.path.join(tmp, "o.log"), "w")
proc = subprocess.Popen([sys.executable, "-m", "app"],
                        cwd=os.path.join(ROOT, "backend"), env=env,
                        stdout=log, stderr=subprocess.STDOUT)
up = False
for _ in range(70):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read()
        up = True
        break
    except Exception:
        time.sleep(2)


def call(method, path, tok=None, body=None):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    d = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=d, headers=h, method=method)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return time.time() - started, r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return time.time() - started, e.code, None
    except Exception as e:
        return time.time() - started, 0, str(e)[:140]


def finish(code):
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    return code


check("the app started", up)
if not up:
    sys.exit(2)
tok = (call("POST", "/api/auth/login",
            body={"username": "admin", "password": "admin"})[2] or {}).get("token")

# Warm any lazy import cost so the comparison is about the query.
call("GET", "/api/app-store/bootstrap", tok)
boot_s, st, _ = call("GET", "/api/app-store/bootstrap", tok)
scan_s, st2, inv = call("GET", "/api/app-store/tenants/inventory", tok)
check("bootstrap is quick with a large historian", st == 200 and boot_s < 1.0,
      "%.2f s" % boot_s)
# Timing alone cannot prove this: a freshly-written 300k-row table scans in
# ~0.1 s, while the live install (fragmented, far larger, under collection
# load) took 17 s. What must hold at every size is that bootstrap does not run
# the scan at all - so assert that at the source, where it is unambiguous.
router_src = io.open(os.path.join(ROOT, "backend", "app", "routers", "app_store.py"),
                     encoding="utf-8", errors="replace").read()
boot_body = router_src.split('def get_bootstrap(', 1)[-1].split("@router.", 1)[0]
check("  because bootstrap peeks instead of scanning",
      "peek_historian_tenant_inventory" in boot_body
      and "list_historian_tenant_inventory" not in boot_body,
      "bootstrap %.2f s, full scan %.2f s at this size" % (boot_s, scan_s))
check("the inventory page still gets real numbers",
      st2 == 200 and any(int(r.get("row_count") or 0) == ROWS
                         for r in ((inv or {}).get("rows") or [])),
      "a cached banner must not cost the page its accuracy")

# --------------------------------------------------------------- the wipe
print()
print("[a page that never loaded cannot replace what is stored]")


def gateways():
    _, _, boot = call("GET", "/api/app-store/bootstrap", tok)
    return ((boot or {}).get("data") or {}).get("gateway_configurations") or []


TWO = [
    {"id": "gw-plc", "name": "PLC", "gateway_type": "allen_bradley",
     "plc_ip": "192.168.10.240", "tags": ["t%d" % i for i in range(49)]},
    {"id": "gw-ifm", "name": "IFM", "gateway_type": "ifm_iolink", "plc_ip": "192.168.10.250"},
]
_, st, _ = call("PUT", "/api/app-store/domain", tok,
                {"domain": "gateway_configurations", "payload": TWO, "actor": "test"})
check("two gateways are stored", st == 200 and len(gateways()) == 2)

# The exact incident: an empty page adds one gateway and saves. One removal,
# one addition - the old bulk-removal rule allowed it.
_, st, _ = call("PUT", "/api/app-store/domain", tok,
                {"domain": "gateway_configurations",
                 "payload": [{"id": "gw-1788124300732", "name": "PLC",
                              "gateway_type": "point_io", "plc_ip": "192.168.10.105"}],
                 "actor": "test", "base_version": 0})   # a page that never read
after = gateways()
check("a write from an unread version is REFUSED", st == 409,
      "HTTP %s - the shape is irrelevant; this write did not know what it replaced" % st)
check("  the PLC and its 49 tags are still there",
      any(g.get("id") == "gw-plc" and len(g.get("tags") or []) == 49 for g in after),
      "%d gateway(s) stored" % len(after))

# Real editing must still work.
_, _, _boot = call("GET", "/api/app-store/bootstrap", tok)
_ver = int(((_boot or {}).get("versions") or {}).get("gateway_configurations") or 0)
edited = [dict(TWO[0], name="PLC renamed"), TWO[1],
          {"id": "gw-pio", "name": "POINT IO", "gateway_type": "point_io",
           "plc_ip": "192.168.10.105",
           "point_io_modules": [{"slot": 2, "name": "1734-IB8", "points": 8}],
           "point_io_points": [{"address": "Slot2_Pt1", "name": "Feed", "enabled": True}],
           "tags": ["Feed"]}]
_, st, _ = call("PUT", "/api/app-store/domain", tok,
                {"domain": "gateway_configurations", "payload": edited,
                 "actor": "test", "base_version": _ver})
after = gateways()
pio = next((g for g in after if g.get("id") == "gw-pio"), {})
check("renaming and adding a gateway still works", st == 200 and len(after) == 3,
      "HTTP %s, %d stored" % (st, len(after)))
check("  the POINT I/O rack mapping is stored with it",
      len(pio.get("point_io_modules") or []) == 1 and len(pio.get("point_io_points") or []) == 1,
      "modules=%d points=%d" % (len(pio.get("point_io_modules") or []),
                                len(pio.get("point_io_points") or [])))

# Restoring a backup over a damaged configuration is a fully disjoint write.
# Under the version rule that needs no special flag at all - a client holding
# the current version may replace everything, which is exactly what a restore
# is. The old `allow_replace` escape hatch is gone with the guard it escaped.
_, _, _b2 = call("GET", "/api/app-store/bootstrap", tok)
_ver2 = int(((_b2 or {}).get("versions") or {}).get("gateway_configurations") or 0)
_, st, _ = call("PUT", "/api/app-store/domain", tok,
                {"domain": "gateway_configurations",
                 "payload": [{"id": "gw-restored", "name": "PLC", "gateway_type": "allen_bradley",
                              "plc_ip": "192.168.10.240", "tags": ["t%d" % i for i in range(49)]}],
                 "actor": "restore", "base_version": _ver2})
after = gateways()
# The stored id may legitimately differ: _stabilise_gateway_ids_by_plc_ip
# reuses the existing id for a gateway at the same PLC IP so the historian
# stays continuous. What matters is that the wholesale replacement was allowed.
check("a restore CAN replace everything, no special flag needed",
      st == 200 and len(after) == 1 and len(after[0].get("tags") or []) == 49,
      "HTTP %s, ids=%s - allow_replace is off by default and never sent by the savers"
      % (st, [g.get("id") for g in after]))

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
