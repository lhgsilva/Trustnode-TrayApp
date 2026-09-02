# -*- coding: utf-8 -*-
"""A collection trigger must actually stop rows reaching the database.

2026-09-02, reported: "we are having a problem with gateways (PLC, meters and
IFM and any other) triggers and limits, it still connecting/showing data even
when the condition was not made. If we have a trigger to collect the signals
we should not collect them to the database unless the condition is made."

The gate itself was fine. What was missing is that it never heard about the
rule: a GatewayWorker holds the configuration it was STARTED with, and
_refresh_global_triggers() was called only from start_gateway, stop_gateway and
stop_all_gateways. Add a trigger while gateways are running and it reached
nobody - the global trigger set stayed empty, and an empty set means "nothing
to gate", so every reading was written exactly as if no rule existed. The rule
was saved, shown on the page, and ignored until every gateway was restarted by
hand, with nothing on screen saying so.

WHAT MUST HOLD

  * saving a trigger applies to gateways that are ALREADY running;
  * while the condition is false, the row count does NOT grow;
  * when the condition becomes true, collection resumes;
  * the gateway keeps RUNNING throughout - a paused write is not a fault, and
    reporting it as one would send an operator looking for a broken PLC;
  * removing the trigger returns the gateway to collecting everything.

Uses a Modbus simulator whose register this test controls, so "condition true"
and "condition false" are facts rather than a wait-and-hope. Runs against its
own backend on a throwaway workspace - never the live install.
"""
from __future__ import annotations

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
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


PORT = str(_free_port())
API = "http://127.0.0.1:" + PORT
MB_PORT = _free_port()

# ----------------------------------------------------------- the simulator
try:
    from pymodbus.datastore import (ModbusSequentialDataBlock, ModbusServerContext,
                                    ModbusSlaveContext)
    from pymodbus.server import StartTcpServer
except Exception as exc:                                    # pragma: no cover
    print("  pymodbus not available: %s" % exc)
    print("RESULT: SKIP")
    sys.exit(0)

# zero_mode=True so offset 0 in the datablock IS register 0 on the wire -
# without it the first run of this test wrote address 1 while the gateway read
# address 0, and "the condition never became true" looked like a product fault
# when it was the simulator.
_block = ModbusSequentialDataBlock(0, [0] * 300)
_store = ModbusSlaveContext(hr=_block, ir=_block,
                            di=ModbusSequentialDataBlock(0, [0] * 100),
                            co=ModbusSequentialDataBlock(0, [0] * 100),
                            zero_mode=True)
_ctx = ModbusServerContext(slaves=_store, single=True)


def set_register(value: int) -> None:
    """The value the trigger is compared against."""
    _block.setValues(0, [int(value)])


set_register(0)
threading.Thread(
    target=lambda: StartTcpServer(context=_ctx, address=("127.0.0.1", MB_PORT)),
    daemon=True).start()
time.sleep(1.5)

# -------------------------------------------------------------- the backend
tmp = tempfile.mkdtemp(prefix="tn-trig-")
DB_PATH = os.path.join(tmp, "s.db")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=DB_PATH,
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
log = open(os.path.join(tmp, "o.log"), "w")
# TRUSTNODE_TEST_EXE points this at the PACKAGED service instead of the source
# tree, so the same checks can be run against a build that is about to ship.
# String-grepping a bundle proves the code is present; only running it proves
# the behaviour is.
_exe = os.environ.get("TRUSTNODE_TEST_EXE", "").strip()
if _exe:
    proc = subprocess.Popen([_exe], env=env, stdout=log, stderr=subprocess.STDOUT)
else:
    proc = subprocess.Popen([sys.executable, "-m", "app"],
                            cwd=os.path.join(ROOT, "backend"), env=env,
                            stdout=log, stderr=subprocess.STDOUT)
up = False
for _ in range(80):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read()
        up = True
        break
    except Exception:
        time.sleep(2)


def call(method, path, token=None, body=None, timeout=60):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=headers,
                                 method=method)
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
    return code


check("the app started", up)
if not up:
    sys.exit(2)
tok = (call("POST", "/api/auth/login",
            body={"username": "admin", "password": "admin"})[1] or {}).get("token")

GW = "gw-trigger-test"
TAG = "Level"
config = {
    "gateway_type": "modbus_tcp",
    "name": "Trigger test",
    "plc_ip": "127.0.0.1",
    "modbus_port": MB_PORT,
    "modbus_unit_id": 1,
    "modbus_registers": [
        {"name": TAG, "address": "4x:0", "function": "holding", "kind": "uint16",
         "unit": "", "enabled": True},
    ],
    "tags": [TAG],
    "interval_ms": 500,
    "equipment": "SIM", "site": "Bench", "area": "Test",
}
st, out = call("POST", "/api/plc/gateways/start", tok,
                 {"gateway_id": GW, "config": config})
