# -*- coding: utf-8 -*-
"""A public reset must never hand the caller the reset token.

2026-10-20 (SECURITY). `POST /api/control-plane/password-reset/public/issue`
took a username, took NO credentials, and returned the token in its own
response body:

    row = control_plane_store.issue_password_reset(...)
    return {"ok": True, "tenant_id": tid, "row": row}
    #        row carries "reset_token": secrets.token_urlsafe(28)

`password-reset/public/apply` then accepted that token with a new password.
Two unauthenticated requests took over any account, `master` included. The
route was confirmed reachable on the public portal (an empty username
answered 400 username_required, which exercises only the validation branch
and issues nothing).

A reset code is a secret only while it travels to the ACCOUNT OWNER. It now
goes out by email through Supabase and never back to the caller, every
outcome returns the same sentence so the endpoint cannot be used to discover
usernames, and the whole thing is rate limited per IP because it sends mail.

This drives a REAL backend on a scratch workspace: the response body is the
thing that leaked, so the test reads the response body.
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
PORT = "8177"
API = "http://127.0.0.1:" + PORT
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("TrustNode - a public reset emails a code and returns nothing secret")
print()

tmp = tempfile.mkdtemp(prefix="tn-reset-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
# No Supabase here on purpose: delivery must FAIL and the endpoint must still
# answer identically. A test that only passes when mail works would not have
# caught the leak.
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


def call(path, body=None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
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

print()
print("[1. the response body carries no secret]")
status, body = call("/api/control-plane/password-reset/public/issue",
                    {"username": "admin"})
check("the request is accepted", status == 200, "HTTP %s" % status)
low = body.lower()
check("  no reset_token in the body", "reset_token" not in low,
      "this single field was the whole vulnerability: %s" % body[:160])
check("  no token-ish field at all",
      not any(k in low for k in ('"token"', '"code"', '"secret"')),
      body[:160])
check("  and no row object is echoed", '"row"' not in low, body[:160])

print()
print("[2. it cannot be used to discover usernames]")
_s1, b1 = call("/api/control-plane/password-reset/public/issue",
               {"username": "definitely-not-a-real-user-zz"})
_s2, b2 = call("/api/control-plane/password-reset/public/issue",
               {"username": "admin"})
check("an unknown user gets the same answer as a known one", b1 == b2,
      "different replies are a free enumeration oracle:\n      %s\n      %s"
      % (b1[:90], b2[:90]))
check("  and the answer does not promise delivery happened",
      "has been sent to it" in b1.lower() or "if that account" in b1.lower(),
      b1[:120])

print()
print("[3. it is rate limited - it sends email]")
codes = []
for i in range(9):
    st, _ = call("/api/control-plane/password-reset/public/issue",
                 {"username": "admin"})
    codes.append(st)
check("repeated requests are eventually refused", 429 in codes,
      "saw %s - an unauthenticated endpoint that sends mail is a spam "
      "amplifier without this" % codes)

print()
print("[4. apply still refuses the obvious]")
st, _ = call("/api/control-plane/password-reset/public/apply",
             {"username": "admin", "reset_token": "whatever", "new_password": "short"})
check("a short password is refused", st == 400, "HTTP %s" % st)
st, _ = call("/api/control-plane/password-reset/public/apply",
             {"username": "admin", "reset_token": "", "new_password": "longenough123"})
check("  a missing code is refused", st == 400, "HTTP %s" % st)

print()
print("[5. the source says why, for whoever edits it next]")
SRC = io.open(os.path.join(ROOT, "backend", "app", "routers",
                           "control_plane.py"), encoding="utf-8").read()
check("the issue path no longer returns row",
      'return {"ok": True, "tenant_id": tid, "row": row}\n' not in
      SRC[SRC.index("password-reset/public/issue"):
          SRC.index("password-reset/public/apply")],
      "the exact line that leaked")
check("  the reason is recorded next to the code",
      "account takeover in two" in SRC or "never back to the caller" in SRC,
      "a future 'why not just return it, it is easier' needs an answer here")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
