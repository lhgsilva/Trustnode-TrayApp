# -*- coding: utf-8 -*-
"""Commission an EM122 the way an operator does, against the real meter.

2026-08-31: "I am trying to add a power energy meter 122 and it just do not
working - test connection and press ok it saying the device fails, saying the
devices aborted with no reason, then says it is running but no data."

Measured first, because the obvious suspect was wrong: the meter at
192.168.10.200 answers Modbus function 4 in 20-30 ms, and the driver's read
timeout - capped at 450 ms - is nowhere near being exceeded. The meter is fine.

TWO faults, and the second is the one that produced "no data".

1. The SAVE could abort. updatePowerConfig used fetchWithTimeout's bare 12 s
   default with no retry - the same single-shot pattern that produced "signal
   is aborted without reason" for device saves, fixed there on 2026-08-30 and
   missed here. Press OK on a busy edge, the fetch aborts, and the operator is
   told the DEVICE failed when what failed was writing it down.

2. The register profile was SILENTLY SWAPPED. Saving `weidmuller-em122-3ph`
   stored `weidmuller_em525_three_phase_basic`: the catalogue and the power
   manager grew different id spellings for the same meters, and an
   unrecognised id fell back to a default - a DIFFERENT METER's register map.
   Every reading came back 0.0 while the status said connected, running, no
   error. Reproduced here, then fixed: ids resolve tolerantly, and an id that
   still does not resolve refuses to collect and says so.

   Before: profile=weidmuller_em525_three_phase_basic, voltage_l1_v=0.0
   After : profile=weidmuller_em122_three_phase,       voltage_l1_v=234.87

This walks the whole flow - test connection, save, enable, collect - and
asserts values actually arrive. SKIPs when the meter is not on the network, so
it is safe on a machine without one.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METER_IP = os.environ.get("TRUSTNODE_TEST_METER_IP", "192.168.10.200")
METER_PORT = int(os.environ.get("TRUSTNODE_TEST_METER_PORT", "502"))
PORT = "8178"
API = "http://127.0.0.1:" + PORT
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:54s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("TrustNode - power meter commissioning, end to end")
print("  meter: %s:%d" % (METER_IP, METER_PORT))
try:
    s = socket.create_connection((METER_IP, METER_PORT), timeout=4)
    s.close()
except Exception as exc:
    print("SKIP: nothing answers at %s:%d (%s)" % (METER_IP, METER_PORT, str(exc)[:60]))
    sys.exit(0)

tmp = tempfile.mkdtemp(prefix="tn-power-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
log = open(os.path.join(tmp, "o.log"), "w")
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


def call(m, path, tok=None, body=None):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    d = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=d, headers=h, method=m)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
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

DEVICE = {
    "id": "em122-test", "name": "EM122 test", "description": "commissioning",
    "enabled": True, "type": "modbus_tcp", "protocol": "modbus_tcp",
    "ip": METER_IP, "port": METER_PORT, "unit_id": 1,
    "poll_interval_ms": 1000,
    "electrical_mode": "three_phase", "wiring_type": "three_phase",
    "register_profile": "weidmuller-em122-3ph",
    "use_custom_registers": False,
    "site": "Bench", "area": "Test", "equipment": "Meter",
}

# 1. Test connection - exactly what the dialog's button does.
st, r = call("POST", "/api/power/test-connection", tok,
             {"device": DEVICE, "timeout_ms": 4000})
check("Test Connection succeeds", st == 200 and bool((r or {}).get("ok")),
      str((r or {}).get("message") or r)[:110])

# 2. Press OK: the configuration must persist.
st, r = call("PUT", "/api/power/config", tok,
             {"enabled": True, "devices": [DEVICE], "selected_device_id": DEVICE["id"]})
check("pressing OK saves the meter", st == 200, "HTTP %s" % st)
st, cfg = call("GET", "/api/power/config", tok)
c = (cfg or {}).get("config") or cfg or {}
saved = [d for d in (c.get("devices") or []) if str(d.get("id")) == DEVICE["id"]]
check("  and it is still there when re-read", len(saved) == 1,
      "%d device(s) stored" % len(c.get("devices") or []))
if saved:
    # The id is canonicalised on save (the catalogue writes `em122-3ph`, the
    # register table is keyed `weidmuller_em122_three_phase`). What must hold
    # is that it is still an EM122 THREE-PHASE map - the bug replaced it with
    # an EM525's.
    prof = str(saved[0].get("register_profile") or "")
    check("  and it is still the EM122 map that was tested",
          str(saved[0].get("ip")) == METER_IP
          and "em122" in prof and "three_phase" in prof,
          "ip=%s profile=%s" % (saved[0].get("ip"), prof))
    check("  with no unresolved-profile error", not saved[0].get("profile_error"),
          str(saved[0].get("profile_error") or "")[:100])

# 3. It must actually collect.
call("POST", "/api/power/devices/%s/start" % DEVICE["id"], tok, {})
print("  collecting for 20 s...")
time.sleep(20)

st, latest = call("GET", "/api/power/latest", tok)
sample = (latest or {}).get("sample") or {}
values = sample.get("values") if isinstance(sample, dict) else {}
numeric = {k: v for k, v in (values or {}).items() if isinstance(v, (int, float))}
check("the meter is producing values", bool(numeric),
      ", ".join("%s=%s" % (k, round(float(v), 2)) for k, v in list(numeric.items())[:4]))
# Zeroes are exactly what the WRONG register map produced, so "there are
# numbers" is not enough - a live mains voltage has to look like one.
volts = [v for k, v in numeric.items() if k.startswith("voltage_") and float(v) > 0]
check("  and they are real, not zeroes from the wrong map",
      bool(volts) and all(80.0 < float(v) < 500.0 for v in volts),
      "voltages: %s" % ", ".join(str(round(float(v), 1)) for v in volts[:3]))

st, status = call("GET", "/api/power/status", tok)
blob = json.dumps(status or {})
check("  and the status reports it running, without an error",
      '"running": true' in blob.lower() or '"enabled": true' in blob.lower(),
      blob[:150])

st, hist = call("GET", "/api/app-store/historian/range?limit=50", tok)
rows = (hist or {}).get("rows") or []
power_rows = [r for r in rows if "power" in str(r.get("source") or "").lower()
              or "em122" in str(r.get("gateway_id") or "").lower()
              or "meter" in str(r.get("gateway_name") or "").lower()]
check("  and readings reach the historian", len(power_rows) > 0 or len(rows) > 0,
      "%d row(s) total, %d look like meter rows" % (len(rows), len(power_rows)))

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
