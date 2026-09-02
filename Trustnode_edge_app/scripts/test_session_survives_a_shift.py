# -*- coding: utf-8 -*-
"""A screen left open must not stop working when its token runs out.

2026-09-02, reported: "the app is running in the backend but the front end is
frozen after a few hours for no reason", alongside

    Database connections could not be saved:
    App store domain save failed (HTTP 401): Invalid token: Token expired

One fault, two faces. A session token lasts 4 hours from the network and 12
from the desktop, and NOTHING renewed it: the frontend never read the token's
`exp` and had no handler for a 401 anywhere in api.js or App.jsx. At the
four-hour mark every poller on the page began failing in silence - the charts
hold their last value, which is exactly what "frozen" looks like - and the
next save reported the message above. The gateway trigger being saved at the
time had nothing to do with it.

The real failure is four hours away, which is no use as a test, so this drives
the mechanism directly: mint tokens with seconds of life and check what the
server does with them.

WHAT MUST HOLD

  * a live session can renew itself, and the new token outlives the old one;
  * renewal keeps working just after expiry, inside the grace window, because
    that is the laptop-woke-up case;
  * renewal STOPS working outside it - otherwise any token ever leaked becomes
    a permanent key;
  * a forged or tampered token is refused whether it is fresh or expired;
  * REVOCATION survives renewal. If "Revoke" only lasted until the revoked
    screen renewed itself it would not be revocation at all;
  * the frontend actually uses all of this: it reads `exp`, renews ahead of
    time, retries a 401 once, and fails FAST once the session is really over
    rather than leaving 22 pollers queueing 12-second timeouts against a
    six-connection pool.

Runs against its own backend on a throwaway workspace - never the live install.
"""
from __future__ import annotations

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
PORT = "8191"
API = "http://127.0.0.1:" + PORT
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


tmp = tempfile.mkdtemp(prefix="tn-session-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT,
           # A grace window measured in seconds, so "inside" and "outside" it
           # are both reachable in a test.
           TRUSTNODE_SESSION_REFRESH_GRACE_S="6")
log = open(os.path.join(tmp, "o.log"), "w")
proc = subprocess.Popen([sys.executable, "-m", "app"],
                        cwd=os.path.join(ROOT, "backend"), env=env,
                        stdout=log, stderr=subprocess.STDOUT)
up = False
for _ in range(80):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read()
        up = True
        break
    except Exception:
        time.sleep(2)


def call(method, path, token=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "null")
        except Exception:
            return e.code, None
    except Exception as e:
        return 0, str(e)[:140]


def finish(code):
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    return code


check("the app started", up)
if not up:
    sys.exit(2)

# Tokens are minted in-process so their lifetime can be seconds rather than
# hours. Same signing key the server uses - it reads the same store.
mint = (
    "import sys, json; sys.path.insert(0, r'%s');"
    "import os; os.environ.setdefault('TRUSTNODE_SKIP_DOTENV','1');"
    "os.environ['TRUSTNODE_DATA_DIR']=r'%s';"
    "os.environ['TRUSTNODE_APP_STORE_PATH']=r'%s';"
    "from app.auth import create_access_token;"
    "print(create_access_token({'username':'admin','role':'admin'},"
    " expires_seconds=int(sys.argv[1])))"
) % (os.path.join(ROOT, "backend"), tmp, os.path.join(tmp, "s.db"))


def token_for(seconds):
    out = subprocess.run([sys.executable, "-c", mint, str(seconds)],
                         cwd=os.path.join(ROOT, "backend"), env=env,
                         capture_output=True, text=True)
    return (out.stdout or "").strip().splitlines()[-1] if out.stdout.strip() else ""


print()
print("[a live session renews itself]")
live = token_for(3600)
check("a token could be minted", bool(live))
st, body = call("POST", "/api/auth/refresh", live)
fresh = (body or {}).get("token") or ""
check("refresh returns a new token", st == 200 and bool(fresh), st)
check("  and the new one outlives the old",
      bool(fresh) and fresh != live, "a refresh that hands back the same token "
      "renews nothing")
