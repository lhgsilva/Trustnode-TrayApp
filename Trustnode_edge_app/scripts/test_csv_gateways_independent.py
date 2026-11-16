# -*- coding: utf-8 -*-
"""Gateways on one PLC run independently; CSV exports keep their own shape.

2026-11-16, from the owner:
  * "when setting the gateway to export to csv and select to have it arranged
    by timestamp per row, it is still not showing the tags in different
    columns" - the PLC writers never read `layout`;
  * "when start/stop gateways that use the same plc ip address both gateways
    csv and database stop and start at the same time but they should each one
    have their own control" - only one worker per address was allowed;
  * "I needed the csv gateway to only log the values of the tags only once
    when the condition is true" - a One-Time edge was used up by other
    evaluations and held back by the export interval;
  * parallel exports "that log the same information or less or more gateways
    and different arrangements ... fully customized".

Runs its OWN backend on a throwaway workspace with a Modbus simulator whose
registers this test sets, so every condition is a fact (never the live app):

  1. two gateways on 127.0.0.1:<sim> both collect at the same time;
  2. stopping one leaves the other collecting, and it starts again alone;
  3. the CSV gateway's own file is wide: one row per timestamp, a column per
     tag, the tag values in their columns;
  4. a parallel CSV restricted to ONE gateway and ONE tag, long layout, gets
     only that - its own arrangement beside the wide file;
  5. a One-Time trigger scoped to the CSV gateway, with a 10 s export
     interval on its file, writes exactly one row per false->true edge - a
     second edge 3 s later included - while the database gateway keeps
     collecting ungated;
  6. an "All gateways" rule evaluates (it never did), a dry run never uses
     an edge up, and restarts rebuild a gateway with ALL its exports.

    python scripts/test_csv_gateways_independent.py
"""
from __future__ import annotations

import csv
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS: list = []


def check(name, ok, detail=""):
    print("  {0:66s}: {1}{2}".format(name, "PASS" if ok else "FAIL", (" - " + str(detail)[:220]) if (detail and not ok) else ""))
    if not ok:
        FAILS.append(name)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------- unit part
print("[the gate, in-process]")
sys.path.insert(0, os.path.join(ROOT, "backend"))
_UT = tempfile.mkdtemp(prefix="tn-csvind-unit-")
os.environ.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=_UT,
                  TRUSTNODE_APP_STORE_PATH=os.path.join(_UT, "s.db"), TRUSTNODE_BOOT_INTEGRITY_CHECK="never")
from app.services.plc_manager import PLCManager  # noqa: E402
try:
    from app.services.plc_manager import saved_gateway_sinks  # noqa: E402
except ImportError:
    saved_gateway_sinks = None
from app.models import GatewayReading  # noqa: E402


def _reading(tag, value):
    return GatewayReading(ts_utc=time.strftime("%Y-%m-%d %H:%M:%S.000", time.gmtime()), gateway_id="x", tag_name=tag,
                          value=float(value), quality=192, quality_label="GOOD", source="t", site="s", area="a", equipment="e")


m = PLCManager.__new__(PLCManager)
for k, v in (("global_live_values", {}), ("global_collection_triggers", []), ("global_trigger_latches", {}),
             ("one_time_released", {}), ("global_collection_trigger_mode", "any"), ("workers", {}),
             ("applied_collection_triggers", []), ("applied_collection_trigger_mode", "any"),
             ("global_collection_allowed", True), ("global_collection_reason", None)):
    setattr(m, k, v)
m.apply_collection_triggers([{"gateway_id": "*", "tag_name": "Run", "operator": ">", "value": 0,
                              "trigger_type": "continuous", "enabled": True}], "any")
ok_star, why_star = m._evaluate_global_collection_gate("gwA", [_reading("Run", 1)])
check("an 'All gateways' tag rule evaluates (it looked up the key '*::Run')", ok_star is True, why_star)
m.apply_collection_triggers([{"gateway_id": "gwA", "tag_name": "Run", "operator": ">", "value": 0,
                              "trigger_type": "one_time", "enabled": True}], "any")
