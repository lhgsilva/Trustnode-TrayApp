# -*- coding: utf-8 -*-
"""A power meter set to 1 s must land a row every 1 s.

2026-08-26, reported from the field: "it is not collecting every second as set
in the gateway interval, the historian is showing a gap of 4 s, charts are not
properly updating."

A gap can come from three different places, and they need different fixes:
  * the poll itself overruns the interval, so the loop SKIPS slots
    (_run_device_loop advances next_due past `finished`);
  * the writer queue fills and _enqueue_rows DROPS the oldest batch;
  * collection is fine and only the read-back/chart is thin.

So this measures the real thing end to end - a fake Modbus meter, the real
power manager, the real historian - and then reports which of the three it was.
It needs no hardware.
"""
import io
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = "8105"
API = "http://127.0.0.1:" + PORT
FAILS = []
# How long a meter takes to answer one Modbus request. A real meter on a busy
# line is not instant; 25 ms is realistic and still leaves room at 1 s.
RESPONSE_DELAY_S = float(os.environ.get("TN_METER_DELAY_S", "0.025"))
RUN_S = int(os.environ.get("TN_POWER_RUN_S", "30"))


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:120]) if detail else ""
    print("  {0:56s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


# --------------------------------------------------------------- fake meter
# Minimal Modbus TCP: function 3 (read holding registers) only, which is what
# the power manager uses. Registers hold a plausible single-phase meter.
# The Weidmuller EM525 profile reads INPUT registers (function 4) in the
# 19000 range, as float32 pairs. Cover that span with a plausible value.
REGISTERS = {}
for _a in range(18990, 19120, 2):
    REGISTERS[_a] = 0x4348          # float32 big-endian ~200.3
    REGISTERS[_a + 1] = 0x0000

REQUESTS = {"n": 0}
_rlock = threading.Lock()


def _serve(conn):
    try:
        conn.settimeout(20)
        while True:
            head = b""
            while len(head) < 6:
                chunk = conn.recv(6 - len(head))
                if not chunk:
                    return
                head += chunk
            tid, pid, length = struct.unpack(">HHH", head)
            body = b""
            while len(body) < length:
                chunk = conn.recv(length - len(body))
                if not chunk:
                    return
                body += chunk
            unit = body[0]
            func = body[1]
            with _rlock:
                REQUESTS["n"] += 1
            if RESPONSE_DELAY_S:
                time.sleep(RESPONSE_DELAY_S)
            if func in (3, 4):          # holding OR input registers
                start, count = struct.unpack(">HH", body[2:6])
                count = max(1, min(int(count), 125))
                payload = b"".join(
                    struct.pack(">H", int(REGISTERS.get(start + i, 0)) & 0xFFFF)
                    for i in range(count))
                pdu = struct.pack(">BBB", unit, func, len(payload)) + payload
            else:
                pdu = struct.pack(">BBB", unit, func | 0x80, 1)
            conn.sendall(struct.pack(">HHH", tid, pid, len(pdu)) + pdu)
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _accept(sock):
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        threading.Thread(target=_serve, args=(conn,), daemon=True).start()


meter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
meter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
meter.bind(("127.0.0.1", 0))
meter.listen(8)
METER_HOST, METER_PORT = meter.getsockname()
threading.Thread(target=_accept, args=(meter,), daemon=True).start()
print("  fake Modbus meter on {0}:{1} ({2:.0f} ms per request)"
      .format(METER_HOST, METER_PORT, RESPONSE_DELAY_S * 1000))

# ----------------------------------------------------------------- backend
tmp = tempfile.mkdtemp(prefix="tn-power-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
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
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw or "null")
        except Exception:
            return e.code, raw[:300]
    except Exception as e:
        return 0, str(e)[:300]


def finish(code):
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    try:
        meter.close()
    except Exception:
        pass
    sys.exit(code)


st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
admin = (b or {}).get("token") if isinstance(b, dict) else None
check("admin login", st == 200 and bool(admin))
if not admin:
    finish(2)

st, cfg = call("GET", "/api/power/config", admin)
base = (cfg or {}).get("config") if isinstance(cfg, dict) else {}
if not isinstance(base, dict):
    base = {}

DEVICE = {
    "id": "pm-test", "name": "Test Meter", "enabled": True,
    "type": "modbus_tcp", "protocol": "modbus_tcp",
    "ip": METER_HOST, "port": METER_PORT, "unit_id": 1,
    "poll_interval_ms": 1000,
    "electrical_mode": os.environ.get("TN_POWER_MODE", "single_phase"),
    "voltage_connected": True, "ct_connected": True,
    "ct_primary": 80.0, "ct_secondary": 5.0,
    "vt_primary": 230.0, "vt_secondary": 230.0,
}
payload = dict(base)
payload["enabled"] = True
payload["devices"] = [DEVICE]
payload["selected_device_id"] = "pm-test"
# the endpoint takes the config itself, not a {"config": ...} envelope
st, r = call("PUT", "/api/power/config", admin, payload)
check("the meter is configured at 1000 ms", st == 200,
      "status={0} {1}".format(st, str(r)[:110]))
if st != 200:
    finish(2)

print("\n[collecting for {0}s]".format(RUN_S))
time.sleep(RUN_S)

st, hist = call("GET", "/api/power/history?limit=20000", admin)
rows = (hist or {}).get("rows") or []
check("the meter produced history", len(rows) > 0, len(rows))
if not rows:
    st, diag = call("GET", "/api/power/diagnostics", admin)
    d = ((diag or {}).get("diagnostics") or {})
    print("  worker_count      : %s" % d.get("worker_count"))
    print("  writer_batches    : %s" % d.get("writer_batches"))
    print("  devices_metrics   : %s" % str(d.get("devices_metrics"))[:220])
    print("  devices_status    : %s" % str(d.get("devices_status"))[:400])
    st, lat = call("GET", "/api/power/latest", admin)
    print("  latest            : %s" % str(lat)[:400])
    st, raw = call("GET", "/api/app-store/historian?limit=20", admin)
    rr = ((raw or {}).get("rows") or [])
    print("  raw historian rows: %d" % len(rr))
    if rr:
        print("  sample            : %s" % str(rr[0])[:220])
    finish(2)

# cadence of ONE tag - mixing tags would hide a gap
by_tag = {}
for r in rows:
    by_tag.setdefault(str(r.get("tag") or ""), []).append(str(r.get("ts") or ""))
tag = max(by_tag, key=lambda t: len(by_tag[t]))
stamps = sorted(set(by_tag[tag]))
print("  densest tag: {0} ({1} distinct timestamps)".format(tag, len(stamps)))


def _sec(ts):
    from datetime import datetime
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(ts.replace("Z", "").split("+")[0], fmt).timestamp()
        except Exception:
            continue
    return None


secs = [s for s in (_sec(t) for t in stamps) if s is not None]
secs.sort()
gaps = [round(secs[i + 1] - secs[i], 2) for i in range(len(secs) - 1)]
if gaps:
    gaps_sorted = sorted(gaps)
    median = gaps_sorted[len(gaps_sorted) // 2]
    worst = max(gaps)
    over_2s = [g for g in gaps if g > 2.0]
    print("  intervals: median {0:.2f}s, worst {1:.2f}s, {2} gap(s) over 2s"
          .format(median, worst, len(over_2s)))
    check("the median interval is ~1s", 0.7 <= median <= 1.4,
          "{0:.2f}s".format(median))
    check("no gap exceeds 2s", not over_2s,
          "worst {0:.2f}s, gaps: {1}".format(worst, sorted(over_2s, reverse=True)[:6]))
    expected = int(RUN_S * 0.75)
    check("roughly one sample per second overall", len(secs) >= expected,
          "{0} samples in {1}s (expected >= {2})".format(len(secs), RUN_S, expected))

# which of the three causes, if any
print("\n[why - the manager's own metrics]")
st, diag = call("GET", "/api/power/diagnostics", admin)
d = (diag or {}).get("diagnostics") or {}
metrics = {}
if isinstance(d, dict):
    for key in ("devices_metrics", "metrics_by_device", "metrics", "devices"):
        if isinstance(d.get(key), dict):
            metrics = d[key]
            break
m = metrics.get("pm-test") if isinstance(metrics, dict) else {}
if not isinstance(m, dict):
    m = {}
print("  poll_duration_ms      : {0}".format(m.get("poll_duration_ms")))
print("  schedule_lag_ms       : {0}".format(m.get("schedule_lag_ms")))
print("  effective_interval_ms : {0}".format(m.get("effective_interval_ms")))
print("  skipped_cycles        : {0}".format(m.get("skipped_cycles")))
print("  writer_queue_depth    : {0}".format(m.get("writer_queue_depth")))
print("  writer_dropped_rows   : {0}".format(m.get("writer_dropped_rows")))
print("  modbus requests served: {0}".format(REQUESTS["n"]))

check("the poll fits inside the interval",
      float(m.get("poll_duration_ms") or 0) < 1000.0, m.get("poll_duration_ms"))
check("no cycles were skipped", int(m.get("skipped_cycles") or 0) == 0,
      m.get("skipped_cycles"))
check("the writer dropped nothing", int(m.get("writer_dropped_rows") or 0) == 0,
      m.get("writer_dropped_rows"))

# --- the bucketed read the Power Overview and reports depend on ------------
# The rollup tables are empty here (no retention policy has run), which is the
# normal state of an edge. Before 2026-08-26 that meant the bucketed endpoint
# returned nothing and every long-range chart fell back to a capped raw slice.
print()
print("[the bucketed read used by long-range charts]")
from datetime import datetime as _dtm, timedelta as _td, timezone as _tz
_now = _dtm.now(_tz.utc)
_from = (_now - _td(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
_to = (_now + _td(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
import urllib.parse as _up
_q = _up.urlencode({"bucket": "minute", "from_utc": _from, "to_utc": _to,
                    "tag": "voltage", "limit": "2000",
                    "source": "power_modbus,power_insight"})
st, agg = call("GET", "/api/app-store/historian/agg?" + _q, admin)
agg_rows = (agg or {}).get("rows") or []
check("the bucketed endpoint answers in the shipped build", st == 200,
      "status={0} {1}".format(st, str(agg)[:100]))
check("  and returns buckets with an EMPTY rollup table", len(agg_rows) > 0,
      "{0} bucket(s)".format(len(agg_rows)))
if agg_rows:
    r0 = agg_rows[0]
    check("  each bucket carries avg/min/max and a sample count",
          r0.get("value") is not None and r0.get("value_min") is not None
          and int(r0.get("sample_count") or 0) > 0,
          {k: r0.get(k) for k in ("value", "value_min", "value_max", "sample_count")})


# --- what the Registers table reads ----------------------------------------
# 2026-08-26, from the field: the meter polled perfectly (Running, 984 ms
# measured, 0 skipped, 0 dropped) yet every register showed "-". The table was
# deriving its values from the CHART's history fetch, and for any window wider
# than 15 minutes that fetch is narrowed to the chart metric plus insight.*
# tags - voltage and current are simply not in it. It now reads the live
# sample, so these assert the live sample carries the whole register set.
print()
print("[the live sample behind the Registers table]")
st, lat = call("GET", "/api/power/latest?device_id=pm-test", admin)
sample = (lat or {}).get("sample") or {}
check("the live sample endpoint answers", st == 200 and bool(sample),
      "status={0} {1}".format(st, str(lat)[:90]))
vals = sample.get("values_scaled") or sample.get("values") or {}
check("  it carries scaled values", bool(vals), list(vals)[:6])
check("  keyed by the register tag the table shows",
      any(k.startswith("voltage") for k in vals), sorted(vals)[:6])
check("  and it names the device it came from",
      str(sample.get("device") or "") == "pm-test", sample.get("device"))
check("  with a timestamp the UI can parse",
      bool(str(sample.get("ts") or "")), sample.get("ts"))

# every register the operator configured should be present, or the table shows
# a dash for a register that is in fact being read
st, cfg2 = call("GET", "/api/power/config", admin)
dev = next((d for d in (((cfg2 or {}).get("config") or {}).get("devices") or [])
            if str(d.get("id")) == "pm-test"), {})
regs = list((dev.get("registers") or {}).keys())
missing = [r for r in regs if r not in vals]
check("  EVERY configured register has a live value", not missing,
      "missing: {0}".format(missing[:6]) if missing else "{0} register(s)".format(len(regs)))

# the UI must prefer this over the chart's row set
src = io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
              encoding="utf-8", errors="replace").read()
i = src.find("const selectedPowerLatestByTag")
block = src[i:i + 2200] if i >= 0 else ""
check("the Registers table prefers the live sample",
      "powerSample" in block and "values_scaled" in block)
check("  and re-renders when it changes",
      "powerHistoryRows, powerConfig, powerSample" in src)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
