# -*- coding: utf-8 -*-
"""An operator must not be told their configuration failed to save.

2026-10-22, from a customer edge, signed in as a user with role
`operator`:

    Database connections could not be saved: App store domain save failed
    (HTTP 403): Forbidden: role 'operator' may not PUT /api/app-store/domain

The backend is RIGHT to refuse - access_policy gives a config mutation
CONFIG_ROLES = {admin, super, engineer}. The fault was that the UI tried
anyway: seven debounced auto-savers (dashboards, gateways, devices,
database connections, power, triggers, tags) were guarded only by
isReadOnlySession(), which treats just viewer and client as read-only. An
operator sailed through all seven, each fired a PUT, each came back 403,
and simply opening a page reported a configuration failure.

This pins both halves:
  * the backend still refuses an operator and still allows an engineer;
  * every auto-saver asks first, and asks using currentUser.role - NOT
    the module-level cache in api.js, which is filled by its own effect
    and would read empty if a saver ran first. That would skip the write
    for an ADMIN too: a silently lost config save, which is the failure
    this file has been bitten by before.
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
PORT = os.environ.get("TN_CFG_ROLES_PORT", "8109")
API = "http://127.0.0.1:" + PORT
FAILS = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(
        name, "PASS" if ok else "FAIL", (" - " + str(detail)[:120]) if not ok else ""))
    if not ok:
        FAILS.append(name)


# ---------------------------------------------------------------- the UI
print("An operator is not told their configuration failed to save")
print()
app = io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"), encoding="utf-8").read()

guards = app.count("if (!canWriteSharedConfigNow()) return;")
check("every auto-saver asks before writing", guards >= 7,
      "%d guarded; a missed one still 403s for an operator" % guards)
check("  no saver is still guarded only by isReadOnlySession",
      "isReadOnlySession()) return; // Phase B" not in app,
      "that guard lets an operator through")

m = re.search(r"const canWriteSharedConfigNow = useCallback\(\(\) => \{(.{0,600}?)\}, \[", app, re.S)
body = m.group(1) if m else ""
check("the guard reads the role from currentUser", "currentUser?.role" in body,
      "the api.js session-role cache is filled by its own effect; reading it "
      "here would skip an ADMIN's write if a saver ran first")
check("  and allows exactly admin / super / engineer",
      all(r in body for r in ('"admin"', '"super"', '"engineer"')),
      "must mirror CONFIG_ROLES in access_policy.py")

# ----------------------------------------------------------- the backend
tmp = tempfile.mkdtemp(prefix="tn-cfgroles-")
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


def call(method, path, token=None, body=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:160]
    except Exception as e:
        return 0, str(e)[:160]


def finish(code):
    try:
        proc.terminate(); proc.wait(timeout=20)
    except Exception:
        proc.kill()
    sys.exit(code)


st, r = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
admin_tok = (r or {}).get("token") if isinstance(r, dict) else None
if not admin_tok:
    check("signed in to set the fixture up", False, "status=%s" % st)
    finish(2)

TEN = "default"
for uname, role, pw in (("operator", "operator", "OpPass!2026aa"),
                        ("eng1", "engineer", "EngPass!2026a")):
    call("POST", "/api/control-plane/users?tenant_id=" + TEN, admin_tok,
         {"username": uname, "password": pw, "role": role, "status": "active"})

PAYLOAD = {"domain": "database_configurations",
           "payload": [{"id": "c1", "name": "Local", "type": "sqlite"}],
           "actor": "test"}

st, r = call("POST", "/api/auth/login", body={"username": "operator", "password": "OpPass!2026aa"})
op_tok = (r or {}).get("token") if isinstance(r, dict) else None
check("a user NAMED operator with role operator can log in", bool(op_tok),
      "status=%s %s" % (st, str(r)[:90]))
if op_tok:
    st, r = call("PUT", "/api/app-store/domain", op_tok, PAYLOAD)
    check("  the backend still refuses their config write", st == 403,
          "status=%s - the UI must not ask" % st)

st, r = call("POST", "/api/auth/login", body={"username": "eng1", "password": "EngPass!2026a"})
eng_tok = (r or {}).get("token") if isinstance(r, dict) else None
check("an engineer can log in", bool(eng_tok), "status=%s" % st)
if eng_tok:
    st, r = call("PUT", "/api/app-store/domain", eng_tok, PAYLOAD)
    check("  AND their config write is allowed", st == 200,
          "status=%s %s - config must not become admin-only" % (st, str(r)[:80]))

st, r = call("PUT", "/api/app-store/domain", admin_tok, PAYLOAD)
check("an admin can still write configuration", st == 200, "status=%s" % st)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