seq = []
for i, v in enumerate([0, 0, 1, 1, 1, 0, 1, 1]):
    if i == 2:
        m._evaluate_global_collection_gate("", [])          # the dry run every save/start/stop does
    seq.append(m._evaluate_global_collection_gate("gwA", [_reading("Run", v)])[0])
check("One-Time writes exactly one cycle per rising edge", seq == [False, False, True, False, False, False, True, False],
      seq)
check("  and a dry run in between never used the edge up", seq[2] is True, seq)
check("  the edge is reported to the worker for that cycle", m.one_time_released.get("gwA") is False and True in seq)
gw = {"id": "gwA", "database_id": "csv-a"}
rows = [{"id": "csv-a", "engine": "csv_file", "file_path": "a.csv", "layout": "wide", "export_interval_s": 10,
         "tag_filters": ["Run", "Flow"]},
        {"id": "db-1", "engine": "sqlite", "sqlite_path": "x.db"},
        {"id": "csv-b", "engine": "csv_file", "file_path": "b.csv", "layout": "long", "gateway_filters": ["gwB"],
         "tag_filters": ["Temp"]},
        {"id": "csv-off", "engine": "csv_file", "enabled": False},
        {"id": "mysql-1", "engine": "mysql"}]
prim, allsinks = saved_gateway_sinks(gw, rows) if saved_gateway_sinks else ({}, [])
check("a restart rebuilds the gateway with ALL its exports (not the primary alone)",
      [s["id"] for s in allsinks] == ["csv-a", "db-1", "csv-b"], [s["id"] for s in allsinks])
check("  each with its layout, interval and selection kept",
      len(allsinks) == 3 and prim["layout"] == "wide" and prim["export_interval_s"] == 10 and prim["tag_filters"] == ["Run", "Flow"]
      and allsinks[2]["gateway_filters"] == ["gwB"], json.dumps(prim)[:160])
main_src = open(os.path.join(ROOT, "backend", "app", "main.py"), encoding="utf-8").read()
check("  boot auto-resume uses that rule and restores the saved triggers first",
      "_saved_sinks(gw, db_rows)" in main_src and 'get_domain_scoped_fast("triggers_limits")' in main_src)

# ---------------------------------------------------------------- the app's pages
app_src = open(os.path.join(ROOT, "frontend", "src", "App.jsx"), encoding="utf-8").read()
check("the CSV 'Tags to export' picker reads gateway tags as plain names",
      '(t && typeof t === "object") ? (t.tag_name || t.name || "") : (t || "")' in app_src
      and ".map((t) => String(t?.tag_name || t?.name || \"\").trim())" not in app_src)
_edit = app_src.split("const openEditDbConnection", 1)[1].split("setDbTestResult(", 1)[0]
check("reopening a CSV connection loads its row layout, interval, rollover and size",
      all(k in _edit for k in ('layout: String(conn.layout', 'export_interval_s: Number(conn.export_interval_s',
                                'rollover: String(conn.rollover', 'max_file_mb: Number(conn.max_file_mb')))
check("a gateway never borrows a sibling's RUNNING status on the same PLC",
      "if (ownedIds.has(String(row?.gateway_id || \"\"))) return false;" in app_src)
_trig = app_src.split("const openEditCollectionTrigger", 1)[1][:1600]
check("editing a schedule trigger keeps it a schedule trigger",
      'kind: String(trigger.kind || "tag")' in _trig and "schedule_interval: String(trigger.schedule_interval" in _trig)
check("saving a gateway that shares tags with another on the same PLC says so",
      "already reads some of these tags" in app_src)

# ---------------------------------------------------------------- end to end
print("\n[two gateways on one PLC, end to end]")
try:
    from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext, ModbusSlaveContext
    from pymodbus.server import StartTcpServer
except Exception as exc:                                    # pragma: no cover
    print("  pymodbus not available: %s" % exc)
    print("RESULT: SKIP")
    sys.exit(0)

MB_PORT = _free_port()
_block = ModbusSequentialDataBlock(0, [0] * 300)
_store = ModbusSlaveContext(hr=_block, ir=_block, di=ModbusSequentialDataBlock(0, [0] * 100),
                            co=ModbusSequentialDataBlock(0, [0] * 100), zero_mode=True)
