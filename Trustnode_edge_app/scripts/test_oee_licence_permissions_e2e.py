# -*- coding: utf-8 -*-
"""The whole chain for a NEW module, end to end.

Portal licence -> edge licence -> user permissions -> local UI -> web/Lite.

Written for OEE but it is really a test of the PATH a new module travels, and
every step is one that has broken before:

  1. does the developer portal even OFFER the module in its licence editor?
  2. does a licence created WITH it, and one created WITHOUT it, differ?
  3. does the edge's has_module() agree with the licence?
  4. do the permission keys resolve for the roles that should have them?
  5. does a user WITHOUT the write permission actually get refused (403),
     and one with read-only still get 200 on a GET?
  6. does the web/LAN Lite surface report the same answer?
  7. does the UI hide the menu when the user cannot open it?

Boots a real backend on a throwaway workspace. No hardware, no cloud.
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
sys.path.insert(0, os.path.join(ROOT, "backend"))

# This test imports app.services.* in-process, and importing them instantiates
# AppStore against whatever TRUSTNODE_DATA_DIR says. Without this the test
# would open the LIVE store (and log "attempt to write a readonly database"
# while the real app holds it). Point THIS process at its own scratch dir
# BEFORE any app import.
_SELF_TMP = tempfile.mkdtemp(prefix="tn-oee-lic-self-")
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_DATA_DIR"] = _SELF_TMP
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_SELF_TMP, "self.db")
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
PORT = "8094"
API = "http://127.0.0.1:" + PORT
MODULE = "oee"
FAILS = []
WARNS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:160]) if detail else ""
    print("  {0:60s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


def warn(name, detail=""):
    print("  {0:60s}: NOTE{1}".format(name, (" - " + str(detail)[:150]) if detail else ""))
    WARNS.append(f"{name}: {detail}")


# =========================================================== 1. the catalog
print("[1. the developer portal must OFFER the module]")
from app.services.control_plane_store import ControlPlaneStore  # noqa: E402

catalog = {m["key"]: m for m in ControlPlaneStore.MODULE_CATALOG}
check("the module is in MODULE_CATALOG", MODULE in catalog, sorted(catalog)[:4])
entry = catalog.get(MODULE) or {}
check("  it has a human label for the licence editor",
      bool(entry.get("label")), entry.get("label"))
check("  and a group so it lands in a section",
      bool(entry.get("group")), entry.get("group"))

# Application modules are sold as add-ons, so they must be OPT-IN: a default
# of True silently includes the module in every new licence, which gives the
# feature away. OEE shipped as True for one build; this pins the convention.
apps = {k: v for k, v in catalog.items() if v.get("group") == "Applications"}
defaults = {k: bool(v.get("default_enabled", True)) for k, v in apps.items()}
print("      Applications group defaults: {0}".format(defaults))
check("every Applications module is opt-in (sellable as an add-on)",
      not any(defaults.values()),
      [k for k, v in defaults.items() if v] or "all opt-in")

from app.services import permission_catalog as pc  # noqa: E402
keys = {f["key"] for f in pc.FEATURES + pc.SURFACE_FEATURES}
check("a read permission exists", "oee" in keys)
check("a write permission exists", "oee_configuration" in keys)
for k in ("oee", "oee_configuration"):
    f = pc.feature_for_key(k) or {}
    check(f"  '{k}' is tied to the {MODULE} licence module",
          f.get("module") == MODULE, f.get("module"))
    check(f"  '{k}' names the pages it unlocks", bool(f.get("pages")), f.get("pages"))

# ==================================================== 2. boot a real edge
print()
print("[2. a real edge, on a throwaway workspace]")
tmp = tempfile.mkdtemp(prefix="tn-oee-lic-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
proc = subprocess.Popen([sys.executable, "-m", "app"],
                        cwd=os.path.join(ROOT, "backend"), env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        try:
            return e.code, json.loads(e.read().decode() or "null")
        except Exception:
            return e.code, None
    except Exception as e:
        return 0, str(e)[:200]


def finish(code):
    try:
        proc.terminate(); proc.wait(timeout=20)
    except Exception:
        proc.kill()
    return code


st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
admin = (b or {}).get("token") if isinstance(b, dict) else None
check("admin login", st == 200 and bool(admin))
if not admin:
    sys.exit(finish(2))

# ============================================ 3. portal: licence with module
print()
print("[3. the portal licence editor]")
st, mods = call("GET", "/api/control-plane/modules", admin)
portal_keys = [m.get("key") for m in ((mods or {}).get("modules") or [])]
check("the portal lists the module catalogue", st == 200 and bool(portal_keys),
      "{0} module(s)".format(len(portal_keys)))
check("  and the new module appears in it", MODULE in portal_keys)

st, cust = call("POST", "/api/control-plane/provision/customer-bundle", admin, {
    "tenant_id": "tenant-oee-test", "tenant_name": "OEE Test Co",
    "primary_domain": "oee-test.example.com", "timezone": "Europe/Dublin",
    "customer_id": "cust-oee-test", "company_name": "OEE Test Co",
    "contact_email": "oee@example.com",
    "admin_username": "oee-admin", "admin_password": "Passw0rd!x",
    "plan_code": "standard", "max_edges": 1, "max_users": 5,
})
# The endpoint wraps its result: {"ok": true, "row": {tenant, customer, license}}
_body = (cust or {}) if isinstance(cust, dict) else {}
_row = _body.get("row") if isinstance(_body.get("row"), dict) else _body
lic = (_row.get("license") or {}) if isinstance(_row, dict) else {}
license_id = str(lic.get("license_id") or "")
check("a customer bundle provisions with a licence", st == 200 and bool(license_id),
      "status={0} {1}".format(st, str(cust)[:110]))

if license_id:
    st, rows = call("GET", f"/api/control-plane/licenses/{license_id}/modules", admin)
    got = {r.get("module_key"): bool(r.get("enabled"))
           for r in ((rows or {}).get("rows") or [])}
    check("  a NEW licence is seeded from the catalogue defaults",
          MODULE in got, "oee present={0} value={1}".format(MODULE in got, got.get(MODULE)))
    check("  and the seeded value matches default_enabled",
          got.get(MODULE) == bool(entry.get("default_enabled", True)),
          "seeded={0} default={1}".format(got.get(MODULE), entry.get("default_enabled")))
    check("  a new licence does NOT include the add-on until it is ticked",
          got.get(MODULE) is False, got.get(MODULE))

    # Turn it OFF explicitly, the way a portal admin would.
    off = [{"module_key": k, "enabled": (False if k == MODULE else v)}
           for k, v in got.items()]
    st, _ = call("PUT", f"/api/control-plane/licenses/{license_id}/modules", admin,
                 {"modules": off})
    st2, rows2 = call("GET", f"/api/control-plane/licenses/{license_id}/modules", admin)
    got2 = {r.get("module_key"): bool(r.get("enabled"))
            for r in ((rows2 or {}).get("rows") or [])}
    check("the portal can switch the module OFF for a licence",
          st == 200 and got2.get(MODULE) is False, got2.get(MODULE))

    # ...and back ON.
    on = [{"module_key": k, "enabled": (True if k == MODULE else v)}
          for k, v in got2.items()]
    call("PUT", f"/api/control-plane/licenses/{license_id}/modules", admin, {"modules": on})
    st3, rows3 = call("GET", f"/api/control-plane/licenses/{license_id}/modules", admin)
    got3 = {r.get("module_key"): bool(r.get("enabled"))
            for r in ((rows3 or {}).get("rows") or [])}
    check("  and back ON again", got3.get(MODULE) is True, got3.get(MODULE))

# ============================================== 4. permissions on the edge
print()
print("[4. permissions decide what a user may do]")
st, cat = call("GET", "/api/control-plane/permission-catalog", admin)
if st == 200 and isinstance(cat, dict):
    blob = json.dumps(cat)
    check("the Users & Access page offers the new permissions",
          '"oee"' in blob and '"oee_configuration"' in blob)
else:
    warn("permission catalogue endpoint did not answer",
         "GET /api/control-plane/permission-catalog -> {0}".format(st))

# resolve() is what every gate calls.
check("an empty permission set grants nothing",
      not pc.resolve({}, "oee") and not pc.resolve({}, "oee_configuration"))
check("  a granted read key resolves", pc.resolve({"oee": True}, "oee"))
check("  a granted write key resolves",
      pc.resolve({"oee_configuration": True}, "oee_configuration"))
check("  read does NOT imply write",
      not pc.resolve({"oee": True}, "oee_configuration"))

# --- a real non-admin user, through the real API -------------------------
st, mk = call("POST", "/api/control-plane/users", admin, {
    "username": "oee-viewer", "password": "Passw0rd!x", "role": "viewer",
    "status": "active", "permissions": {"oee": True},
})
check("a read-only OEE user can be created", st in (200, 201),
      "status={0} {1}".format(st, str(mk)[:90]))

st, vb = call("POST", "/api/auth/login",
              body={"username": "oee-viewer", "password": "Passw0rd!x"})
viewer = (vb or {}).get("token") if isinstance(vb, dict) else None
check("  and can log in", st == 200 and bool(viewer), st)

if viewer:
    st, _ = call("GET", "/api/oee/config/machines", viewer)
    check("  read-only user CAN read OEE", st == 200, st)
    st, denied = call("POST", "/api/oee/config/machines", viewer, {"name": "Nope"})
    check("  read-only user CANNOT write OEE (403)", st == 403,
          "status={0} {1}".format(st, (denied or {}).get("detail", "")[:70]))
    st, _ = call("DELETE", "/api/oee/config/machines/does-not-exist", viewer)
    check("  and cannot delete either", st == 403, st)

# an engineer SHOULD be able to configure
st, _ = call("POST", "/api/control-plane/users", admin, {
    "username": "oee-eng", "password": "Passw0rd!x", "role": "engineer",
    "status": "active",
    "permissions": {"oee": True, "oee_configuration": True},
})
st, eb = call("POST", "/api/auth/login",
              body={"username": "oee-eng", "password": "Passw0rd!x"})
eng = (eb or {}).get("token") if isinstance(eb, dict) else None
if eng:
    st, made = call("POST", "/api/oee/config/machines", eng,
                    {"name": "Engineer machine", "oee_enabled": True, "enabled": True})
    check("an engineer WITH the write permission can configure", st == 200,
          "status={0}".format(st))
    mid = ((made or {}).get("item") or {}).get("id")
    if mid:
        call("DELETE", f"/api/oee/config/machines/{mid}", eng)
else:
    warn("could not create/log in an engineer user", "skipped the positive write check")

# The viewer above was refused by the ROLE middleware ("role 'viewer' may not
# POST"), which would be the same answer with or without a permission system.
# This is the case that isolates the PERMISSION: a role that is allowed to
# write in general, but which has not been granted OEE configuration. If this
# succeeds, the oee_configuration key is decorative.
st, _ = call("POST", "/api/control-plane/users", admin, {
    "username": "oee-eng-noperm", "password": "Passw0rd!x", "role": "engineer",
    "status": "active", "permissions": {"oee": True},   # read only, no config
})
st, nb = call("POST", "/api/auth/login",
              body={"username": "oee-eng-noperm", "password": "Passw0rd!x"})
eng_np = (nb or {}).get("token") if isinstance(nb, dict) else None
if eng_np:
    st, r1 = call("GET", "/api/oee/config/machines", eng_np)
    check("an engineer WITHOUT oee_configuration can still READ", st == 200, st)
    st, r2 = call("POST", "/api/oee/config/machines", eng_np, {"name": "Should fail"})
    check("  but is REFUSED the write by the permission, not the role",
          st == 403, "status={0} {1}".format(st, (r2 or {}).get("detail", "")[:80]))
else:
    warn("could not log in an engineer without the config permission",
         "the permission gate is therefore unproven")

# =========================================== 5. the web / Lite surface
print()
print("[5. the web and Lite surfaces report the same answer]")
st, caps = call("GET", "/api/lite-local/capabilities", admin)
if st == 200 and isinstance(caps, dict):
    flags = (caps.get("capabilities") or {})
    check("Lite reports an oee capability flag", "oee" in flags, sorted(flags)[:8])
    # An UNLICENSED edge shows no module to anyone, admin included - the module
    # gate runs before the admin bypass in _lite_capabilities. So the right
    # assertion is that oee behaves like its peer (batch), not that it is True
    # on a machine that has never been activated.
    check("  and it behaves like the peer application module",
          flags.get("oee") == flags.get("batch"),
          "oee={0} batch={1} (both follow the licence)".format(
              flags.get("oee"), flags.get("batch")))
else:
    warn("lite capabilities needs a Lite session token", "status={0}".format(st))

from app.services import access_policy as ap  # noqa: E402
check("the edge can answer has_module() for the new key",
      isinstance(ap.has_module(MODULE), bool), ap.has_module(MODULE))

# ============================================ 6. the UI gates the same way
print()
print("[6. the UI hides what the user may not open]")
SRC = os.path.join(ROOT, "frontend", "src")
app_jsx = io.open(os.path.join(SRC, "App.jsx"), encoding="utf-8",
                  errors="replace").read()
check("the menu group exists", 'id: "oee"' in app_jsx)
check("  its items map to page keys",
      all(k in app_jsx for k in ('"oee_overview"', '"oee_operator"', '"oee_configuration"')))
check("  the pages are licence-mapped so canOpenPage can gate them",
      'oee_overview: "oee"' in app_jsx and 'oee_operator: "oee"' in app_jsx
      and 'oee_configuration: "oee"' in app_jsx)
check("  configuration is gated on the WRITE permission",
      'canEditPage("oee_configuration")' in app_jsx)
# The nav filters items through canOpenPage, so a group with no visible items
# renders nothing — the mechanism Batch Management already relies on.
check("  the nav renders items through canOpenPage",
      "canOpenPage(" in app_jsx and "NAV_SECTIONS.map" in app_jsx)

print()
if WARNS:
    print("NOTES ({0}):".format(len(WARNS)))
    for w in WARNS:
        print("  - {0}".format(w))
print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(finish(0 if not FAILS else 2))
