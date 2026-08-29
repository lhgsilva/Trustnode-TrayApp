# -*- coding: utf-8 -*-
"""Power-meter tags must reach the live stream at the meter's poll rate.

2026-08-26, reported: "the chart when have power meters tags linked to the
series still are not updated as defined in the gateway, in this case 1 s. the
historian also is delaying to update something like 3-6 s."

The historian was NOT delayed - the operator's own screenshot showed a row at
every second. What lagged was the VIEW: power meters wrote to the historian and
to nothing else, so a dashboard widget bound to a meter tag had no live source
and could only poll. PLC tags on the same chart updated live because gateways
push to the WebSocket stream via fanout_threadsafe.

Two defects, both fixed:
  * power readings were never fanned out at all;
  * PLCManager._loop - which fanout_threadsafe needs - was set ONLY inside the
    V2 reader's start path, and V2 is off by default, so the fanout would have
    been a silent no-op even once wired.

This subscribes to the real /ws/stream and measures the arrival cadence.
"""
import base64
import hashlib
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
PORT = "8111"
API = "http://127.0.0.1:" + PORT
FAILS = []
RUN_S = int(os.environ.get("TN_STREAM_RUN_S", "20"))


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:115]) if detail else ""
    print("  {0:56s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


# --- a meter that answers the single-phase set -----------------------------
IMPL = {19000: 232.4, 19012: 3.003, 19020: 58.0, 19044: 0.9, 19050: 50.0, 19054: 65.9}


def _f32(v):
    return struct.unpack(">HH", struct.pack(">f", float(v)))


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
            start, count = struct.unpack(">HH", body[2:6])
            out = b""
            i = 0
            while i < count:
                a = start + i
                if a in IMPL:
                    hi, lo = _f32(IMPL[a])
                    out += struct.pack(">HH", hi, lo)
                    i += 2
                    continue
                out += struct.pack(">H", 0)
                i += 1
            out = (out + b"\x00" * (count * 2))[: count * 2]
            pdu = struct.pack(">BBB", unit, func, len(out)) + out
            conn.sendall(struct.pack(">HHH", tid, pid, len(pdu)) + pdu)
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


meter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
meter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
meter.bind(("127.0.0.1", 0))
meter.listen(8)
MHOST, MPORT = meter.getsockname()


def _accept():
    while True:
        try:
            c, _ = meter.accept()
        except OSError:
            return
        threading.Thread(target=_serve, args=(c,), daemon=True).start()


threading.Thread(target=_accept, daemon=True).start()

tmp = tempfile.mkdtemp(prefix="tn-live-")
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
payload = dict(((cfg or {}).get("config") or {}))
payload["enabled"] = True
payload["selected_device_id"] = "PGE"
payload["devices"] = [{
    "id": "PGE", "name": "PGE Meter", "enabled": True, "type": "modbus_tcp",
    "protocol": "modbus_tcp", "ip": MHOST, "port": MPORT, "unit_id": 1,
    "poll_interval_ms": 1000, "electrical_mode": "single_phase",
}]
st, r = call("PUT", "/api/power/config", admin, payload)
check("the meter is configured at 1000 ms", st == 200, "status={0}".format(st))

# --- subscribe to the real WebSocket stream --------------------------------
# A minimal RFC6455 client: pulling in a websocket library for one test is not
# worth it, and this exercises the same endpoint the browser uses.
print("\n[subscribing to /ws/stream for {0}s]".format(RUN_S))
key = base64.b64encode(os.urandom(16)).decode()
sock = socket.create_connection(("127.0.0.1", int(PORT)), timeout=10)
req = (
    "GET /ws/stream?token={0} HTTP/1.1\r\n"
    "Host: 127.0.0.1:{1}\r\n"
    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
    "Sec-WebSocket-Key: {2}\r\nSec-WebSocket-Version: 13\r\n\r\n"
).format(admin, PORT, key)
sock.sendall(req.encode())
resp = b""
while b"\r\n\r\n" not in resp:
    chunk = sock.recv(4096)
    if not chunk:
        break
    resp += chunk
accept = base64.b64encode(hashlib.sha1(
    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
check("the stream accepted the subscription",
      b"101" in resp.split(b"\r\n")[0] and accept.encode() in resp,
      resp.split(b"\r\n")[0][:60])


def read_frames(sock, seconds):
    """Yield text payloads for `seconds`. Handles unmasked server frames."""
    out = []
    sock.settimeout(1.0)
    buf = b""
    end = time.time() + seconds
    while time.time() < end:
        try:
            data = sock.recv(65536)
            if not data:
                break
            buf += data
        except socket.timeout:
            continue
        except Exception:
            break
        while len(buf) >= 2:
            b1, b2 = buf[0], buf[1]
            opcode = b1 & 0x0F
            ln = b2 & 0x7F
            off = 2
            if ln == 126:
                if len(buf) < 4:
                    break
                ln = struct.unpack(">H", buf[2:4])[0]
                off = 4
            elif ln == 127:
                if len(buf) < 10:
                    break
                ln = struct.unpack(">Q", buf[2:10])[0]
                off = 10
            if len(buf) < off + ln:
                break
            payload = buf[off:off + ln]
            buf = buf[off + ln:]
            if opcode == 1:
                out.append((time.time(), payload.decode("utf-8", "replace")))
    return out


frames = read_frames(sock, RUN_S)
try:
    sock.close()
except Exception:
    pass

power_msgs = []
for ts, txt in frames:
    try:
        msg = json.loads(txt)
    except Exception:
        continue
    if str(msg.get("gateway_id") or "") == "PGE" and isinstance(msg.get("readings"), list):
        power_msgs.append((ts, msg))

check("POWER TAGS ARRIVE ON THE LIVE STREAM", bool(power_msgs),
      "{0} message(s) in {1}s".format(len(power_msgs), RUN_S))
if power_msgs:
    gaps = [round(power_msgs[i + 1][0] - power_msgs[i][0], 2)
            for i in range(len(power_msgs) - 1)]
    if gaps:
        gaps_sorted = sorted(gaps)
        median = gaps_sorted[len(gaps_sorted) // 2]
        worst = max(gaps)
        print("  arrival interval: median {0:.2f}s, worst {1:.2f}s".format(median, worst))
        check("  at the meter's 1 s cadence, not 3-6 s", 0.7 <= median <= 1.5,
              "{0:.2f}s".format(median))
        check("  with no 3 s+ stall", worst < 3.0, "worst {0:.2f}s".format(worst))
    tags = set()
    for _, m in power_msgs:
        for r in m.get("readings") or []:
            tags.add(str(r.get("tag_name") or ""))
    check("  carrying the meter's register tags",
          any(t.startswith("voltage") or t.startswith("current") for t in tags),
          sorted(tags)[:6])
    check("  and the derived insight tags the KPIs use",
          any(t.startswith("insight.") for t in tags), sorted(tags)[:6])
    sample = (power_msgs[-1][1].get("readings") or [{}])[0]
    check("  each reading has a parseable timestamp",
          bool(str(sample.get("ts_utc") or "")) and "1970" not in str(sample.get("ts_utc")),
          sample.get("ts_utc"))
    check("  and a quality label", bool(sample.get("quality_label")),
          sample.get("quality_label"))

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