_ctx = ModbusServerContext(slaves=_store, single=True)


def reg(addr, value):
    _block.setValues(addr, [int(value)])


reg(0, 0); reg(1, 11); reg(2, 22)          # Run, Flow, Temp
threading.Thread(target=lambda: StartTcpServer(context=_ctx, address=("127.0.0.1", MB_PORT)), daemon=True).start()
time.sleep(1.5)

PORT = str(_free_port())
API = "http://127.0.0.1:" + PORT
tmp = tempfile.mkdtemp(prefix="tn-csvind-")
DB_PATH = os.path.join(tmp, "s.db")
WIDE = os.path.join(tmp, "exports", "csv_gateway_wide.csv")
TEMP_ONLY = os.path.join(tmp, "exports", "db_gateway_temp_only.csv")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp, TRUSTNODE_APP_STORE_PATH=DB_PATH,
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
log = open(os.path.join(tmp, "o.log"), "w")
_exe = os.environ.get("TRUSTNODE_TEST_EXE", "").strip()
proc = (subprocess.Popen([_exe], env=env, stdout=log, stderr=subprocess.STDOUT) if _exe else
        subprocess.Popen([sys.executable, "-m", "app"], cwd=os.path.join(ROOT, "backend"), env=env,
                         stdout=log, stderr=subprocess.STDOUT))


def call(method, path, token=None, body=None, timeout=60):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "null")
        except Exception:
            return e.code, None
    except Exception as e:
        return 0, str(e)[:140]


def finish(code):
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    print("\nRESULT: %s" % ("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
    sys.exit(code)


up = False
for _ in range(80):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read()
        up = True
        break
    except Exception:
        time.sleep(2)
check("the test backend started (own workspace)", up)
if not up:
    finish(2)
tok = (call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})[1] or {}).get("token")


def gw_config(name, tags):
    regs = [{"name": "Run", "address": "4x:0", "function": "holding", "kind": "uint16", "unit": "", "enabled": True},
            {"name": "Flow", "address": "4x:1", "function": "holding", "kind": "uint16", "unit": "", "enabled": True},
            {"name": "Temp", "address": "4x:2", "function": "holding", "kind": "uint16", "unit": "", "enabled": True}]
    return {"gateway_type": "modbus_tcp", "name": name, "plc_ip": "127.0.0.1", "modbus_port": MB_PORT,
            "modbus_unit_id": 1, "modbus_registers": [r for r in regs if r["name"] in tags], "tags": tags,
            "interval_ms": 500, "equipment": "SIM", "site": "Bench", "area": "Test"}


LOCAL = {"id": "local-sqlite", "name": "Local SQLite", "engine": "sqlite", "sqlite_path": DB_PATH, "table": "plc_readings"}
WIDE_SINK = {"id": "csv-wide", "name": "CSV wide", "engine": "csv_file", "file_path": WIDE, "layout": "wide",
             "tag_filters": [], "gateway_filters": [], "csv_format": "", "csv_header": "", "export_interval_s": 0}
TEMP_SINK = {"id": "csv-temp", "name": "Temp only", "engine": "csv_file", "file_path": TEMP_ONLY, "layout": "long",
             "tag_filters": ["Temp"], "gateway_filters": ["gw-db"], "csv_format": "", "csv_header": "", "export_interval_s": 0}
A, B = "gw-csv", "gw-db"


def start(gid, cfg, sink, sinks):
    return call("POST", "/api/plc/gateways/start", tok, {"gateway_id": gid, "config": cfg, "db_sink": sink, "db_sinks": sinks})


def hist(gid):
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=30)
        try:
            return con.execute("SELECT COUNT(*) FROM historian_readings WHERE gateway_id=?", (gid,)).fetchone()[0]
        finally:
            con.close()
    except Exception:
        return -1


def lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [r for r in csv.reader(f)]
    except Exception:
        return []


