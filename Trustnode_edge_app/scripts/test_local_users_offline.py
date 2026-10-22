# -*- coding: utf-8 -*-
"""Local users must work on an ACTIVATED edge with no internet at all.

Reported from a customer install: after activation the locally created
users could not log in, and the operator needed the recovery path to get
back in. Many sites have no cloud database and no internet; the edge must
run, authenticate and recover entirely on its own.

Everything here runs against a throwaway workspace with every cloud
variable stripped and the cloud URL pointed at a black hole, so any code
path that quietly needs the internet shows up as a failure or a hang
rather than passing because this machine happens to be online.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = os.environ.get("TN_LOCAL_USERS_PORT", "8106")
API = "http://127.0.0.1:" + PORT
FAILS = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(
        name, "PASS" if ok else "FAIL", (" - " + str(detail)[:130]) if not ok or detail else ""))
    if not ok:
        FAILS.append(name)


tmp = tempfile.mkdtemp(prefix="tn-localusers-")
env = dict(os.environ)
# Strip EVERY cloud credential: this edge has no cloud database.
for k in list(env):
    if k.startswith(("TRUSTNODE_CLOUD_", "TRUSTNODE_SUPABASE_")):
        env.pop(k, None)
env.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT,
           TRUSTNODE_DISABLE_CONFIG_PUSH="1")
proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=os.path.join(ROOT, "backend"),
                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
boot_ok = False
for _ in range(60):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read()
        boot_ok = True
        break
    except Exception:
        time.sleep(2)


def call(method, path, token=None, body=None, timeout=45):
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
        return 0, str(e)[:200]


def finish(code):
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    sys.exit(code)


def login(username, password, tries=25):
    """Log in, waiting out the per-IP limiter this file's own failures trip."""
    st, r = 0, None
    for _ in range(tries):
        st, r = call("POST", "/api/auth/login",
                     body={"username": username, "password": password})
        if st != 429:
            return st, r
        time.sleep(4)
    return st, r


print("Local users on an activated, OFFLINE edge")
print("  no cloud database, no Supabase, config push disabled")
print()
check("the edge starts with no cloud configured at all", boot_ok,
      "it must run standalone - many sites have no internet")
if not boot_ok:
    finish(2)

st, h = call("GET", "/api/health")
check("  and reports healthy", st == 200, "status={0}".format(st))

# ---------------------------------------------------------------- activate
TEN, CUST = "tenant-cust-off", "cust-off"
ADMIN_PW = "OfflineAdmin!2026"
st, r = call("POST", "/api/control-plane/edge-link/local-finalize", body={
    "tenant_id": TEN, "edge_id": "edge-off-1", "edge_name": "Offline",
    "customer_id": CUST, "license_id": "lic-off-1", "license_status": "active",
    "license_plan_code": "standard",
    "license_modules": [{"module_key": "dashboard", "enabled": True},
                        {"module_key": "users_and_access_control", "enabled": True}],
    # A cloud URL that cannot be reached: activation must not depend on it.
    "cloud_api_url": "http://127.0.0.1:9",
    "admin_username": "admin", "admin_password": ADMIN_PW,
})
check("activation completes with the cloud unreachable", st == 200,
      "status={0} {1}".format(st, str(r)[:110]))

st, r = login("admin", ADMIN_PW)
token = (r or {}).get("token") if isinstance(r, dict) else None
check("the activation admin can log in", st == 200 and bool(token),
      "status={0} {1}".format(st, str(r)[:110]))
if not token:
    finish(2)

# ------------------------------------------------- a locally created user
OP_PW = "Operator!2026xy"
st, r = call("POST", "/api/control-plane/users?tenant_id=" + TEN, token, {
    "username": "operator1", "password": OP_PW, "role": "operator",
    "status": "active", "email": "op1@example.com",
})
check("an admin can create a local user after activation", st == 200,
      "status={0} {1}".format(st, str(r)[:110]))

st, r = login("operator1", OP_PW)
op_token = (r or {}).get("token") if isinstance(r, dict) else None
check("THAT USER CAN LOG IN", st == 200 and bool(op_token),
      "status={0} {1}".format(st, str(r)[:130]))
if op_token:
    st, me = call("GET", "/api/auth/me", op_token)
    tid = (((me or {}).get("user") or me or {}) if isinstance(me, dict) else {}).get("tenant_id")
    check("  and lands in the edge's own tenant", str(tid) == TEN,
          "{0} (wanted {1})".format(tid, TEN))

st, r = login("operator1", "WrongPassword!99")
check("  a wrong password is still refused", st not in (200, 429),
      "status={0}".format(st))

# ------------------------------------------------- an admin resets them
NEW_PW = "Operator!2026zz"
st, r = call("POST", "/api/control-plane/users/operator1/password?tenant_id=" + TEN,
             token, {"password": NEW_PW})
check("an admin can reset that user's password", st == 200,
      "status={0} {1}".format(st, str(r)[:110]))
st, r = login("operator1", NEW_PW)
check("  the new password works", st == 200, "status={0}".format(st))

# ---------------------------------------------------- local recovery path
st, req = call("POST", "/api/auth/local-recovery/request")
path = (req or {}).get("recovery_file") if isinstance(req, dict) else ""
code = ""
if path and os.path.isfile(path):
    for line in open(path, encoding="utf-8").read().splitlines():
        if line.lower().startswith("recovery code:"):
            code = line.split(":", 1)[1].strip()
check("recovery works with no internet", bool(code),
      "status={0} file={1}".format(st, path))

if code:
    RESCUE_PW = "Rescued!2026abc"
    st, r = call("POST", "/api/auth/local-recovery/complete",
                 body={"code": code, "username": "admin", "password": RESCUE_PW})
    check("  the recovery code resets the admin", st == 200,
          "status={0} {1}".format(st, str(r)[:110]))
    st, r = login("admin", RESCUE_PW)
    check("  THE OPERATOR IS BACK IN", st == 200, "status={0}".format(st))

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
