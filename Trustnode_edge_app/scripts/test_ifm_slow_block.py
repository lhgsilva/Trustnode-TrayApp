# -*- coding: utf-8 -*-
"""A real ifm block with many tags must still collect - the 2026-08-26 failure.

The operator's AL-series block was configured with ~28 datapoints. It showed
RUNNING with W:0, every Last Value blank, and the UI reported "no fresh sample
or check arrived in 90 s". Nothing in the product said why.

Mechanism: the driver asked for all 28 addresses in ONE getdatamulti. When that
did not come back usable it fell back to reading them ONE AT A TIME, with no
bound on the total. 28 requests x the per-request timeout cannot fit in a 1 s
cycle, so the collection loop's own 8 s cap fired EVERY cycle, orphaned the
executor, and stamped no progress. No cycle ever completed, so there were no
rows and no error an operator could act on.

These tests pin the three properties that make that impossible:
  * a read is bounded by a deadline the caller sets, always;
  * slow or dead addresses cost their own value, not the whole cycle;
  * a partly-readable block still produces rows, and says what was missed.

The fake block is deliberately hostile - it refuses getdatamulti and stalls on
some addresses - because that is what the real one appeared to do.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ.setdefault("TRUSTNODE_SKIP_DOTENV", "1")
PORT = "8103"
API = "http://127.0.0.1:" + PORT
FAILS = []


def _port_open(host, port, timeout=1.0):
    import socket as _s
    try:
        with _s.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:110]) if detail else ""
    print("  {0:58s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


# 8 ports x 2 pins = 16 digital inputs, plus the device-level leaves a real
# block reports through querytree. 28 datapoints, same as the operator's.
INPUTS = {}
for _p in range(1, 9):
    for _pin in ("pin2", "pin4"):
        INPUTS["/io/port[{0}]/{1}/digital_input/getdata".format(_p, _pin)] = _p % 2
EXTRA = ["temperature", "voltage_us", "current_us", "supervisionstatus_us",
         "connectionstatus", "maincounter_value", "batchcounter_value",
         "direction", "disable", "reset", "mode", "status"]
for _i, _leaf in enumerate(EXTRA):
    INPUTS["/processdatamaster/{0}/getdata".format(_leaf)] = 100 + _i

# these stall past any sane per-request timeout, like an unplugged port or a
# parameter the firmware refuses to answer promptly
STALLED = {"/processdatamaster/reset/getdata",
           "/processdatamaster/disable/getdata"}

GET_DELAY_S = 0.12
REQUESTS = {"multi": 0, "get": 0}
_lock = threading.Lock()


def _tree():
    ports = []
    for p in range(1, 9):
        pins = []
        for pin in ("pin2", "pin4"):
            pins.append({"identifier": pin, "type": "structure",
                         "subs": [{"identifier": "digital_input", "type": "data",
                                   "profiles": ["processdata"],
                                   "subs": [{"identifier": "getdata",
                                             "type": "service"}]}]})
        ports.append({"identifier": "port[{0}]".format(p), "type": "structure",
                      "subs": pins})
    return {"identifier": "fake-al", "type": "device",
            "subs": [{"identifier": "querytree", "type": "service"},
                     {"identifier": "io", "type": "structure", "subs": ports}]}


class Hostile(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        with _lock:
            REQUESTS["get"] += 1
        if self.path in STALLED:
            time.sleep(30)          # never answers in time
            return
        time.sleep(GET_DELAY_S)
        v = INPUTS.get(self.path)
        self._send({"cid": -1, "data": {"value": v}, "adr": self.path,
                    "code": 200 if v is not None else 404})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode() or "{}")
        adr = body.get("adr")
        if adr == "gettree":
            self._send({"cid": -1, "code": 200, "data": _tree()})
            return
        if adr == "querytree":
            self._send({"cid": -1, "code": 200,
                        "data": {"subs": [{"adr": a} for a in sorted(INPUTS)]}})
            return
        if adr == "/getdatamulti":
            # the hostile part: this block simply will not do it
            with _lock:
                REQUESTS["multi"] += 1
            self._send({"cid": 1, "code": 400}, status=400)
            return
        self._send({"cid": 1, "code": 400}, status=400)


class Threaded(ThreadingMixIn, HTTPServer):
    daemon_threads = True


block = Threaded(("127.0.0.1", 0), Hostile)
threading.Thread(target=block.serve_forever, daemon=True).start()
HOST, HPORT = block.server_address
print("  hostile block on {0}:{1} - {2} datapoints, getdatamulti refused, "
      "{3} stalled".format(HOST, HPORT, len(INPUTS), len(STALLED)))

from app.drivers.ifm_iolink import (  # noqa: E402
    IfmMasterClient, datapoints_from_config)

print("\n[the driver finishes inside its budget]")
cli = IfmMasterClient(host=HOST, port=HPORT, timeout_s=1.0)
disc = cli.discover_datapoints(variant="io_module", port_count=8)
points = datapoints_from_config(disc.get("datapoints") or [])
check("discovery finds every datapoint", len(points) == len(INPUTS), len(points))

BUDGET = 3.0
cli.begin_read(BUDGET)
t0 = time.monotonic()
rows = cli.read_datapoints(points)
elapsed = time.monotonic() - t0
cli.end_read()

check("the read RETURNS (it used to overrun the cycle cap)", bool(rows), len(rows))
check("  and finishes inside the budget", elapsed <= BUDGET + 1.5,
      "{0:.2f}s for {1} datapoints (budget {2}s)".format(elapsed, len(points), BUDGET))

good = [r for r in rows if r.get("quality")]
bad = [r for r in rows if not r.get("quality")]
check("  most values come back GOOD", len(good) >= len(points) - len(STALLED) - 1,
      "{0} good / {1} bad of {2}".format(len(good), len(bad), len(rows)))
check("  a stalled address costs only ITSELF", len(bad) <= len(STALLED) + 1,
      [r["name"] for r in bad][:6])
check("  every datapoint is accounted for", len(rows) == len(points),
      "{0} rows for {1} points".format(len(rows), len(points)))
check("  and the driver explains what it dropped",
      bool(getattr(cli, "last_transport_note", "")) if bad else True,
      getattr(cli, "last_transport_note", "") or "(no note)")

# the fallback must be concurrent, not one-at-a-time
print("\n[the fallback is concurrent]")
sequential_estimate = len(points) * GET_DELAY_S
check("  it beats a sequential read comfortably", elapsed < sequential_estimate,
      "{0:.2f}s vs {1:.2f}s sequential".format(elapsed, sequential_estimate))

# a second read must reuse the connection, not rebuild an opener per request
print("\n[the client is reused]")
op1 = cli._opener()
op2 = cli._opener()
check("the HTTP opener is built once, not per request", op1 is op2)

# --- end to end ------------------------------------------------------------
print("\n[END TO END - the gateway collects from a hostile block]")
tmp = tempfile.mkdtemp(prefix="tn-ifmslow-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
# TN_SERVICE_EXE runs the PACKAGED backend instead of the source tree, so the
# fix can be proven in the artefact that actually ships.
_exe = os.environ.get("TN_SERVICE_EXE")
if _exe and os.path.isfile(_exe):
    print("  backend: packaged {0}".format(os.path.basename(_exe)))
    proc = subprocess.Popen([_exe], cwd=os.path.dirname(_exe), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
else:
    proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=os.path.join(ROOT, "backend"),
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(60):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read()
        break
    except Exception:
        time.sleep(2)


def call(method, path, token=None, body=None, timeout=90):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:400]
    except Exception as e:
        return 0, str(e)[:300]


def finish(code):
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    block.shutdown()
    sys.exit(code)


st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
admin = (b or {}).get("token") if isinstance(b, dict) else None
check("admin login", st == 200 and bool(admin))
if not admin:
    finish(2)

# configured the way the operator did: tag NAMES only, 1 s interval
names = [d["name"] for d in (disc.get("datapoints") or [])]
st, r = call("POST", "/api/plc/gateways/start", admin, {
    "gateway_id": "gw-slow",
    "config": {"gateway_type": "ifm_iolink", "name": "IFM Block",
               "device_name": "IMF", "plc_ip": HOST, "ifm_http_port": HPORT,
               "tags": names, "interval_ms": 1000,
               "site": "Limerick", "area": "LineA", "equipment": "Block"}})
check("the gateway starts with {0} tags".format(len(names)), st == 200,
      "status={0} {1}".format(st, str(r)[:110]))

time.sleep(12)
st, hist = call("GET", "/api/app-store/historian?limit=400", admin)
rows = [x for x in ((hist or {}).get("rows") or [])
        if str(x.get("gateway_id")) == "gw-slow"]
check("IT COLLECTS (this is what was broken)", len(rows) > 0, len(rows))
stamps = sorted(set(str(x.get("ts")) for x in rows))
check("  across multiple cycles", len(stamps) >= 2,
      "{0} distinct cycle(s)".format(len(stamps)))
good_rows = [x for x in rows if str(x.get("quality_label")) == "GOOD"]
check("  with real values, not just BAD markers", len(good_rows) > 0,
      "{0} GOOD of {1}".format(len(good_rows), len(rows)))
tags_seen = set(str(x.get("tag")) for x in good_rows)
check("  covering the digital inputs", any(t.startswith("Port") for t in tags_seen),
      sorted(tags_seen)[:5])

# the worker must be alive and honest about it
st, status = call("GET", "/api/plc/gateways/status", admin)


def _find(payload):
    if isinstance(payload, dict):
        for key in ("gateways", "items", "data"):
            if isinstance(payload.get(key), list):
                return payload[key]
        if "gw-slow" in payload and isinstance(payload["gw-slow"], dict):
            return [dict(payload["gw-slow"], gateway_id="gw-slow")]
    return payload if isinstance(payload, list) else []


row = next((g for g in _find(status)
            if str(g.get("gateway_id") or g.get("id")) == "gw-slow"), {})
wc = row.get("historian_write_count")
check("the write counter moves (footer W: stops showing 0)",
      isinstance(wc, int) and wc > 0, wc)
check("  and a partly-readable block reports WHY, not silence",
      bool(str(row.get("last_error") or "")) or len(good_rows) == len(names),
      str(row.get("last_error") or "(none)")[:90])

call("POST", "/api/plc/gateways/stop", admin, {"gateway_id": "gw-slow"})

# --- the block was DOWN when the gateway started ---------------------------
# Previously a failed discovery cached an empty list forever, so the gateway
# stayed dead until someone stopped and started it by hand - long after the
# block was reachable again.
print("\n[a block that is down at start must recover on its own]")
block.shutdown()
# shutdown() only stops serve_forever; the LISTENING SOCKET stays open, so a
# rebind on the same port silently keeps serving from the dead one.
block.server_close()
time.sleep(1.0)
check("  the block is genuinely unreachable now",
      not _port_open(HOST, HPORT), "{0}:{1}".format(HOST, HPORT))

st, r = call("POST", "/api/plc/gateways/start", admin, {
    "gateway_id": "gw-late",
    "config": {"gateway_type": "ifm_iolink", "name": "Late block",
               "device_name": "IMF", "plc_ip": HOST, "ifm_http_port": HPORT,
               "tags": names, "interval_ms": 1000,
               "site": "Limerick", "area": "LineA", "equipment": "Block"}})
check("a gateway starts even though the block is unreachable", st == 200,
      "status={0} {1}".format(st, str(r)[:100]))
time.sleep(4)

st, hist = call("GET", "/api/app-store/historian?limit=200", admin)
early = [x for x in ((hist or {}).get("rows") or [])
         if str(x.get("gateway_id")) == "gw-late"]
check("  it collects nothing while the block is down", len(early) == 0, len(early))

# bring the block back on the same port
late = Threaded((HOST, HPORT), Hostile)
threading.Thread(target=late.serve_forever, daemon=True).start()
check("  the block is answering again", _port_open(HOST, HPORT),
      "{0}:{1}".format(HOST, HPORT))

# discovery retries on a 30 s cadence, so allow for it
deadline = time.time() + 75
rows_late = []
while time.time() < deadline:
    time.sleep(5)
    st, hist = call("GET", "/api/app-store/historian?limit=400", admin)
    rows_late = [x for x in ((hist or {}).get("rows") or [])
                 if str(x.get("gateway_id")) == "gw-late"]
    if rows_late:
        break
check("  IT RECOVERS BY ITSELF once the block answers", len(rows_late) > 0,
      "{0} row(s)".format(len(rows_late)))
call("POST", "/api/plc/gateways/stop", admin, {"gateway_id": "gw-late"})
try:
    late.shutdown()
except Exception:
    pass

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
