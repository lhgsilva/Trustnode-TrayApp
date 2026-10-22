# -*- coding: utf-8 -*-
"""An activation must bring the licence's MODULE ENTITLEMENTS with it.

2026-10-22, reported from a new customer: the licence was created with a
subset of modules ticked, the edge activated fine, and then every menu
showed anyway.

A brand-new machine activates from the LOGIN SCREEN, so nobody is signed
in and the request carries no Authorization header. The edge tried to
enrich the licence itself with two upstream GETs:

    GET /api/control-plane/licenses?tenant_id=...
    GET /api/control-plane/licenses/{id}/modules

forwarding that absent header. Both were refused, the failure was
swallowed by a bare `except Exception: pass`, and `license.modules`
arrived empty. local-finalize skips set_license_modules on an empty list,
so the edge held NO entitlements - and a licence with no modules gates
nothing, which is why everything showed.

The activation code is the authorisation: it already proved which
tenant, customer, edge and licence this machine may have. So
/activation-code/apply answers WITH the licence and its modules, and a
fresh install needs no second, authenticated call.

This drives the real endpoints on a throwaway workspace.
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
PORT = "8104"
API = "http://127.0.0.1:" + PORT
FAILS = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(
        name, "PASS" if ok else "FAIL", (" - " + str(detail)[:120]) if detail else ""))
    if not ok:
        FAILS.append(name)


tmp = tempfile.mkdtemp(prefix="tn-actmods-")
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
        with urllib.request.urlopen(req, timeout=60) as r:
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


# The break-glass admin is the only account on a blank edge.
st, r = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
token = (r or {}).get("token") if isinstance(r, dict) else None
if not token:
    check("signed in to set the fixture up", False, "status={0}".format(st))
    finish(2)

TEN = "tenant-cust-mods"
WANTED = ["dashboard", "historian"]
NOT_WANTED = "reporting"

call("POST", "/api/control-plane/tenants", token, {"tenant_id": TEN, "name": "Mods"})
call("POST", "/api/control-plane/customers?tenant_id=" + TEN, token,
     {"customer_id": "cust-mods", "company_name": "Mods", "contact_email": "mods@example.com"})
st, lic = call("POST", "/api/control-plane/licenses?tenant_id=" + TEN, token,
               {"license_id": "lic-mods", "customer_id": "cust-mods", "status": "active"})
st, edge = call("POST", "/api/control-plane/edges?tenant_id=" + TEN, token,
                {"edge_id": "edge-mods", "edge_name": "Mods Edge", "customer_id": "cust-mods"})

# Only a SUBSET is licensed - that is the whole point.
st, r = call("PUT", "/api/control-plane/licenses/lic-mods/modules", token,
             {"modules": [{"module_key": k, "enabled": True} for k in WANTED]
                         + [{"module_key": NOT_WANTED, "enabled": False}]})
check("the licence records only the ticked modules", st == 200,
      "status={0} {1}".format(st, str(r)[:90]))

st, issued = call("POST", "/api/control-plane/activation-code/issue", token,
                  {"customer_id": "cust-mods", "edge_id": "edge-mods",
                   "license_id": "lic-mods", "ttl_minutes": 30})
code = ((issued or {}).get("row") or {}).get("activation_code") if isinstance(issued, dict) else None
check("an activation code was issued", bool(code), "status={0}".format(st))
if not code:
    finish(2)

# THE POINT: a brand-new machine applies the code with NO Authorization
# header, exactly as the login screen does.
st, applied = call("POST", "/api/control-plane/activation-code/apply", None,
                   {"activation_code": code, "edge_id": "edge-mods"})
check("the code applies without being signed in", st == 200,
      "status={0} {1}".format(st, str(applied)[:90]))

lic_obj = (applied or {}).get("license") if isinstance(applied, dict) else None
check("the response carries the LICENCE", isinstance(lic_obj, dict) and bool(lic_obj),
      "without it the edge must make an authenticated call it cannot make")
mods = (lic_obj or {}).get("modules") if isinstance(lic_obj, dict) else None
check("  and the module entitlements with it", isinstance(mods, list) and bool(mods),
      "modules={0}".format(str(mods)[:80]))

enabled = {str(m.get("module_key")) for m in (mods or []) if isinstance(m, dict) and m.get("enabled")}
for key in WANTED:
    check("  '{0}' is licensed".format(key), key in enabled, sorted(enabled))
check("  '{0}' is NOT licensed".format(NOT_WANTED), NOT_WANTED not in enabled,
      "an edge that licenses everything gates nothing")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
finish(0 if not FAILS else 2)
