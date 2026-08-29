# -*- coding: utf-8 -*-
"""Every configured register must report a value, or say why it cannot.

2026-08-26, from the field (PGE meter): of 20 configured registers only 6 read.
The six that worked were exactly the single-phase profile keys; the three-phase
keys read "-". Two of them share an ADDRESS with a working one:

    voltage_l1_v      19000  ->  "-"        voltage_v      19000 -> 232.393
    current_l1_a      19012  ->  "-"        current_a      19012 ->   3.003
    active_power_l1_w 19020  ->  "-"        active_power_w 19020 ->  58.085
    power_factor_l1   19044  ->  "-"        power_factor   19044 ->   0.087

Same address, same Modbus response - so those four are not a device problem,
they are a bookkeeping problem on our side. The rest (19002, 19004, 19014 ...)
are addresses this particular meter genuinely does not implement.

These tests separate the two, because they need different answers: the first is
a bug to fix, the second is something the UI has to SAY rather than show as a
blank cell.
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
PORT = "8109"
API = "http://127.0.0.1:" + PORT
FAILS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:115]) if detail else ""
    print("  {0:56s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


# --- a meter that implements ONLY the single-phase set ---------------------
# Exactly the operator's PGE meter: L1 values answer, L2/L3 do not exist.
IMPLEMENTED = {19000: 232.393, 19012: 3.003, 19020: 58.085,
               19044: 0.087, 19050: 50.252, 19054: 65.922}
ABSENT = {19002, 19004, 19014, 19016, 19022, 19024, 19026, 19046, 19048, 19060}


def _f32(value):
    return struct.unpack(">HH", struct.pack(">f", float(value)))


def _serve(conn):
    try:
        conn.settimeout(20)
        while True:
            head = b""
            while len(head) < 6:
                c = conn.recv(6 - len(head))
                if not c:
                    return
                head += c
            tid, pid, length = struct.unpack(">HHH", head)
            body = b""
            while len(body) < length:
                c = conn.recv(length - len(body))
                if not c:
                    return
                body += c
            unit, func = body[0], body[1]
            if func not in (3, 4):
                conn.sendall(struct.pack(">HHH", tid, pid, 3)
                             + struct.pack(">BBB", unit, func | 0x80, 1))
                continue
            start, count = struct.unpack(">HH", body[2:6])
            # A real meter rejects a block that covers a register it does not
            # implement - that is what forces the driver down its split path.
            span = set(range(start, start + count))
            if span & ABSENT:
                pdu = struct.pack(">BBB", unit, func | 0x80, 2)   # illegal address
            else:
                out = b""
                for i in range(count):
                    a = start + i
                    if a in IMPLEMENTED:
                        hi, lo = _f32(IMPLEMENTED[a])
                        out += struct.pack(">HH", hi, lo)
                        continue
                    if (a - 1) in IMPLEMENTED:
                        continue          # low word already emitted
                    out += struct.pack(">H", 0)
                out = out[: count * 2]
                out += b"\x00" * (count * 2 - len(out))
                pdu = struct.pack(">BBB", unit, func, len(out)) + out
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
MHOST, MPORT = meter.getsockname()
threading.Thread(target=_accept, args=(meter,), daemon=True).start()
print("  meter on {0}:{1} - implements {2} address(es), rejects {3}"
      .format(MHOST, MPORT, len(IMPLEMENTED), len(ABSENT)))

# the operator's register map: three-phase profile PLUS single-phase keys,
# four of which land on the SAME addresses
REGISTERS = {
    "voltage_l1_v": 19000, "voltage_l2_v": 19002, "voltage_l3_v": 19004,
    "current_l1_a": 19012, "current_l2_a": 19014, "current_l3_a": 19016,
    "active_power_l1_w": 19020, "active_power_l2_w": 19022,
    "active_power_l3_w": 19024, "active_power_total_w": 19026,
    "power_factor_l1": 19044, "power_factor_l2": 19046,
    "power_factor_total": 19048, "frequency_hz": 19050,
    "energy_total_wh": 19060,
    # the manually added single-phase keys
    "active_power_w": 19020, "current_a": 19012, "energy_wh": 19054,
    "power_factor": 19044, "voltage_v": 19000,
}
SHARED = {"voltage_l1_v": "voltage_v", "current_l1_a": "current_a",
          "active_power_l1_w": "active_power_w", "power_factor_l1": "power_factor"}

tmp = tempfile.mkdtemp(prefix="tn-regcov-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
_exe = os.environ.get("TN_SERVICE_EXE")
cmd = [_exe] if (_exe and os.path.isfile(_exe)) else [sys.executable, "-m", "app"]
cwd = os.path.dirname(_exe) if (_exe and os.path.isfile(_exe)) else os.path.join(ROOT, "backend")
proc = subprocess.Popen(cmd, cwd=cwd, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        return e.code, e.read()[:300]
    except Exception as e:
        return 0, str(e)[:300]


def finish(code):
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    meter.close()
    sys.exit(code)


st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
admin = (b or {}).get("token") if isinstance(b, dict) else None
check("admin login", st == 200 and bool(admin))
if not admin:
    finish(2)

st, cfg = call("GET", "/api/power/config", admin)
base = ((cfg or {}).get("config") or {}) if isinstance(cfg, dict) else {}
payload = dict(base)
payload["enabled"] = True
payload["selected_device_id"] = "PGE"
payload["devices"] = [{
    "id": "PGE", "name": "PGE Meter", "enabled": True,
    "type": "modbus_tcp", "protocol": "modbus_tcp",
    "ip": MHOST, "port": MPORT, "unit_id": 1, "poll_interval_ms": 1000,
    "electrical_mode": "three_phase",
    "use_custom_registers": True, "registers": REGISTERS,
}]
st, r = call("PUT", "/api/power/config", admin, payload)
check("the 20-register meter is configured", st == 200,
      "status={0} {1}".format(st, str(r)[:90]))

time.sleep(12)
st, lat = call("GET", "/api/power/latest?device_id=PGE", admin)
vals = ((lat or {}).get("sample") or {}).get("values") or {}
print("  {0} of {1} registers reported a value".format(len(vals), len(REGISTERS)))

print()
print("[a register that shares an address with a working one MUST read]")
for twin, works in SHARED.items():
    got_twin = twin in vals
    got_work = works in vals
    check("{0} reads (same address as {1})".format(twin, works),
          got_twin and got_work,
          "{0}={1} {2}={3}".format(twin, vals.get(twin), works, vals.get(works)))
    if got_twin and got_work:
        check("  and they agree", abs(float(vals[twin]) - float(vals[works])) < 1e-6,
              "{0} vs {1}".format(vals[twin], vals[works]))

print()
print("[a register the meter does not implement]")
absent_keys = [k for k, a in REGISTERS.items() if a in ABSENT]
reported = [k for k in absent_keys if k in vals]
check("unimplemented registers do not fabricate a value", not reported,
      reported[:5])
check("  and they do not stop the implemented ones",
      len([k for k, a in REGISTERS.items() if a in IMPLEMENTED and k in vals]) >= 6,
      len(vals))

print()
print("[the operator can tell WHY a register is blank]")
st, diag = call("GET", "/api/power/diagnostics", admin)
d = (diag or {}).get("diagnostics") or {}
statuses = d.get("devices_status") or {}
mine = statuses.get("PGE") or {}
check("the device is reported connected", bool(mine.get("connected")), mine.get("connected"))
check("  and unreadable registers are named",
      bool(mine.get("unreadable_registers")),
      str(mine.get("unreadable_registers"))[:90] or "(not reported)")

# --- the KPIs must work on a meter with NO total registers ----------------
# The three-phase profile reads totals from 19026 / 19060. A meter that does
# not implement them used to give live_kw = 0 and a Power Overview of zeroes
# while every per-phase register was reading fine. Total active power IS the
# sum of the phases, so it is derived.
print()
print("[the Power Overview on a meter with no total registers]")
st, lat2 = call("GET", "/api/power/latest?device_id=PGE", admin)
vals2 = ((lat2 or {}).get("sample") or {}).get("values") or {}
has_total = "active_power_total_w" in vals2
check("the meter really has no total-power register", not has_total,
      vals2.get("active_power_total_w"))

st, hist = call("GET", "/api/power/history?limit=4000", admin)
rows = (hist or {}).get("rows") or []
live = [r for r in rows if str(r.get("tag")) == "insight.live_kw"]
check("insight.live_kw is still produced", bool(live), len(live))
if live:
    newest = max(live, key=lambda r: str(r.get("ts") or ""))
    kw = float(newest.get("value") or 0.0)
    # the meter reports 58.085 W on L1 and nothing on L2/L3
    check("  and it is DERIVED from the phases, not zero", kw > 0,
          "{0} kW".format(kw))
    check("  matching the phase power that was read",
          abs(kw - 58.085 / 1000.0) < 1e-4, "{0} kW".format(kw))

cur = [r for r in rows if str(r.get("tag")) == "insight.current_a"]
if cur:
    newest_c = max(cur, key=lambda r: str(r.get("ts") or ""))
    amps = float(newest_c.get("value") or 0.0)
    check("current is reported (averaged, never summed)",
          abs(amps - 3.003) < 1e-3, "{0} A".format(amps))

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
