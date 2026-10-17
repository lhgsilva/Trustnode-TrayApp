# -*- coding: utf-8 -*-
"""Clearing the alarms has to survive a restart.

2026-10-17, reported: "check why when we clear the alarms is not cleared and
we restart the app". It never was cleared. Measured in the live store that
afternoon:

    config_documents_scoped, this edge's scope
      domain alarms_setup   alarms=300   version 5647

    app_logs
      "Refused to clear 300 saved item(s) in 'alarms_setup': the request
       carried an empty list."
      12 of them - first 2026-08-26 14:24:59, last 2026-10-17 16:14:48
      (two of those were the operator pressing Clear that afternoon)

`alarms_setup` is a PROTECTED collection: after a headless check blanked three
dashboard widgets on 2026-08-22, the server refuses to replace a saved
collection with an empty list unless the caller states that emptying it was
the point. "Clear All Alarms" only set React state; the save that followed
carried neither `allow_empty` nor a `base_version`, so it was refused - and
the frontend's `catch (_) {}` swallowed the refusal. On screen the list was
empty, in the store it was not, and the next start read the 300 back.

The guard is right. The clear was mute. It now carries the operator's intent,
the same way the dashboard's own clear has since 2026-08-31.

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
PORT = "8171"
API = "http://127.0.0.1:" + PORT
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("TrustNode - a cleared alarm list stays cleared")
print()

tmp = tempfile.mkdtemp(prefix="tn-alarmclear-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
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
        return 0, str(e)[:150]


def finish(code):
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:
        proc.kill()
    sys.exit(code)


def stored():
    _, b = call("GET", "/api/app-store/bootstrap", tok)
    data = (b or {}).get("data") or {}
    vers = (b or {}).get("versions") or {}
    doc = data.get("alarms_setup") or {}
    return (doc.get("alarms") or []), int(vers.get("alarms_setup") or 0)


check("the app started", up)
if not up:
    print(io.open(os.path.join(tmp, "o.log"), encoding="utf-8",
                  errors="replace").read()[-800:])
    finish(2)
tok = (call("POST", "/api/auth/login",
            body={"username": "admin", "password": "admin"})[1] or {}).get("token")
check("  and accepted a login", bool(tok))
if not tok:
    finish(2)

print()
print("[1. a list of alarms is stored]")
ALARMS = [{"id": "al-%03d" % i, "severity": "warning",
           "message": "Temperature high", "ts_utc": "2026-10-17 10:%02d:00" % (i % 60)}
          for i in range(300)]
_, v0 = stored()
st, _ = call("PUT", "/api/app-store/domain", tok,
             {"domain": "alarms_setup", "payload": {"alarms": ALARMS},
              "actor": "test", "base_version": v0})
have, v1 = stored()
check("300 alarms saved", st == 200 and len(have) == 300, "HTTP %s, %d stored" % (st, len(have)))

print()
print("[2. the save the old Clear button made]")
# No allow_empty, no base_version - exactly what App.jsx used to send.
st, _ = call("PUT", "/api/app-store/domain", tok,
             {"domain": "alarms_setup", "payload": {"alarms": []}, "actor": "test"})
have, _v = stored()
check("an unexplained empty list is still REFUSED", st >= 400,
      "HTTP %s - the guard from the 2026-08-22 dashboard wipe" % st)
check("  and the 300 alarms are still there", len(have) == 300,
      "%d stored - this is what came back after every restart" % len(have))

print()
print("[3. the save the fixed Clear button makes]")
have, v2 = stored()
st, body = call("PUT", "/api/app-store/domain", tok,
                {"domain": "alarms_setup", "payload": {"alarms": []},
                 "actor": "test", "allow_empty": True, "base_version": v2})
have, v3 = stored()
check("a deliberate clear is HONOURED", st == 200, "HTTP %s" % st)
check("  the stored list is empty", len(have) == 0, "%d still stored" % len(have))
check("  and the version moved on", v3 > v2, "%s -> %s" % (v2, v3))

print()
print("[4. it survives a restart]")
# The whole complaint: "we restart the app" and they are back. Restarting the
# process re-reads the document from disk, which is the only honest check.
try:
    proc.terminate()
    proc.wait(timeout=30)
except Exception:
    proc.kill()
proc = subprocess.Popen([sys.executable, "-m", "app"],
                        cwd=os.path.join(ROOT, "backend"), env=env,
                        stdout=log, stderr=subprocess.STDOUT)
back = False
for _ in range(70):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read()
        back = True
        break
    except Exception:
        time.sleep(2)
check("the app came back up", back)
if back:
    tok = (call("POST", "/api/auth/login",
                body={"username": "admin", "password": "admin"})[1] or {}).get("token")
    have, _v = stored()
    check("  the alarms are STILL cleared after the restart", len(have) == 0,
          "%d alarm(s) read back from disk" % len(have))

print()
print("[5. the page sends the intent]")
APP = io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
              encoding="utf-8").read()
check("Clear All marks the emptying as deliberate",
      "alarmsClearIntentRef.current = true" in APP,
      "without this the save is indistinguishable from a half-loaded page")
check("  Clear Selected does too (selecting every row empties it)",
      APP.count("alarmsClearIntentRef.current = true") >= 2,
      APP.count("alarmsClearIntentRef.current = true"))
check("  the save forwards it as allowEmpty",
      "{ allowEmpty: emptyIsIntentional }" in APP,
      "intent at the button is no use if the request does not carry it")
check("  and does NOT version this high-churn save",
      'domainVersionsRef.current?.["alarms_setup"]' not in APP,
      "alarms append every few seconds; a base_version here buys nothing and "
      "adds a 409 path to a domain that has never had one")
check("  only an EMPTY payload may be intentional",
      "payload.alarms.length === 0 ? alarmsClearIntentRef.current === true : false" in APP,
      "allow_empty on a non-empty save would be meaningless, and on every "
      "save would reopen the hole the guard exists for")
check("  a refused clear is no longer swallowed",
      "Alarms could not be cleared" in APP,
      "twelve refusals went unnoticed from 2026-08-26 to 2026-10-17 because "
      "the catch block said nothing")
check("  and it does not record the signature on failure",
      "alarmsDomainLastPersistSignatureRef.current = signature;\n        if (emptyIsIntentional)" in APP,
      "recording it would make the page believe the empty list was stored")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
