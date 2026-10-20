# -*- coding: utf-8 -*-
"""Sign in and recover with an emailed code, and change a password while in.

2026-10-20, requested: "instead of forget password, the users can request a
code, and I want to use supabase features for that ... we should have a way
to change the password when logged using the user logged in button, together
with logout option", and sign-in by code alone ("email should be enough").

What this pins:

  * The three code endpoints are PUBLIC. Every one of them is for somebody
    who cannot sign in; behind the auth gate they are unreachable by the only
    people who need them. The first version of this shipped behind the gate
    and every call answered 401 - caught by running it, not by reading it.
  * They answer identically whether or not the account exists, so they cannot
    be used to discover usernames.
  * A wrong code is refused, and a weak password is refused by the SAME
    policy the link flow uses - a second way in must not be a weaker way in.
  * The emailed code never appears in any response body.
  * The user menu offers Change password beside Logout, and the fresh token
    it receives is stored under the key the rest of the app reads.

Supabase is deliberately NOT configured here. Delivery therefore fails, and
the endpoints must still behave: a test that only passes when mail works
would not catch a response that leaks.

Brings up its own backend on a scratch workspace; never the live store.
"""
from __future__ import annotations

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
PORT = "8185"
API = "http://127.0.0.1:" + PORT
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("TrustNode - sign in by code, recover by code, change password in-app")
print()

tmp = tempfile.mkdtemp(prefix="tn-logincode-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
env.pop("TRUSTNODE_SUPABASE_URL", None)
env.pop("TRUSTNODE_SUPABASE_ANON_KEY", None)

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


def call(path, body):
    req = urllib.request.Request(
        API + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode() if e.fp else "")
    except Exception as e:
        return 0, str(e)[:160]


def finish(code):
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    sys.exit(code)


check("the app started", up)
if not up:
    print(io.open(os.path.join(tmp, "o.log"), encoding="utf-8",
                  errors="replace").read()[-800:])
    finish(2)

print("[1. the code endpoints are reachable WITHOUT a session]")
# This is the whole point: the caller is locked out. 401 here means the
# feature cannot be used by anyone who needs it.
for path, body in (
    ("/api/auth/login-code/request", {"identifier": "admin"}),
    ("/api/auth/login-code/verify", {"identifier": "admin", "code": "000000"}),
    ("/api/auth/reset-password/code",
     {"identifier": "admin", "code": "000000", "new_password": "longenough1"}),
):
    st, _b = call(path, body)
    check("  %-34s is not behind the auth gate" % path.replace("/api/auth", ""),
          st != 401 or "code is not valid" in _b,
          "HTTP %s %s" % (st, _b[:90]))

print()
print("[2. requesting a code gives nothing away]")
s1, b1 = call("/api/auth/login-code/request", {"identifier": "admin"})
s2, b2 = call("/api/auth/login-code/request", {"identifier": "no-such-user-zz"})
check("a known user is accepted", s1 == 200, "HTTP %s" % s1)
check("  an unknown user gets the SAME reply", b1 == b2,
      "a different answer is a free enumeration oracle:\n      %s\n      %s"
      % (b1[:90], b2[:90]))
for field in ("code", "token", "secret", "otp"):
    check("  no %-6s in the body" % field, ('"%s"' % field) not in b1.lower(),
          b1[:120])

print()
print("[3. a wrong code is refused, and so is a weak password]")
st, b = call("/api/auth/login-code/verify", {"identifier": "admin", "code": "000000"})
check("a wrong sign-in code is rejected", st == 401, "HTTP %s %s" % (st, b[:80]))
check("  and it does not say whether the user exists",
      "not valid" in b.lower(),
      "the same wording as a wrong password: %s" % b[:90])
st, b = call("/api/auth/reset-password/code",
             {"identifier": "admin", "code": "000000", "new_password": "longenough1"})
check("a wrong recovery code is rejected", st == 400, "HTTP %s" % st)
st, b = call("/api/auth/reset-password/code",
             {"identifier": "admin", "code": "000000", "new_password": "short"})
check("  a short password is refused before the code is even checked",
      st == 400 and "8 characters" in b,
      "a second way in must not be a weaker way in: %s" % b[:90])

print()
print("[4. forgot-password prefers a code, keeps the link for offline]")
AUTH = io.open(os.path.join(ROOT, "backend", "app", "routers", "auth.py"),
               encoding="utf-8").read()
check("it sends a code by default", 'delivery: str = "code"' in AUTH)
check("  and still falls back to the link",
      "fall through to the link" in AUTH,
      "an edge with no internet cannot be reached by Supabase, and that is "
      "exactly the machine whose operator is locked out")
check("  the offline file recovery is untouched",
      "/api/auth/local-recovery/request" in io.open(
          os.path.join(ROOT, "backend", "app", "main.py"),
          encoding="utf-8").read())

print()
print("[5. Supabase does the work - no second code store]")
REC = io.open(os.path.join(ROOT, "backend", "app", "services",
                           "recovery_codes.py"), encoding="utf-8").read()
check("codes are requested from Supabase", "/auth/v1/otp" in REC)
check("  verified by Supabase", "/auth/v1/verify" in REC)
check("  and a placeholder address is not treated as deliverable",
      "trustnode.local" in REC,
      "telling an operator a code is on its way to a mailbox nobody reads "
      "is worse than telling them it failed")

print()
print("[6. change password while signed in]")
APP = io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
              encoding="utf-8").read()
check("the user menu offers Change password", "Change password" in APP)
check("  next to Logout", "<span>Logout</span>" in APP)
check("  a voluntary change can be cancelled",
      "forceChangePassword.voluntary ? (" in APP,
      "a forced change has no way out by design; one the user opened "
      "must be escapable or the menu item is a trap")
check("  the new token is stored under the key the app READS",
      'setAuthToken(String(res.token))' in APP
      and 'localStorage.setItem("auth_token"' not in APP,
      "it wrote auth_token while every reader uses trustnode_auth_token, so "
      "changing your password left the app holding the invalidated one")

print()
print("[7. the login screen never shows the code]")
LOGIN = io.open(os.path.join(ROOT, "frontend", "src", "components", "Login",
                             "Login.jsx"), encoding="utf-8").read()
check("it no longer reads reset_token from the response",
      "res?.row?.reset_token" not in LOGIN,
      "this put the code on screen for whoever was standing there")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
