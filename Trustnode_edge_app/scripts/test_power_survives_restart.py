# -*- coding: utf-8 -*-
"""A restart must not disable the operator's power meter, or rewrite its config.

2026-08-30, reported from the plant: "the power meter again did not start when
the app opened". The boot policy force-stopped every meter and PERSISTED that,
turning a stored `enabled: true` into `enabled: false` under actor "system".

Two separate failures in one line of code:

  * the meter had to be started by hand after every restart, and
  * the operator's setting was destroyed doing it, so nothing in the store
    remembered the meter was meant to be running.

Diffing the live store against the morning backup showed exactly two changed
fields - `.enabled` and `.devices[0].enabled`, both True -> False - with the
whole register map untouched. That is a policy overwriting configuration,
which is the same shape as the 2026-08-26 data loss that the surrounding
function was written to fix.

This seeds a config that says the meter is enabled, boots the app, and asserts
the stored config still says so afterwards. It needs no meter: the point is
what the STORE says after a boot, not whether anything answered on the wire.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = "8149"
API = "http://127.0.0.1:" + PORT
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:56s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


# --- the rule, at the source ---------------------------------------------
print("[boot reads the stored configuration; it does not rewrite it]")
src = io.open(os.path.join(ROOT, "backend", "app", "services", "power_manager.py"),
              encoding="utf-8", errors="replace").read()
head = src.split("def get_config", 1)[0]
check("boot never persists a policy decision",
      "upsert_domain" not in head,
      "the stored config is the operator's intent, not a scratchpad")
check("  auto-start is the default",
      'TRUSTNODE_POWER_AUTO_START", "1"' in src,
      "a meter that was running when the app closed comes back, like a gateway")
check("  the override still exists",
      '"0", "false", "no", "off"' in src)

# --- and in behaviour ----------------------------------------------------
print()
print("[a boot with the meter enabled leaves it enabled]")
tmp = tempfile.mkdtemp(prefix="tn-pwrboot-")
db = os.path.join(tmp, "s.db")

CFG = {
    "enabled": True,
    "selected_device_id": "EM1",
    "devices": [{
        "id": "EM1", "name": "EM1", "enabled": True, "protocol": "modbus_tcp",
        # TEST-NET-2: nothing answers, which is the point - the meter's
        # reachability has no bearing on whether its config survives a boot.
        "ip": "198.51.100.9", "port": 502, "unit_id": 1,
        "poll_interval_ms": 1000,
        "registers": {"active_power_w": 30013, "voltage_l1_v": 30001},
    }],
}
conn = sqlite3.connect(db)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("CREATE TABLE config_documents (domain TEXT PRIMARY KEY, payload_json TEXT, "
             "version INTEGER, updated_utc TEXT)")
conn.execute("INSERT INTO config_documents VALUES (?,?,?,?)",
             ("power_management_config", json.dumps(CFG), 1, "2026-08-30 00:00:00"))
conn.commit()
conn.close()

env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=db, TRUSTNODE_BOOT_INTEGRITY_CHECK="never",
           TRUSTNODE_PORT=PORT)
env.pop("TRUSTNODE_POWER_AUTO_START", None)
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
check("the app started", up)


def call(method, path, tok=None, body=None):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    d = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=d, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return 0, str(e)[:120]


def finish(code):
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    return code


if up:
    # Give the lazy loader a chance to run its boot policy.
    time.sleep(12)
    st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
    tok = (b or {}).get("token")
    st, r = call("GET", "/api/power/config", tok)
    cfg = (r or {}).get("config") or {}
    devs = cfg.get("devices") or []
    check("the served config still says enabled", cfg.get("enabled") is True,
          "enabled=%s" % cfg.get("enabled"))
    check("  and so does the device", bool(devs) and devs[0].get("enabled") is True,
          [(d.get("id"), d.get("enabled")) for d in devs])
    check("  the register map is untouched",
          bool(devs) and len(devs[0].get("registers") or {}) == 2,
          len((devs[0].get("registers") or {})) if devs else "no device")

    # And the STORE - the part that survives to the next boot.
    c = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True, timeout=20)
    row = c.execute("SELECT payload_json FROM config_documents "
                    "WHERE domain='power_management_config'").fetchone()
    c.close()
    stored = json.loads(row[0]) if row else {}
    sdev = (stored.get("devices") or [{}])[0]
    check("the STORED config still says enabled", stored.get("enabled") is True,
          "this is the field a boot used to flip to False")
    check("  and the stored device too", sdev.get("enabled") is True)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
