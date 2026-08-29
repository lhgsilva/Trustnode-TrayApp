# -*- coding: utf-8 -*-
"""Reading the saved config must not take the global write mutex - and must
still return exactly what was saved.

2026-08-28. The release gate measured `/api/plc/gateways/status` at p50 59 ms /
**p95 6021 ms**; a 24-sample probe reproduced it (p50 56 ms, one call 2535 ms).
The endpoint is fast; it was waiting on `get_bootstrap_scoped`, which for a
"Read-only; never moves data" operation took `AppStore._lock` - the GLOBAL
write mutex - and a write-capable connection, twice. So every config read
queued behind every config write and every historian commit.

The fix was to drop the LOCK. The first attempt also switched to
`_connect_readonly()` - and that caused an outage the same day: its 3 s
busy_timeout is right for a historian page and wrong for a config read against
a 13 GB store at boot, so the read timed out and the callers turned the
exception into "the operator has no gateways".

So the property this file guards is narrow and deliberate:

    no global mutex          (the latency fix - kept)
    NOT the read-only handle (the outage - must never come back)

Config loss has happened twice on this product. A faster read that returns less
is far worse than a slow one, and this test exists to keep that ordering.
Correctness under load is covered by test_scoped_config_survives_load.py.
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
PORT = "8126"
API = "http://127.0.0.1:" + PORT
FAILS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:150]) if detail else ""
    print("  {0:56s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


# --- 1. the source no longer takes the write lock for a read --------------
print("[the read path]")
src = open(os.path.join(ROOT, "backend", "app", "services", "app_store.py"),
           encoding="utf-8", errors="replace").read()
head = src[src.index("def get_bootstrap_scoped"):]
body = head[:head.index("\n    def ", 10)]
check("get_bootstrap_scoped does not take the global lock",
      "with self._lock:" not in body,
      "a pure SELECT must not queue behind every write")
# The read-only handle carries busy_timeout=3000. That bound is right for a
# historian page and wrong for a config read against a 13 GB store at boot -
# it timed out, and the callers turn any exception into "no gateways".
check("  and does NOT use the 3 s read-only handle",
      "self._connect_readonly()" not in body,
      "" if "self._connect_readonly()" not in body
      else "this is the 2026-08-28 outage; a config read needs the 10 s connection")
check("  it keeps the write-capable connection's 10 s timeout",
      body.count("self._connect()") >= 2, body.count("self._connect()"))

# --- 2. it still returns exactly what was saved ---------------------------
print()
print("[what it returns]")
tmp = tempfile.mkdtemp(prefix="tn-cfgread-")
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
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    return code


check("the app is up", up)
if not up:
    print(open(os.path.join(tmp, "o.log")).read()[-1500:])
    sys.exit(finish(2))

st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
tok = (b or {}).get("token")
if not tok:
    check("login", False, st)
    sys.exit(finish(2))

GATEWAYS = [
    {"id": "gw-cfgtest-1", "name": "One", "gateway_type": "allen_bradley",
     "plc_ip": "10.0.0.1", "tags": ["A", "B"], "interval_ms": 1000},
    {"id": "gw-cfgtest-2", "name": "Two", "gateway_type": "ethernet_ip",
     "plc_ip": "10.0.0.2", "tags": ["C"], "interval_ms": 2000,
     "eip_input_assembly": 100},
]
st, _ = call("PUT", "/api/app-store/domain", tok, {
    "domain": "gateway_configurations", "payload": GATEWAYS, "actor": "test"})
check("a config document saves", st == 200, st)

st, boot = call("GET", "/api/app-store/bootstrap", tok)
got = ((boot or {}).get("data") or {}).get("gateway_configurations") or []
check("the scoped read returns every saved gateway",
      len(got) == len(GATEWAYS), "{0} saved, {1} returned".format(len(GATEWAYS), len(got)))
by_id = {str(g.get("id")): g for g in got}
check("  with their fields intact",
      by_id.get("gw-cfgtest-2", {}).get("eip_input_assembly") == 100
      and by_id.get("gw-cfgtest-1", {}).get("tags") == ["A", "B"],
      json.dumps(by_id.get("gw-cfgtest-2"))[:110])

# The endpoint that showed the 6 s spike, read repeatedly. A read that no
# longer takes the write mutex should be consistently quick.
worst = 0.0
for _ in range(12):
    t0 = time.perf_counter()
    call("GET", "/api/plc/gateways/status", tok)
    worst = max(worst, (time.perf_counter() - t0) * 1000)
check("  and /api/plc/gateways/status stays responsive",
      worst < 2000, "worst of 12 calls: {0:.0f} ms".format(worst))

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
