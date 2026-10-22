# -*- coding: utf-8 -*-
"""Forgot password: six digits by email, then set a password.

2026-10-22, reported: "the forgot password also is not working ... we
should not be needed to provide the tenant", and "the code is not being
sent by email".

Measured before the fix, and this is the whole story:

  * login_audit, both auth stores, every row whose detail mentions a code
    or a reset: ZERO. Not one code had EVER been requested. The Forgot
    panel called the PORTAL's /control-plane/password-reset/public/issue,
    which needs a tenant nobody on a customer edge knows - while
    /api/auth/login-code/{request,verify}, built 2026-10-20 to do exactly
    this with Supabase, were called by no component at all.
  * All 29 local users had NO email. The add-user form collects one and
    the CLOUD payload sent it; the LOCAL user object dropped it, and the
    local store is what recovery reads.
  * Every failure - no Supabase key, no address - returned the same
    "a code has been sent", so a dead flow looked like a working one.

The flow now: identifier -> code -> that code signs you in on a SHORT
session carrying `pwreset` -> the set-password dialog does not ask for a
current password, because someone recovering an account does not have one.

That last step is the one with teeth, so it is tested against a running
backend rather than read: a pwreset token may set a password with no
current password, and an ORDINARY token with the same blank field must
still be refused. Otherwise the claim would be a way for any stolen token
to rotate a password.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = os.environ.get("TN_RECOVERY_PORT", "8111")
API = "http://127.0.0.1:" + PORT
SECRET = "recovery-flow-test-secret-2026-10-22"
FAILS = []


def check(name, ok, detail=""):
    print("  {0:60s}: {1}{2}".format(
        name, "PASS" if ok else "FAIL",
        (" - " + str(detail)[:140]) if not ok else ""))
    if not ok:
        FAILS.append(name)


# ======================================================== the login screen
print("Forgot password: one field, six digits, then set a password")
print()
login = io.open(os.path.join(ROOT, "frontend", "src", "components",
                             "Login", "Login.jsx"), encoding="utf-8").read()

check("the screen asks for no tenant", "forgotForm.tenant_id" not in login,
      "an operator on a customer edge does not know it")
check("it requests the code from the edge", "requestLoginCode(" in login,
      "the old path called the portal's reset endpoint")
check("the code signs the user in", "verifyLoginCode(" in login)
check("it no longer calls the portal reset endpoints",
      "issuePublicPasswordReset" not in login
      and "applyPublicPasswordReset" not in login,
      "those need a tenant and a portal a fresh edge cannot reach")
check("it reports when THIS edge cannot send codes",
      "can_send_codes" in login,
      "otherwise a missing key is indistinguishable from a sent code")

app = io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
              encoding="utf-8").read()
check("a recovered session is not asked for a current password",
      "forceChangePassword.recovered" in app,
      "they are here precisely because they do not have one")

# the dropped address - the reason there was nowhere to send a code to
create = re.search(r"const newUser = \{(.{0,1400}?)\};", app, re.S)
edit = re.search(
    r"if \(u\.username !== editingUsername\) return u;(.{0,900}?)\};",
    app, re.S)
check("a user created on the edge keeps their email",
      bool(create) and "email:" in create.group(1),
      "the local store is what recovery reads")
check("  and so does one that is edited",
      bool(edit) and "email:" in edit.group(1))

# ============================================================ the backend
tmp = tempfile.mkdtemp(prefix="tn-recovery-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT,
           TRUSTNODE_AUTH_SECRET=SECRET)
# No cloud keys: this is a customer edge that has never reached Supabase,
# which is the machine the operator was actually holding.
for _k in ("TRUSTNODE_SUPABASE_URL", "TRUSTNODE_SUPABASE_ANON_KEY",
           "TRUSTNODE_SUPABASE_SERVICE_KEY"):
    env.pop(_k, None)

proc = subprocess.Popen([sys.executable, "-m", "app"],
                        cwd=os.path.join(ROOT, "backend"), env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(60):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read()
        break
    except Exception:
        time.sleep(2)


def call(method, path, token=None, body=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=h,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]
    except Exception as e:
        return 0, str(e)[:200]


def finish(code):
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    sys.exit(code)


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def mint(username, pwreset, role="viewer", tenant="default"):
    """A token exactly as the app signs them - the secret is shared by env."""
    now = int(time.time())
    payload = {"sub": username, "role": role, "permissions": {},
               "modules": [], "tenant_id": tenant, "iat": now,
               "exp": now + 1800, "tv": 0}
    if pwreset:
        payload["pwreset"] = True
    p1 = b64(json.dumps({"alg": "HS256", "typ": "JWT"},
                        separators=(",", ":")).encode())
    p2 = b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(SECRET.encode(), (p1 + "." + p2).encode(),
                   hashlib.sha256).digest()
    return p1 + "." + p2 + "." + b64(sig)


print()
print("[the edge says plainly that it cannot send codes]")
st, r = call("GET", "/api/auth/code-readiness")
check("readiness answers", st == 200, "status=%s" % st)
if st == 200 and isinstance(r, dict):
    check("  this edge reports it cannot send codes",
          r.get("can_send_codes") is False,
          "no Supabase key is configured here")
    check("  and names what is missing", bool(r.get("reason")),
          "a silent failure is why nobody knew")
    check("  without naming any account",
          "@" not in str(r.get("reason") or ""),
          "readiness is a property of the edge, not of a user")

print()
print("[requesting a code still never reveals whether an account exists]")
st_known, r_known = call("POST", "/api/auth/login-code/request",
                         body={"identifier": "admin"})
st_bogus, r_bogus = call("POST", "/api/auth/login-code/request",
                         body={"identifier": "no-such-person-9f3a"})
check("a real and a bogus identifier answer identically",
      st_known == st_bogus and r_known == r_bogus,
      "%s vs %s" % (st_known, st_bogus))

print()
print("[a code-recovered session may set a password; nothing else may]")
st, r = call("POST", "/api/auth/login", body={"username": "admin",
                                             "password": "admin"})
admin_tok = (r or {}).get("token") if isinstance(r, dict) else None
if not admin_tok:
    check("signed in to set the fixture up", False, "status=%s" % st)
    finish(2)

call("POST", "/api/control-plane/users?tenant_id=default", admin_tok,
     {"username": "recoverme", "password": "OldPass!2026a", "role": "viewer",
      "status": "active", "email": "recoverme@example.com"})

st, r = call("POST", "/api/auth/change-password", mint("recoverme", True),
             {"current_password": "", "new_password": "BrandNew!2026x"})
check("a pwreset session sets a password with no current password",
      st == 200, "status=%s %s" % (st, str(r)[:110]))

st, r = call("POST", "/api/auth/login",
             body={"username": "recoverme", "password": "BrandNew!2026x"})
check("  the new password works", st == 200 and bool((r or {}).get("token")),
      "status=%s" % st)
st, r = call("POST", "/api/auth/login",
             body={"username": "recoverme", "password": "OldPass!2026a"})
check("  and the OLD password no longer does", st != 200,
      "status=%s - the edge has two user stores and a one-store reset "
      "leaves the old password valid" % st)

st, r = call("POST", "/api/auth/change-password", mint("recoverme", False),
             {"current_password": "", "new_password": "Stolen!2026xyz"})
check("an ORDINARY token is still refused a blank current password",
      st == 401, "status=%s - the claim must not become a way for any "
                 "stolen token to rotate a password" % st)
st, r = call("POST", "/api/auth/login",
             body={"username": "recoverme", "password": "Stolen!2026xyz"})
check("  and that password was never set", st != 200, "status=%s" % st)

print()
print("RESULT: {0}".format("PASS" if not FAILS
                           else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
