# -*- coding: utf-8 -*-
"""The SHIPPED binary must collect into the historian, not just the source tree.

Every test in this repo that spawns `python -m app` proves the source is
correct. It does not prove the artefact the customer installs is correct - a
PyInstaller bundle can miss a module, and the 2026-08-21 historian defect
shipped precisely because the gate was never run against a built app.

This drives the packaged trustnode-service.exe on a throwaway data dir with a
fake ifm block in front of it, and asserts rows land in the local historian.
It needs no hardware, no UAC and no desktop session, so it can run on every
build. It is NOT a substitute for the full release gate.
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Defaults to the packaged app; TN_SERVICE_EXE points it at backend/dist so the
# bundle can be checked before electron-builder has finished wrapping it.
EXE = os.environ.get("TN_SERVICE_EXE") or os.path.join(
    ROOT, "desktop", "dist", "win-unpacked", "resources",
    "backend", "trustnode-service.exe")
PORT = "8099"
API = "http://127.0.0.1:" + PORT
FAILS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:110]) if detail else ""
    print("  {0:56s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


if not os.path.isfile(EXE):
    print("  packaged service not found - build first:  cd desktop && npm run dist")
    print("  looked for: " + EXE)
    sys.exit(2)
built = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(EXE)))
print("  packaged service: {0}  (built {1})".format(os.path.basename(EXE), built))

# --- a fake AL4022-style block --------------------------------------------
INPUTS = {"/io/port[1]/pin2/digital_input/getdata": 1,
          "/io/port[2]/pin4/digital_input/getdata": 0}


def _tree():
    ports = []
    for p in (1, 2):
        pins = []
        for pin in ("pin2", "pin4"):
            pins.append({"identifier": pin, "type": "structure",
                         "subs": [{"identifier": "digital_input", "type": "data",
                                   "profiles": ["processdata"],
                                   "subs": [{"identifier": "getdata",
                                             "type": "service"}]}]})
        ports.append({"identifier": "port[{0}]".format(p), "type": "structure",
                      "subs": pins})
    return {"identifier": "fake-al4022", "type": "device",
            "subs": [{"identifier": "getdatamulti", "type": "service"},
                     {"identifier": "querytree", "type": "service"},
                     {"identifier": "io", "type": "structure", "subs": ports}]}


class FakeBlock(BaseHTTPRequestHandler):
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
        v = INPUTS.get(self.path)
        self._send({"cid": -1, "data": {"value": v},
                    "adr": self.path, "code": 200 if v is not None else 404})

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
            data = {}
            for a in (body.get("data") or {}).get("datatosend") or []:
                v = INPUTS.get(a)
                data[a] = {"data": v, "code": 200 if v is not None else 404}
            self._send({"cid": 1, "data": data, "code": 200})
            return
        self._send({"cid": 1, "code": 400}, status=400)


block = HTTPServer(("127.0.0.1", 0), FakeBlock)
threading.Thread(target=block.serve_forever, daemon=True).start()
HOST, HPORT = block.server_address
print("  fake ifm block on {0}:{1}".format(HOST, HPORT))

# --- run the packaged service ---------------------------------------------
tmp = tempfile.mkdtemp(prefix="tn-packaged-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
proc = subprocess.Popen([EXE], cwd=os.path.dirname(EXE), env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
lines = []
threading.Thread(target=lambda: [lines.append(l.decode("utf-8", "replace").rstrip())
                                 for l in iter(proc.stdout.readline, b"")],
                 daemon=True).start()

up = False
t0 = time.time()
for _ in range(90):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read()
        up = True
        break
    except Exception:
        time.sleep(2)
check("the packaged service boots and serves /api/health", up,
      "{0:.1f}s".format(time.time() - t0))


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
        proc.wait(timeout=25)
    except Exception:
        proc.kill()
    block.shutdown()
    sys.exit(code)


if not up:
    print("\n  service output (tail):")
    for l in lines[-15:]:
        print("    " + l)
    finish(2)

st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
admin = (b or {}).get("token") if isinstance(b, dict) else None
check("admin login against the packaged service", st == 200 and bool(admin))
if not admin:
    finish(2)

st, scan = call("POST", "/api/plc/ifm/scan-ports", admin,
                {"plc_ip": HOST, "http_port": HPORT, "variant": "auto"})
pts = (scan or {}).get("datapoints") or []
check("the packaged driver discovers the block", st == 200 and len(pts) >= 2, len(pts))

st, r = call("POST", "/api/plc/gateways/start", admin, {
    "gateway_id": "gw-packaged",
    "config": {"gateway_type": "ifm_iolink", "name": "Packaged block",
               "device_name": "AL4022", "plc_ip": HOST, "ifm_http_port": HPORT,
               "ifm_datapoints": pts, "tags": [p["name"] for p in pts],
               "interval_ms": 1000, "site": "Limerick", "area": "LineA",
               "equipment": "Block"}})
check("the gateway starts", st == 200, "status={0} {1}".format(st, str(r)[:110]))

time.sleep(8)
st, hist = call("GET", "/api/app-store/historian?limit=200", admin)
rows = [x for x in ((hist or {}).get("rows") or [])
        if str(x.get("gateway_id")) == "gw-packaged"]
check("rows reach the historian IN THE SHIPPED BUILD", len(rows) > 0, len(rows))
# the read API returns the column as `ts` (the insert dict calls it ts_utc)
stamps = sorted(set(str(x.get("ts") or x.get("ts_utc")) for x in rows))
check("  and they keep advancing (not one cycle repeated)", len(stamps) >= 2,
      "{0} distinct timestamp(s)".format(len(stamps)))

# the defect logged this every cycle while looking healthy
bad = [l for l in lines if "historian-write-fail" in l or "historian-buffer" in l]
check("no historian buffering warnings", not bad, bad[:1])

st, status = call("GET", "/api/plc/gateways/status", admin)


def _find(payload):
    if isinstance(payload, dict):
        for key in ("gateways", "items", "data"):
            if isinstance(payload.get(key), list):
                return payload[key]
        if "gw-packaged" in payload and isinstance(payload["gw-packaged"], dict):
            return [dict(payload["gw-packaged"], gateway_id="gw-packaged")]
    return payload if isinstance(payload, list) else []


row = next((g for g in _find(status)
            if str(g.get("gateway_id") or g.get("id")) == "gw-packaged"), {})
wc = row.get("historian_write_count")
check("the durable write counter moves (the footer W: value)",
      isinstance(wc, int) and wc > 0, wc)

call("POST", "/api/plc/gateways/stop", admin, {"gateway_id": "gw-packaged"})

# --- a locked-out operator must be able to recover on the SHIPPED build -----
# New routes and the public-path allowlist both have to survive packaging; if
# either is missing the operator meets a 401 with no account to authenticate
# with, which is the dead end this whole flow exists to remove.
print()
st, r = call("GET", "/api/auth/recovery-status")
check("recovery status is public in the shipped build", st == 200,
      "status={0} {1}".format(st, str(r)[:100]))

st, req2 = call("POST", "/api/auth/local-recovery/request")
rpath = (req2 or {}).get("recovery_file") or ""
check("the shipped build writes a recovery code file",
      st == 200 and os.path.isfile(rpath), "status={0} {1}".format(st, rpath))

rcode = ""
if rpath and os.path.isfile(rpath):
    with open(rpath, encoding="utf-8") as fh:
        for line in fh.read().splitlines():
            if line.lower().startswith("recovery code:"):
                rcode = line.split(":", 1)[1].strip()

# A weak password here would also mean the bundle predates the policy fix,
# so this doubles as a staleness detector for the packaged backend.
st, r = call("POST", "/api/auth/local-recovery/complete",
             body={"code": rcode, "username": "rescue-packaged",
                   "password": "Passw0rd"})
check("the shipped build enforces the admin password policy", st == 400,
      "status={0} {1}".format(st, str(r)[:100]))

st, r = call("POST", "/api/auth/local-recovery/complete",
             body={"code": rcode, "username": "rescue-packaged",
                   "password": "PackagedPass!42"})
check("the right code creates an admin in the shipped build", st == 200,
      "status={0} {1}".format(st, str(r)[:110]))
st, r = call("POST", "/api/auth/login",
             body={"username": "rescue-packaged", "password": "PackagedPass!42"})
check("  and that admin can sign in", st == 200, "status={0}".format(st))

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