stA, outA = start(A, gw_config("CSV gateway", ["Run", "Flow", "Temp"]), WIDE_SINK, [WIDE_SINK])
stB, outB = start(B, gw_config("DB gateway", ["Flow", "Temp"]), LOCAL, [LOCAL, TEMP_SINK])
check("both gateways on the same PLC address start", stA == 200 and stB == 200
      and (outA or {}).get("started") is True and (outB or {}).get("started") is True, (outA, outB))
time.sleep(6)
a1, b1 = hist(A), hist(B)
time.sleep(4)
a2, b2 = hist(A), hist(B)
check("  and BOTH collect at the same time (neither evicted the other)", a2 > a1 > 0 and b2 > b1 > 0, (a1, a2, b1, b2))

call("POST", "/api/plc/gateways/stop", tok, {"gateway_id": A})
time.sleep(2)
a3, b3 = hist(A), hist(B)
time.sleep(4)
a4, b4 = hist(A), hist(B)
check("stopping the CSV gateway stops only it", a4 == a3 and b4 > b3, (a3, a4, b3, b4))
start(A, gw_config("CSV gateway", ["Run", "Flow", "Temp"]), WIDE_SINK, [WIDE_SINK])
time.sleep(5)
a5, b5 = hist(A), hist(B)
time.sleep(4)
check("  and it starts again on its own, the other still running", hist(A) > a5 and hist(B) > b5, (a5, hist(A), b5, hist(B)))

w = lines(WIDE)
check("the CSV gateway's file is one row per timestamp, a column per tag",
      bool(w) and w[0][:3] == ["ts_local", "ts_utc", "gateway_id"] and set(w[0][3:]) == {"Run", "Flow", "Temp"},
      w[:2])
if w and len(w) > 2:
    hdr = w[0]
    body = w[1:]
    check("  every row holds all three tags in their own columns",
          all(r[hdr.index("Flow")] in ("11", "11.0") and r[hdr.index("Temp")] in ("22", "22.0") for r in body[-5:]),
          body[-2:])
    check("  and one timestamp appears once", len({r[1] for r in body}) == len(body), len(body))
t = lines(TEMP_ONLY)
check("the parallel export restricted to the DB gateway and Temp gets only that",
      bool(t) and all(("Temp" in r) for r in t if r and r[0] != "ts_utc") and not any(("Flow" in r) or ("Run" in r) for r in t),
      t[:3])

# ---- One-Time on the CSV gateway, 10 s export interval -----------------------
print("\n[log once when the condition becomes true]")
GATED = os.path.join(tmp, "exports", "csv_gateway_once.csv")
ONCE_SINK = dict(WIDE_SINK, id="csv-once", file_path=GATED, export_interval_s=10)
call("POST", "/api/plc/gateways/stop", tok, {"gateway_id": A})
reg(0, 0)
st, _ = call("PUT", "/api/app-store/domain", tok, {"domain": "triggers_limits", "actor": "test", "payload": {
    "collection_triggers": [{"gateway_id": A, "tag_name": "Run", "operator": ">", "value": 0,
                             "trigger_type": "one_time", "enabled": True}],
    "collection_trigger_mode": "any", "trigger_rules": []}})
check("a One-Time trigger scoped to the CSV gateway is saved", st == 200, st)
start(A, gw_config("CSV gateway", ["Run", "Flow", "Temp"]), ONCE_SINK, [ONCE_SINK])
time.sleep(4)
check("  nothing is logged while the condition is false", len(lines(GATED)) <= 1, len(lines(GATED)))
reg(0, 1)
time.sleep(4)
n_first = len(lines(GATED)) - 1
check("  the first rising edge logs exactly one row", n_first == 1, lines(GATED))
time.sleep(3)
check("  staying true logs nothing more", len(lines(GATED)) - 1 == 1, len(lines(GATED)) - 1)
reg(0, 0)
time.sleep(2)
reg(0, 1)                                       # a second edge, well inside the 10 s interval
time.sleep(3)
check("  a second edge inside the export interval is logged at once (not held, not lost)",
      len(lines(GATED)) - 1 == 2, lines(GATED)[-2:])
b6 = hist(B)
time.sleep(3)
check("the database gateway keeps collecting, ungated by the CSV gateway's rule", hist(B) > b6, (b6, hist(B)))

finish(0 if not FAILS else 1)
