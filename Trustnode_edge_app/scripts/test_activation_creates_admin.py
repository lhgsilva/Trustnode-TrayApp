# -*- coding: utf-8 -*-
"""Activating an edge MUST leave a local admin who can actually log in.

2026-08-25, reported from a brand-new computer: activation reported success,
but the admin account it promised did not exist, the operator could not log in,
and there was no way back in - no local account recovery, no way to create the
first admin. A locked-out edge is unrecoverable without this.

These drive the LOCAL half of activation (/edge-link/local-finalize), which is
what creates the account, and then try to actually log in with it. No portal,
no network, no activation code needed.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = "8101"
API = "http://127.0.0.1:" + PORT
FAILS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:120]) if detail else ""
    print("  {0:56s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


tmp = tempfile.mkdtemp(prefix="tn-activation-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=os.path.join(ROOT, "backend"),
                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw or "null")
        except Exception:
            return e.code, raw[:400]
    except Exception as e:
        return 0, str(e)[:300]


def finish(code):
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    sys.exit(code)


CUSTOMER = "cust-e5916328"
TENANT = "tenant-" + CUSTOMER
FINALIZE = {
    "tenant_id": TENANT,
    "edge_id": "edge-74d903ffcd",
    "edge_name": "Mari-A",
    "customer_id": CUSTOMER,
    "license_id": "lic-test-0001",
    "license_status": "active",
    "license_plan_code": "standard",
    "license_modules": [],
    "cloud_api_url": "https://trustnode.lsapps.app",
    "admin_username": "admin-newpc",
    "admin_password": "Str0ngPass!2026",
}

print("[the local half of activation]")
st, r = call("POST", "/api/control-plane/edge-link/local-finalize", body=FINALIZE)
check("local-finalize succeeds", st == 200, "status={0} {1}".format(st, str(r)[:150]))

# THE point of the whole flow: the operator can now get in.
st, r = call("POST", "/api/auth/login",
             body={"username": FINALIZE["admin_username"],
                   "password": FINALIZE["admin_password"]})
token = (r or {}).get("token") if isinstance(r, dict) else None
check("the promised admin CAN LOG IN", st == 200 and bool(token),
      "status={0} {1}".format(st, str(r)[:150]))

if token:
    st, me = call("GET", "/api/auth/me", token)
    role = ((me or {}).get("user") or me or {}).get("role") if isinstance(me, dict) else None
    check("  and is an admin", str(role) == "admin", role)

print("\n[an activation that cannot create an account must NOT report success]")
for missing in ("edge_id", "customer_id", "license_id"):
    bad = dict(FINALIZE, admin_username="admin-x-" + missing)
    bad[missing] = ""
    st, r = call("POST", "/api/control-plane/edge-link/local-finalize", body=bad)
    check("a payload with no {0} is rejected".format(missing), st >= 400,
          "status={0}".format(st))
    st2, r2 = call("POST", "/api/auth/login",
                   body={"username": bad["admin_username"],
                         "password": FINALIZE["admin_password"]})
    check("  and creates no half-made account", st2 != 200, "status={0}".format(st2))

print("\n[a locked-out edge must have a way back in]")
st, r = call("GET", "/api/auth/recovery-status")
check("recovery status is reachable WITHOUT logging in", st == 200,
      "status={0} {1}".format(st, str(r)[:120]))
check("  it sees the admin activation created", (r or {}).get("has_admin") is True,
      "has_admin={0} admin_count={1} user_count={2}".format(
          (r or {}).get("has_admin"), (r or {}).get("admin_count"),
          (r or {}).get("user_count")))
check("  it names the break-glass account so a locked-out operator knows",
      bool((r or {}).get("master_account_hint")), (r or {}).get("master_account_hint"))
check("  it never returns a password or a hash",
      "hash" not in json.dumps(r or {}).lower()
      and "password" not in json.dumps(r or {}).lower().replace("master_default_password", ""),
      list((r or {}).keys()))

st, req = call("POST", "/api/auth/local-recovery/request")
path = (req or {}).get("recovery_file") or ""
check("recovery writes a code file into the data directory",
      st == 200 and os.path.isfile(path), "status={0} {1}".format(st, path))
check("  and the RESPONSE does not carry the code",
      "recovery code" not in json.dumps(req or {}).lower(), list((req or {}).keys()))

code = ""
if path and os.path.isfile(path):
    for line in io.open(path, encoding="utf-8").read().splitlines():
        if line.lower().startswith("recovery code:"):
            code = line.split(":", 1)[1].strip()
check("  the file carries a code the operator can read", bool(code),
      (code[:4] + "...") if code else "(none)")

st, r = call("POST", "/api/auth/local-recovery/complete",
             body={"code": "WRON-GCOD-E000", "username": "rescue",
                   "password": "An0therPass!9"})
check("a WRONG code is refused", st == 403, "status={0}".format(st))
st, r = call("POST", "/api/auth/login",
             body={"username": "rescue", "password": "An0therPass!9"})
check("  and the attempt created nothing", st != 200, "status={0}".format(st))

st, r = call("POST", "/api/auth/local-recovery/complete",
             body={"code": code, "username": "rescue", "password": "short"})
check("a too-short password is refused", st == 400, "status={0}".format(st))
# an admin made here must meet the SAME policy as one made anywhere else,
# otherwise recovery is a way to smuggle a weak administrator onto the box
st, r = call("POST", "/api/auth/local-recovery/complete",
             body={"code": code, "username": "rescue", "password": "Passw0rd"})
check("  and so is an 8-char one (admin policy is 12 + letters + digits)",
      st == 400, "status={0} {1}".format(st, str(r)[:90]))
st, r = call("POST", "/api/auth/local-recovery/complete",
             body={"code": code, "username": "rescue", "password": "alllettersonlyhere"})
check("  and one with no digits", st == 400, "status={0}".format(st))

st, r = call("POST", "/api/auth/local-recovery/complete",
             body={"code": code, "username": "rescue", "password": "An0therPass!9"})
check("the right code creates an administrator", st == 200,
      "status={0} {1}".format(st, str(r)[:120]))

st, r = call("POST", "/api/auth/login",
             body={"username": "rescue", "password": "An0therPass!9"})
tok2 = (r or {}).get("token") if isinstance(r, dict) else None
check("THE OPERATOR IS BACK IN", st == 200 and bool(tok2), "status={0}".format(st))
if tok2:
    st, me = call("GET", "/api/auth/me", tok2)
    u = (me or {}).get("user") if isinstance((me or {}).get("user"), dict) else (me or {})
    check("  as an admin", str(u.get("role")) == "admin", u.get("role"))
    check("  in the tenant this edge is bound to",
          str(u.get("tenant_id") or "default") == TENANT, u.get("tenant_id"))

check("the code file is destroyed after use", not os.path.isfile(path), path)
st, r = call("POST", "/api/auth/local-recovery/complete",
             body={"code": code, "username": "rescue2", "password": "An0therPass!9"})
# 400/403 only: a 429 would mean the limiter answered, not the single-use rule
check("the code cannot be replayed", st in (400, 403),
      "status={0} {1}".format(st, str(r)[:90]))
st, r = call("POST", "/api/auth/login",
             body={"username": "rescue2", "password": "An0therPass!9"})
check("  and the replay created no account", st != 200, "status={0}".format(st))

# recovery must also RESET an admin whose password was forgotten
st, req = call("POST", "/api/auth/local-recovery/request")
path = (req or {}).get("recovery_file") or ""
code = ""
if path and os.path.isfile(path):
    for line in io.open(path, encoding="utf-8").read().splitlines():
        if line.lower().startswith("recovery code:"):
            code = line.split(":", 1)[1].strip()
st, r = call("POST", "/api/auth/local-recovery/complete",
             body={"code": code, "username": FINALIZE["admin_username"],
                   "password": "BrandNewPass!77"})
check("an existing admin can have their password reset", st == 200,
      "status={0} {1}".format(st, str(r)[:110]))
st, r = call("POST", "/api/auth/login",
             body={"username": FINALIZE["admin_username"], "password": "BrandNewPass!77"})
check("  the new password works", st == 200, "status={0}".format(st))
st, r = call("POST", "/api/auth/login",
             body={"username": FINALIZE["admin_username"],
                   "password": FINALIZE["admin_password"]})
check("  the old one does not", st != 200, "status={0}".format(st))

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