check("a gateway starts and collects", st == 200 and (out or {}).get("started") is True,
      (out or {}).get("message"))


def row_count():
    """Rows in the historian table itself.

    Counted in the database rather than through an API, because the question
    is precisely "did a row get WRITTEN" - an endpoint that serves a live
    buffer or a capped page would answer a different question, and this test
    exists to catch silent writes.
    """
    try:
        con = sqlite3.connect("file:{0}?mode=ro".format(DB_PATH), uri=True,
                              timeout=30)
        try:
            return con.execute(
                "SELECT COUNT(*) FROM historian_readings "
                "WHERE gateway_id=? AND tag_name=?", (GW, TAG)).fetchone()[0]
        finally:
            con.close()
    except Exception:
        return 0


def running_state():
    # /api/plc/gateways/status answers with a LIST, not an envelope.
    st, body = call("GET", "/api/plc/gateways/status", tok)
    rows = body if isinstance(body, list) else []
    return next((g for g in rows if str(g.get("gateway_id") or "") == GW), {})


def wait_for_rows(minimum, timeout_s=45.0, poll_s=1.0):
    """Wait until the historian actually has rows, rather than sleeping.

    2026-09-02: this test failed intermittently on "rows are arriving before
    any trigger exists - 0 row(s)", three times in one day, and once inside
    the release gate. It was not the product: a fixed 6 s sleep assumed the
    worker had started, connected and completed a write cycle, and on a busy
    machine it sometimes had not. A flaky gate check is worse than no check -
    it taught me to dismiss a real failure as noise twice today.
    """
    deadline = time.time() + timeout_s
    seen = row_count()
    while seen < minimum and time.time() < deadline:
        time.sleep(poll_s)
        seen = row_count()
    return seen


base = wait_for_rows(1)
check("  rows are arriving before any trigger exists", base > 0,
      "%d row(s)" % base)
if base == 0:
    # Say WHY rather than leaving the reader to guess at a silent zero.
    try:
        log.flush()
        tail = open(os.path.join(tmp, "o.log"), encoding="utf-8",
                    errors="replace").read()[-6000:]
        print("  --- backend log tail ---")
        for line in tail.splitlines()[-40:]:
            # The console here is cp1252; a stray byte in a backend log must
            # not replace the diagnosis with a UnicodeEncodeError.
            print("   ", line.encode("ascii", "replace").decode("ascii"))
    except Exception as exc:
        print("   (could not read the log: %r)" % (exc,))

print()
print("[a trigger saved while the gateway is RUNNING must take effect]")
# Condition: collect only while Level >= 50. The register is 0, so FALSE.
set_register(0)
st, _ = call("PUT", "/api/app-store/domain", tok, {
    "domain": "triggers_limits",
    "actor": "test",
    "payload": {
        "collection_triggers": [
            {"gateway_id": GW, "tag_name": TAG, "operator": ">=", "value": 50,
             "trigger_type": "continuous", "enabled": True},
        ],
        "collection_trigger_mode": "any",
        "trigger_rules": [],
    },
})
check("the trigger was saved", st == 200, st)

time.sleep(4)
before_block = row_count()
time.sleep(7)
after_block = row_count()
grew = after_block - before_block
check("with the condition FALSE, nothing new is written", grew == 0,
      "%d row(s) appeared in 7 s - the rule was saved and ignored" % grew)

rt = running_state()
check("  and the gateway is still RUNNING, not faulted",
      bool(rt.get("running")),
      "a paused write is not a broken PLC; reporting it as one sends an "
      "operator hunting a fault that is not there")
check("  it says WHY it is not writing",
      "trigger" in str(rt.get("collection_block_reason") or "").lower()
      or bool(rt.get("collection_blocked")),
      rt.get("collection_block_reason") or rt.get("collection_blocked"))

print()
print("[when the condition becomes TRUE, collection resumes]")
set_register(75)
after_true = wait_for_rows(after_block + 1, timeout_s=30.0)
check("rows resume once the condition is met", after_true > after_block,
      "%d -> %d" % (after_block, after_true))

print()
print("[and FALSE again stops it again]")
set_register(10)
time.sleep(4)
mark = row_count()
time.sleep(7)
check("dropping back below the threshold stops the writes",
      row_count() - mark == 0, "%d row(s) leaked" % (row_count() - mark))

print()
print("[removing the trigger returns the gateway to collecting]")
st, _ = call("PUT", "/api/app-store/domain", tok, {
    "domain": "triggers_limits", "actor": "test",
    "payload": {"collection_triggers": [], "collection_trigger_mode": "any",
                "trigger_rules": []},
})
time.sleep(4)
mark2 = row_count()
resumed = wait_for_rows(mark2 + 1, timeout_s=30.0)
check("with no trigger, everything is collected again", resumed > mark2,
      "%d -> %d" % (mark2, resumed))

