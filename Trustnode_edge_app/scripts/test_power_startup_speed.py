# -*- coding: utf-8 -*-
"""Configured meters must be on screen within seconds of the app opening.

2026-08-26, reported: "when I installed and open the new app it did not load
the meter configured from the database straight away... it is taking minutes so
the data is loaded from the database after startup."

The meters were in the database the whole time. PowerManager loaded them
through get_bootstrap(), which assembles EVERY config domain behind the same
app_store lock that deferred outbox init, cloud sync, live-sync and the
retention scheduler all contend for during start-up. A settings domain is one
row; it should never wait behind that.

Also covered: a meter that has not finished its first poll must report
"starting", not a failure - the UI rendered connected=False as "Device Fails"
before a single Modbus request had been sent.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = "8113"
API = "http://127.0.0.1:" + PORT
FAILS = []
# Generous: the point is "seconds, not minutes". A cold start on a slow disk
# should still be well inside this.
BUDGET_S = float(os.environ.get("TN_STARTUP_BUDGET_S", "15"))


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:115]) if detail else ""
    print("  {0:56s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


TMP = tempfile.mkdtemp(prefix="tn-startup-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=TMP,
           TRUSTNODE_APP_STORE_PATH=os.path.join(TMP, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
_exe = os.environ.get("TN_SERVICE_EXE")
CMD = [_exe] if (_exe and os.path.isfile(_exe)) else [sys.executable, "-m", "app"]
CWD = os.path.dirname(_exe) if (_exe and os.path.isfile(_exe)) else os.path.join(ROOT, "backend")


def call(method, path, token=None, body=None, timeout=60):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300]
    except Exception as e:
        return 0, str(e)[:300]


def boot():
    p = subprocess.Popen(CMD, cwd=CWD, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(90):
        try:
            urllib.request.urlopen(API + "/api/health", timeout=3).read()
            return p
        except Exception:
            time.sleep(1)
    return p


def stop(p):
    try:
        p.terminate()
        p.wait(timeout=25)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


METER = {
    "id": "PGE", "name": "PGE Meter", "enabled": True, "type": "modbus_tcp",
    "protocol": "modbus_tcp", "ip": "192.168.1.117", "port": 502, "unit_id": 1,
    "poll_interval_ms": 1000, "electrical_mode": "three_phase",
}

# --- first run: configure a meter ------------------------------------------
print("[configuring a meter, then restarting the app]")
p1 = boot()
st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
tok = (b or {}).get("token") if isinstance(b, dict) else None
check("admin login", st == 200 and bool(tok))
if not tok:
    stop(p1)
    sys.exit(2)
st, r = call("PUT", "/api/power/config", tok,
             {"enabled": True, "devices": [METER], "selected_device_id": "PGE"})
check("the meter is saved", st == 200, "status={0}".format(st))
stop(p1)

# --- restart, and time how long the meters take to appear ------------------
print()
print("[restart - how long until the meters are on screen?]")
t0 = time.time()
p2 = boot()
health_s = time.time() - t0

tok2 = None
found_s = None
devices = []
deadline = time.time() + BUDGET_S
while time.time() < deadline:
    if not tok2:
        st, b = call("POST", "/api/auth/login",
                     body={"username": "admin", "password": "admin"})
        tok2 = (b or {}).get("token") if isinstance(b, dict) else None
        if not tok2:
            time.sleep(0.2)
            continue
    st, c = call("GET", "/api/power/config", tok2)
    devices = ((c or {}).get("config") or {}).get("devices") or []
    if devices:
        found_s = time.time() - t0
        break
    time.sleep(0.2)

print("  backend answered /api/health after   : {0:.1f}s".format(health_s))
print("  meters visible after                 : {0}".format(
    "{0:.1f}s".format(found_s) if found_s else "NOT within {0}s".format(BUDGET_S)))
check("THE METER IS LOADED FROM THE DATABASE", bool(devices),
      [d.get("name") for d in devices])
check("  and it is there in seconds, not minutes",
      found_s is not None and found_s <= BUDGET_S,
      "{0:.1f}s".format(found_s) if found_s else "timeout")
check("  within ~2s of the backend answering at all",
      found_s is not None and (found_s - health_s) <= 2.5,
      "{0:.1f}s after health".format((found_s - health_s) if found_s else -1))
if devices:
    check("  with its address intact",
          str(devices[0].get("ip")) == "192.168.1.117", devices[0].get("ip"))

# --- a meter that has not polled yet is STARTING, not failed ---------------
print()
print("[a meter that has not finished its first poll]")
# 192.168.1.117 is unreachable from this test machine, so the poll will not
# complete - which is exactly the window the UI used to call "Device Fails".
st, status = call("GET", "/api/power/status", tok2)
# the payload is {"ok":..., "status": {"devices": [...]}}
inner = (status or {}).get("status") or {}
rows = (inner.get("devices") if isinstance(inner, dict) else None) or []
mine = next((r for r in rows if str(r.get("device_id")) == "PGE"), {})
check("the meter appears in status immediately", bool(mine), list(mine)[:6])
if mine:
    reported_starting = bool(mine.get("starting"))
    ever_polled = bool(str(mine.get("last_poll_utc") or "").strip())
    check("  it reports a 'starting' state rather than a failure",
          reported_starting or ever_polled,
          "starting={0} last_poll={1}".format(mine.get("starting"),
                                              mine.get("last_poll_utc")))

# the UI must render that state instead of "Device Fails"
src = open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
           encoding="utf-8", errors="replace").read()
check("  and the UI shows it as Starting, not Device Fails",
      "Starting..." in src and "st.starting" in src)

# --- history must be readable with NOTHING running -------------------------
# "even though the gateway might not be running, the last live data from the
# charts, power overview and historian still should be there".
print()
print("[history is readable straight after a restart, nothing running]")
import sqlite3 as _sq
from datetime import datetime as _dt, timedelta as _td, timezone as _tz
_end = _dt.now(_tz.utc).replace(microsecond=0)
_con = _sq.connect(os.path.join(TMP, "s.db"), timeout=30)
_rows = []
_t = _end - _td(minutes=10)
_n = 0
while _t < _end:
    _stamp = _t.strftime("%Y-%m-%d %H:%M:%S.000")
    for _tag in ("voltage_l1_v", "current_l1_a", "insight.live_kw"):
        _rows.append(("default", _stamp, "PGE", "PGE Meter", "PGE Meter",
                      "192.168.1.117", "Power Management", _tag,
                      100.0 + (_n % 10), None, "REAL", 192, "GOOD",
                      "power_insight" if _tag.startswith("insight.") else "power_modbus",
                      _stamp))
    _n += 1
    _t += _td(seconds=1)
_con.executemany(
    "INSERT INTO historian_readings (tenant_id, ts_utc, gateway_id, gateway_name,"
    " device_name, plc_ip, database_name, tag_name, value, value_text, data_type,"
    " quality, quality_label, source, created_utc)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _rows)
_con.commit()
_con.close()
stop(p2)

# restart with the meter unreachable - nothing will be collecting
_t0 = time.time()
p3 = boot()
_health = time.time() - _t0
st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
tok3 = (b or {}).get("token") if isinstance(b, dict) else None
st, hist = call("GET", "/api/power/history?limit=3000", tok3)
hrows = (hist or {}).get("rows") or []
elapsed = time.time() - _t0
print("  history readable after               : {0:.1f}s ({1} row(s))".format(elapsed, len(hrows)))
check("PREVIOUS DATA IS THERE WITH NOTHING RUNNING", len(hrows) > 0, len(hrows))
check("  without waiting on a gateway", (elapsed - _health) <= 3.0,
      "{0:.1f}s after health".format(elapsed - _health))
tags3 = {str(r.get("tag")) for r in hrows}
check("  covering the meter's tags", "voltage_l1_v" in tags3, sorted(tags3)[:4])
stop(p3)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