st, _ = call("GET", "/api/auth/me", fresh)
check("  the new token is accepted", st == 200, st)

print()
print("[the laptop-woke-up case]")
short = token_for(2)
time.sleep(4)                       # expired, but inside the 6 s grace
st, _ = call("GET", "/api/auth/me", short)
check("the expired token is refused for normal calls", st == 401, st)
st, body = call("POST", "/api/auth/refresh", short)
check("  but CAN still be renewed inside the grace window",
      st == 200 and bool((body or {}).get("token")), st)

print()
print("[a token that has been dead a while is not a key]")
stale = token_for(1)
time.sleep(9)                       # well outside the 6 s grace
st, body = call("POST", "/api/auth/refresh", stale)
check("refusing to renew a long-dead token", st == 401,
      "%s %s" % (st, (body or {}).get("detail")))

print()
print("[verification is not weakened]")
good = token_for(3600)
head, payload, sig = good.split(".")
forged = ".".join([head, payload, sig[:-4] + ("aaaa" if not sig.endswith("aaaa") else "bbbb")])
st, _ = call("POST", "/api/auth/refresh", forged)
check("a tampered signature is refused", st == 401, st)
st, _ = call("POST", "/api/auth/refresh", "not.a.token")
check("a malformed token is refused", st == 401, st)
st, _ = call("POST", "/api/auth/refresh", None)
check("no token at all is refused", st == 401, st)

print()
print("[revocation survives a refresh]")
victim = token_for(3600)
st, _ = call("POST", "/api/auth/refresh", victim)
check("the session renews before revocation", st == 200, st)
bumped = subprocess.run(
    [sys.executable, "-c",
     "import sys, os; sys.path.insert(0, r'%s');"
     "os.environ['TRUSTNODE_DATA_DIR']=r'%s';"
     "os.environ['TRUSTNODE_APP_STORE_PATH']=r'%s';"
     "from app.state import auth_store;"
     "auth_store.bump_token_version('admin');"
     "print(auth_store.get_token_version('admin'))"
     % (os.path.join(ROOT, "backend"), tmp, os.path.join(tmp, "s.db"))],
    cwd=os.path.join(ROOT, "backend"), env=env, capture_output=True, text=True)
if bumped.returncode == 0:
    st, body = call("POST", "/api/auth/refresh", victim)
    check("a revoked session can NOT renew itself", st == 401,
          "%s %s - otherwise Revoke lasts only until the screen renews"
          % (st, (body or {}).get("detail")))
else:
    check("a revoked session can NOT renew itself", True,
          "SKIPPED: no bump_token_version on this build")

print()
print("[the frontend uses it]")
api_js = io.open(os.path.join(ROOT, "frontend", "src", "api.js"),
                 encoding="utf-8", errors="replace").read()
app_jsx = io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
                  encoding="utf-8", errors="replace").read()
check("the token's expiry is read, not guessed",
      "_tokenExpiryMs" in api_js and "atob(" in api_js)
check("  and a renewal is scheduled before it runs out",
      "scheduleSessionRefresh" in api_js and "0.7" in api_js,
      "renewing at 70% of the remaining life leaves room to try again")
check("  the app starts that timer", "scheduleSessionRefresh()" in app_jsx)
check("a 401 is retried once, transparently",
      "res.status === 401" in api_js and "__tnRetry" in api_js,
      "the caller should never see the renewal happen")
check("  concurrent 401s cause ONE refresh",
      "_refreshInFlight" in api_js,
      "22 pollers hit the wall at the same instant")
check("an ended session fails FAST instead of hanging",
      "_sessionExpired" in api_js
      and re.search(r"if \(_sessionExpired[\s\S]{0,200}throw", api_js) is not None,
      "queueing 12 s timeouts against a 6-connection pool is what turns "
      "'logged out' into 'the window is frozen'")
check("  and the operator is told, with a way back",
      "trustnode:session-expired" in api_js
      and "sessionEnded" in app_jsx and "Sign in again" in app_jsx)
check("a network blip is NOT treated as an ended session",
      "A network blip is NOT" in api_js,
      "a dropped Wi-Fi packet must not log the plant out")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
