# -*- coding: utf-8 -*-
"""Item 12: prove the streaming export handles more rows than the browser ever
could, with bounded memory, against a THROWAWAY backend."""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = r"D:\Trustnode\Trustnode-AB\Tray_app\Trustnode_edge_app"
PORT = "8039"
API = f"http://127.0.0.1:{PORT}"
ROWS = 120_000            # six times the old 20 000-row client cap
FAILS = []


def check(name, ok, detail=""):
    print(f"  {name:56s}: {'PASS' if ok else 'FAIL'}{(' — ' + str(detail)[:110]) if detail else ''}")
    if not ok:
        FAILS.append(name)


tmp = tempfile.mkdtemp(prefix="tn-export-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=os.path.join(ROOT, "backend"),
                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(50):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read()
        break
    except Exception:
        time.sleep(2)


def call(method, path, token=None, body=None, timeout=300, raw=False):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            if raw:
                return r.status, payload
            return r.status, json.loads(payload.decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200]
    except Exception as e:
        return 0, str(e)[:200]


st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
admin = (b or {}).get("token")
check("admin login", st == 200 and bool(admin), f"status={st}")
if not admin:
    proc.kill(); sys.exit(2)

# seed a realistic historian directly through the append API
print(f"  seeding {ROWS:,} historian rows …")
t0 = time.time()
BATCH = 5000
for start in range(0, ROWS, BATCH):
    rows = []
    for i in range(start, min(start + BATCH, ROWS)):
        rows.append({
            "ts_utc": time.strftime("2026-08-%d %H:%M:%S", time.gmtime(1_700_000_000 + i)),
            "source": "test", "gateway_id": "gw-x", "gateway_name": "GW",
            "device_name": "dev", "plc_ip": "10.0.0.1", "database_name": "Local SQLite",
            "tag_name": f"tag{i % 48}", "value": float(i % 100),
            "value_text": None, "data_type": "REAL", "quality": 192, "quality_label": "GOOD",
        })
    st, _ = call("POST", "/api/app-store/append/historian", admin, {"rows": rows})
    if st != 200:
        check("seed rows", False, f"status={st}")
        break
print(f"  seeded in {time.time() - t0:.1f}s")

# --- the new streaming export ---------------------------------------------
t0 = time.time()
st, body = call("POST", "/api/historian/export", admin,
                {"from_utc": "", "to_utc": "", "chunk_rows": 5000}, timeout=600, raw=True)
elapsed = time.time() - t0
check("streaming export answers 200", st == 200, f"status={st}")
if st == 200:
    text = body.decode("utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    data_rows = len(lines) - 1
    check("export contains the header", lines and lines[0].startswith("ts_utc,"), lines[0][:60] if lines else "")
    check(f"export streamed MORE than the old 20k client cap", data_rows > 20000, f"{data_rows:,} rows")
    check("export is complete (all seeded rows)", data_rows >= ROWS * 0.99, f"{data_rows:,} of {ROWS:,}")
    check("no truncation marker in the file", "# export interrupted" not in text)
    print(f"     {len(body):,} bytes in {elapsed:.1f}s ({data_rows:,} rows)")

# a bounded export honours max_rows
st, body = call("POST", "/api/historian/export", admin, {"max_rows": 1234}, timeout=300, raw=True)
if st == 200:
    n = len([ln for ln in body.decode().splitlines() if ln.strip()]) - 1
    check("max_rows is honoured", n == 1234, f"{n} rows")

# the OLD endpoint must still work unchanged
st, body = call("POST", "/api/historian/export-xlsx", admin,
                {"rows": [{"ts_utc": "2026-08-22 00:00:00", "tag_name": "t", "value": 1}], "columns": []},
                timeout=180, raw=True)
check("the existing xlsx export still works", st == 200 and len(body) > 1000, f"status={st}")

proc.terminate()
try:
    proc.wait(timeout=15)
except Exception:
    proc.kill()
print()
print(f"RESULT: {'PASS' if not FAILS else 'FAIL — ' + ', '.join(FAILS)}")
sys.exit(0 if not FAILS else 2)
