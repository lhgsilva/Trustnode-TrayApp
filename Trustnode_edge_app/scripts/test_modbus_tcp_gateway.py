# -*- coding: utf-8 -*-
"""The generic Modbus TCP gateway, end to end against a simulated device.

2026-08-28. Modbus TCP was already in the product but reachable only by power
meters. As a gateway type it covers the widest device population in industry -
VSDs, transmitters, weighing controllers, and the gateway boxes that front every
other fieldbus.

Runs against a pymodbus server with KNOWN register contents rather than against
the live meter: some Modbus devices accept only one TCP connection, and a test
that disturbs production collection is not a test worth having.

Follows the same layers as the ifm smoke test: configure -> read -> database ->
tags -> historian, so a failure is attributed to a layer instead of appearing
as "Modbus doesn't work".
"""
from __future__ import annotations

import json
import os
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
PORT = "8127"
API = "http://127.0.0.1:" + PORT
MB_PORT = 15020
GID = "gw-modbus-test"
FAILS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:140]) if detail else ""
    print("  {0:56s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


# The values the simulated device holds. Chosen so a wrong decode is obvious:
# a float read as two uint16s, or with the words swapped, cannot accidentally
# produce these numbers.
VOLTAGE = 239.24
FREQ = 50.03
COUNTER = 1234567          # needs uint32; overflows a single register
TEMP_TENTHS = 235          # 23.5 C at scale 0.1


def f32_words(value: float):
    hi, lo = struct.unpack(">HH", struct.pack(">f", value))
    return [hi, lo]


def u32_words(value: int):
    hi, lo = struct.unpack(">HH", struct.pack(">I", value))
    return [hi, lo]


def start_device():
    """A Modbus TCP server holding the values above in INPUT registers."""
    from pymodbus.datastore import (ModbusSequentialDataBlock, ModbusServerContext,
                                    ModbusSlaveContext)
    from pymodbus.server import StartTcpServer

    regs = [0] * 300
    regs[0:2] = f32_words(VOLTAGE)      # 30001 -> offset 0
    regs[70:72] = f32_words(FREQ)       # offset 70
    regs[100:102] = u32_words(COUNTER)  # offset 100
    regs[150] = TEMP_TENTHS             # offset 150, uint16 x0.1
    regs[200] = 0b1000                  # offset 200, bit 3 set

    store = ModbusSlaveContext(
        ir=ModbusSequentialDataBlock(0, regs),
        hr=ModbusSequentialDataBlock(0, regs),
        di=ModbusSequentialDataBlock(0, [0] * 100),
        co=ModbusSequentialDataBlock(0, [0] * 100),
        zero_mode=True,
    )
    context = ModbusServerContext(slaves=store, single=True)
    t = threading.Thread(
        target=lambda: StartTcpServer(context=context, address=("127.0.0.1", MB_PORT)),
        daemon=True)
    t.start()
    time.sleep(2.5)
    return t


REGISTERS = [
    {"name": "Voltage_L1", "address": "3x:0",   "function": "input", "kind": "float32", "unit": "V"},
    {"name": "Frequency",  "address": "3x:70",  "function": "input", "kind": "float32", "unit": "Hz"},
    {"name": "Counter",    "address": "3x:100", "function": "input", "kind": "uint32"},
    {"name": "Temp_C",     "address": "3x:150", "function": "input", "kind": "uint16", "scale": 0.1, "unit": "C"},
    {"name": "Run_Bit",    "address": "3x:200", "function": "input", "kind": "bool", "bit": 3},
    {"name": "NotWanted",  "address": "3x:250", "function": "input", "kind": "uint16", "enabled": False},
]


def main() -> int:
    print("TrustNode - generic Modbus TCP gateway, end to end")
    try:
        start_device()
    except Exception as exc:
        print("SKIP: could not start the simulated device (%s)" % str(exc)[:120])
        return 0
    print("  simulated device on 127.0.0.1:%d" % MB_PORT)

    # ---------------------------------------------------- 1. the driver alone
    print()
    print("[1. the driver against the device]")
    from app.drivers.modbus_tcp import points_from_config, read_once
    points = points_from_config(REGISTERS)
    check("an unticked register is not even read",
          len(points) == 5, "%d of %d rows became points" % (len(points), len(REGISTERS)))
    rows = {r["name"]: r for r in read_once("127.0.0.1", MB_PORT, 1, points, timeout_s=4.0)}
    check("every ticked register read GOOD",
          all(r["quality"] for r in rows.values()),
          {k: v.get("error") for k, v in rows.items() if not v["quality"]})
    check("  float32 decodes correctly",
          abs(rows["Voltage_L1"]["value"] - VOLTAGE) < 0.01, rows["Voltage_L1"]["value"])
    check("  a 32-bit counter spanning two registers",
          int(rows["Counter"]["value"]) == COUNTER, rows["Counter"]["value"])
    check("  scale is applied",
          abs(rows["Temp_C"]["value"] - 23.5) < 0.001, rows["Temp_C"]["value"])
    check("  a bit within a register",
          rows["Run_Bit"]["value"] is True, rows["Run_Bit"]["value"])
    check("  and the raw words come back for checking before saving",
          rows["Voltage_L1"].get("raw_words") == list(f32_words(VOLTAGE)),
          rows["Voltage_L1"].get("raw_words"))

    # ------------------------------------------- 2. through the whole product
    print()
    print("[2. as a gateway: read -> database -> tags -> historian]")
    tmp = tempfile.mkdtemp(prefix="tn-modbus-")
    db_path = os.path.join(tmp, "s.db")
    env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
               TRUSTNODE_APP_STORE_PATH=db_path,
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
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "null")
            except Exception:
                return e.code, None
        except Exception as e:
            return 0, str(e)[:160]

    def finish(code):
        call("POST", "/api/plc/gateways/stop", tok, {"gateway_id": GID})
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except Exception:
            proc.kill()
        return code

    tok = None
    check("the app is up", up)
    if not up:
        print(open(os.path.join(tmp, "o.log")).read()[-1500:])
        return finish(2)

    st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
    tok = (b or {}).get("token")
    if not tok:
        check("login", False, st)
        return finish(2)

    ticked = [r["name"] for r in REGISTERS if r.get("enabled") is not False]
    st, r = call("POST", "/api/plc/gateways/start", tok, {
        "gateway_id": GID,
        "config": {
            "gateway_type": "modbus_tcp",
            "name": "Modbus test", "device_name": "SIM",
            "plc_ip": "127.0.0.1",
            "modbus_port": MB_PORT, "modbus_unit_id": 1,
            "modbus_registers": REGISTERS,
            "tags": ticked,
            "interval_ms": 1000,
            "equipment": "SIM", "site": "Bench", "area": "Test",
        }})
    check("the gateway starts", st == 200 and (r or {}).get("started") is True, str(r)[:120])
    if not (r or {}).get("started"):
        return finish(2)

    print("  collecting for 15 s...")
    time.sleep(15)

    con = sqlite3.connect("file:{0}?mode=ro".format(db_path), uri=True, timeout=30)
    cur = con.cursor()
    total = cur.execute("SELECT COUNT(*) FROM historian_readings WHERE gateway_id=?",
                        (GID,)).fetchone()[0]
    tags = sorted(r[0] for r in cur.execute(
        "SELECT DISTINCT tag_name FROM historian_readings WHERE gateway_id=?", (GID,)))
    good = cur.execute(
        "SELECT COUNT(*) FROM historian_readings WHERE gateway_id=? AND quality=192",
        (GID,)).fetchone()[0]
    bad_ts = [t[0] for t in cur.execute(
        "SELECT DISTINCT ts_utc FROM historian_readings WHERE gateway_id=? LIMIT 40", (GID,))
        if "T" in str(t[0]) or "+" in str(t[0])]
    volts = cur.execute(
        "SELECT value FROM historian_readings WHERE gateway_id=? AND tag_name='Voltage_L1' "
        "ORDER BY id DESC LIMIT 1", (GID,)).fetchone()
    con.close()

    check("rows reach the historian", total > 0, total)
    check("  only the ticked registers became tags",
          tags == sorted(ticked), tags)
    check("  every row is GOOD quality", good == total, "%d/%d" % (good, total))
    check("  timestamps match every other gateway", not bad_ts, bad_ts[:2])
    check("  the stored value is the real one",
          volts and abs(volts[0] - VOLTAGE) < 0.01, volts and volts[0])
    per_tag = total / max(1, len(tags))
    check("  cadence matches the configured 1000 ms",
          per_tag >= 12, "%.0f samples per tag in 15 s" % per_tag)

    st, live = call("GET", "/api/app-store/live?limit=2000", tok)
    live_rows = [x for x in ((live or {}).get("rows") or [])
                 if str(x.get("gateway_id")) == GID]
    check("every tag has a live value for the Tags page",
          len(live_rows) == len(ticked), len(live_rows))

    print()
    print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
    return finish(0 if not FAILS else 2)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