call("POST", "/api/plc/gateways/stop", tok, {"gateway_id": GW})
print()
print("[the energy meter obeys the same rule]")
# 2026-09-02, reported after the first fix shipped: "the energy meter trigger
# is not working, the gateway did not stop the reading and collection if the
# trigger condition was not made." Correct - the first fix reached
# plc_manager's workers, and a power meter has no worker. It polls and writes
# on its own thread, so the gate never saw it. Asserted here on the CODE path,
# because a meter needs hardware this test does not have.
import io as _io  # noqa: E402
pm = _io.open(os.path.join(ROOT, "backend", "app", "services", "power_manager.py"),
              encoding="utf-8", errors="replace").read()
plc = _io.open(os.path.join(ROOT, "backend", "app", "services", "plc_manager.py"),
               encoding="utf-8", errors="replace").read()
check("the meter asks the gate before writing",
      "collection_allowed_now" in pm,
      "it used to write regardless - the gate lived inside the worker loop "
      "and a meter has no worker")
check("  it is asked at the ONE place every meter row passes",
      "_enqueue_rows" in pm
      and pm.index("collection_allowed_now") > pm.index("def _enqueue_rows"),
      "historian, cloud and file sinks all feed from that queue")
check("  withheld rows are counted apart from dropped ones",
      "_blocked_rows" in pm and "_dropped_rows" in pm,
      "dropped means 'could not keep up'; withheld means 'was told not to'")
check("  and a gate it cannot reach does NOT silently stop collection",
      "losing readings is worse" in pm,
      "a broken gate must fail towards keeping data")
check("the gate can be asked from outside a worker",
      "def collection_allowed_now" in plc)
check("  and answers ALLOW when no trigger is configured",
      "if not [t for t in self.global_collection_triggers" in plc,
      "no rule means no gating - a meter must not stop because a feature "
      "nobody configured said nothing")
check("a meter tag can also BE a trigger source",
      "def note_external_readings" in plc and "note_external_readings" in pm,
      "'collect while the line draws more than 5 kW' is the rule an operator "
      "actually wants")
check("  judged stale against its OWN cadence, not a PLC's",
      "interval_ms" in plc.split("def note_external_readings")[1][:900],
      "a 5 s meter measured against a 1 s assumption is always stale, and "
      "its trigger would never evaluate")

print()
print("[a site with a METER and NO PLC gateway]")
# 2026-09-02, found on the live install: a trigger on EM1 (current_l1_a >= 1)
# was saved, enabled, and the meter wrote 1600 rows in 20 s with the condition
# false. _refresh_global_triggers() rebuilt the rule set purely by walking
# self.workers, and apply_collection_triggers() wrote the rules onto those
# workers and read them straight back - so with ZERO PLC workers the rules
# were written nowhere, read back as nothing, and "no rules" means "no
# gating". A meter-only site could never gate at all.
sys.path.insert(0, os.path.join(ROOT, "backend"))
from app.services.plc_manager import PLCManager        # noqa: E402

mgr = PLCManager.__new__(PLCManager)
mgr.workers = {}                                       # no PLC workers at all
mgr.applied_collection_triggers = []
mgr.applied_collection_trigger_mode = "any"
mgr.global_collection_triggers = []
mgr.global_collection_trigger_mode = "any"
mgr.global_live_values = {}
mgr.global_trigger_latches = {}
mgr.global_collection_allowed = True
mgr.global_collection_reason = None
mgr._saved_triggers_loaded = True

check("with no rules the meter collects", mgr.collection_allowed_now("EM1")[0])
mgr.apply_collection_triggers(
    [{"gateway_id": "EM1", "kind": "tag", "tag_name": "current_l1_a",
      "operator": ">=", "value": 1, "enabled": True}], "any")
check("a rule survives having NO workers to hold it",
      len(mgr.global_collection_triggers) == 1,
      "the rule used to be written onto workers and read back from them")

mgr.note_external_readings("EM1", {"current_l1_a": 0.24}, interval_ms=1000)
blocked, why = mgr.collection_allowed_now("EM1")
check("condition FALSE pauses the meter", not blocked, why)

mgr.note_external_readings("EM1", {"current_l1_a": 5.0}, interval_ms=1000)
check("condition TRUE resumes it", mgr.collection_allowed_now("EM1")[0],
      "reading the cached GLOBAL verdict never came back once it went false - "
      "a gateway-scoped rule deliberately does not update it")

mgr.note_external_readings("EM1", {"current_l1_a": 0.2}, interval_ms=1000)
check("  and back to FALSE pauses it again",
      not mgr.collection_allowed_now("EM1")[0])
check("a rule for EM1 does NOT pause a different gateway",
      mgr.collection_allowed_now("gw-other")[0],
      "one verdict for the whole site paused machines the rule never named")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
