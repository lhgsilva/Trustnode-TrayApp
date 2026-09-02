# -*- coding: utf-8 -*-
"""A gateway reading nothing but nulls must not report itself healthy.

2026-08-29. The ifm block dropped off the network - both transports timing out,
100% ping loss, while the PLC and meter on the same subnet answered in 1 ms.
The driver did the right thing and wrote `value=null, quality=0, BAD` for every
tag instead of repeating the last good reading.

The STATUS was the problem:

    running                  True
    last_error               None
    historian_write_count    97 880   (still climbing)

Healthy by every field the UI reads, on a device that was physically
unplugged. `historian_write_count` counts ROWS, and a BAD row is still a row -
the same shape as the 2026-08-21 distribution wedge, where a counter measured
activity rather than success.

This points a gateway at an address that cannot answer and asserts the status
says so. It needs no hardware: the whole point is that nothing is there.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = "8142"
API = "http://127.0.0.1:" + PORT
GID = "gw-allbad-test"
FAILS: list[str] = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:170]) if detail else ""
    print("  {0:56s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


# --- the rule lives at the convergence point, not in one driver ----------
print("[the check is protocol-agnostic]")
src = io.open(os.path.join(ROOT, "backend", "app", "services", "plc_manager.py"),
              encoding="utf-8", errors="replace").read()
check("the all-BAD streak is counted once, for every protocol",
      src.count("self._all_bad_cycles += 1") == 1,
      "an unreachable Modbus meter deserves the same treatment as an ifm block")
check("  a good reading clears it",
      "self._all_bad_cycles = 0" in src)
check("  one bad cycle is not reported as a fault",
      "self._all_bad_cycles >= 3" in src,
      "a reboot or a nudged cable is a blip; a streak is a fault")

# A dead device times out on every read, so its cycle ALWAYS overruns and the
# cadence warning always fires. If that warning outranks the fault, the operator
# is told to "raise the interval or reduce tag count" - the wrong knob entirely.
check("  a real fault outranks the cadence warning",
      'if not self.last_error or "cadence" in self.last_error.lower():' in src,
      "`not in` here overwrites everything that ISN'T a cadence warning")

tmp = tempfile.mkdtemp(prefix="tn-allbad-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
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
        return e.code, None
    except Exception as e:
        return 0, str(e)[:150]


def finish(code):
    call("POST", "/api/plc/gateways/stop", tok, {"gateway_id": GID})
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    return code


print()
print("[a gateway pointed at an address that cannot answer]")
tok = None
check("the app started", up)
if not up:
    print(open(os.path.join(tmp, "o.log")).read()[-1500:])
    sys.exit(2)

st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
tok = (b or {}).get("token")
if not tok:
    check("login", False, st)
    sys.exit(finish(2))

# 198.51.100.9 is TEST-NET-2 (RFC 5737): reserved for documentation, routable
# nowhere. A read against it always fails, which is exactly the condition.
#
# EtherNet/IP deliberately, not Modbus. The Modbus driver RAISES on a failed
# connect, so last_error was already populated by the exception path - that
# case never had the bug. The EtherNet/IP reader returns a BAD reading per
# signal instead of raising, so the cycle "succeeds", rows are written, and
# nothing set last_error. That is the ifm block's exact failure mode.
st, r = call("POST", "/api/plc/gateways/start", tok, {
    "gateway_id": GID,
    "config": {
        "gateway_type": "ethernet_ip",
        "name": "Unreachable device", "device_name": "TEST-NET",
        "plc_ip": "198.51.100.9", "eip_input_assembly": 100, "eip_slot": 0,
        "eip_signals": [
            {"name": "Port7_Pin4", "byte_offset": 0, "kind": "BOOL", "bit": 6},
            {"name": "Port8_Pin2", "byte_offset": 1, "kind": "BOOL", "bit": 7},
        ],
        "tags": ["Port7_Pin4", "Port8_Pin2"],
        "interval_ms": 1000,
        "equipment": "T", "site": "T", "area": "T",
    }})
check("the gateway starts", st == 200 and (r or {}).get("started") is True, str(r)[:110])
if not (r or {}).get("started"):
    sys.exit(finish(2))

print("  letting it fail for 25 s...")
time.sleep(25)

st, statuses = call("GET", "/api/plc/gateways/status", tok)
row = next((g for g in (statuses if isinstance(statuses, list) else [])
            if str(g.get("gateway_id")) == GID), None)
check("the gateway appears in status", row is not None)
if row:
    err = str(row.get("last_error") or "")
    print("     last_error   : %s" % (err[:150] or "(empty)"))
    print("     all_bad_cycles: %s" % row.get("all_bad_cycles"))
    check("an unreachable device is REPORTED as unreachable", bool(err),
          "this was empty while 97 880 BAD rows were written")
    check("  the message names the address", "198.51.100.9" in err, err[:90])
    check("  and says what to check",
          "unreachable" in err.lower() or "cabling" in err.lower(), err[:110])
    check("  the streak is counted", int(row.get("all_bad_cycles") or 0) >= 3,
          row.get("all_bad_cycles"))

    # The card's small "Last error" line is not enough - the operator needs the
    # "PLC unreachable" fault banner. That banner is driven by a substring
    # match in the frontend, so the worker's wording and the matcher are one
    # contract split across two languages. Assert the REAL message satisfies
    # the REAL matcher, rather than checking each side says something
    # plausible on its own.
    app = io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
                  encoding="utf-8", errors="replace").read()
    fn = app[app.index("function isPlcUnreachableError"):]
    fn = fn[:fn.index(chr(10) + "}")].lower()
    clauses = re.findall(r'm\.includes\("([^"]+)"\)', fn)
    check("  the UI's unreachable banner recognises it",
          any(c in err.lower() for c in clauses),
          "matcher clauses: %s" % (clauses,))

# The other half: rows are still written, and still BAD. Reporting the fault
# must not mean discarding the evidence.
st, live = call("GET", "/api/app-store/live?limit=2000", tok)
rows = [x for x in ((live or {}).get("rows") or []) if str(x.get("gateway_id")) == GID]
check("the readings are still recorded, as BAD", bool(rows),
      "%d tag(s) in the live cache" % len(rows))
if rows:
    check("  with null values, not a stale last-known number",
          all(x.get("value") is None for x in rows),
          "a repeated last value would read as a live device")
    check("  and BAD quality", all(int(x.get("quality") or 0) == 0 for x in rows))

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
