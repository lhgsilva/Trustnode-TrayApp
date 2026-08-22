# -*- coding: utf-8 -*-
"""Phase A (access hardening) against a THROWAWAY backend.

Covers: S2 privilege escalation, S4 admin-only reads, S3 loopback enforcement
for non-admins, the viewer-export decision, and the per-user Intelligence gate.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = r"D:\Trustnode\Trustnode-AB\Tray_app\Trustnode_edge_app"
PORT = os.environ.get("TEST_PORT", "8032")
API = f"http://127.0.0.1:{PORT}"
FAILS = []


def check(name, ok, detail=""):
    print(f"  {name:58s}: {'PASS' if ok else 'FAIL'}{(' — ' + str(detail)[:110]) if detail else ''}")
    if not ok:
        FAILS.append(name)


def call(method, path, token=None, body=None, timeout=40):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            try:
                raw = data.decode() or "null"
            except UnicodeDecodeError:
                # binary body (xlsx export) — success is the status + length
                return r.status, f"<{len(data)} bytes>"
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw[:200]
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw[:200]
    except Exception as e:
        return 0, str(e)[:200]


tmp = tempfile.mkdtemp(prefix="tn-phasea-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
# 2026-08-22: seed a licence carrying every module BEFORE boot. Without it every
# module-gated endpoint answers 404 "not licensed" and a per-user permission gate
# can be completely broken while the test still reads green — which is exactly how
# a 422 on every /api/intelligence route survived a passing run.
_seed = os.path.join(tmp, "seed_license.py")
_SEED_SRC = """
import os, sys
sys.path.insert(0, BACKEND)
from app.state import app_store
from app.services.control_plane_store import ControlPlaneStore
mods = [{'key': m['key'], 'enabled': True} for m in ControlPlaneStore.MODULE_CATALOG]
bs = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
s = dict(bs.get('app_settings') or {})
s['license'] = {'license_id': 'lic-test', 'status': 'active',
                'start_utc': '2026-01-01 00:00:00', 'end_utc': '2030-01-01 00:00:00',
                'modules': mods}
app_store.upsert_domain('app_settings', s, actor='access-policy-test')
print('seeded', len(mods))
"""
with open(_seed, "w", encoding="utf-8") as _fh:
    _fh.write(_SEED_SRC.replace("BACKEND", repr(os.path.join(ROOT, "backend"))))
subprocess.run([sys.executable, _seed], cwd=os.path.join(ROOT, "backend"), env=env,
               capture_output=True, text=True)

proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=os.path.join(ROOT, "backend"),
                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(50):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read()
        break
    except Exception:
        time.sleep(2)

st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
admin = (b or {}).get("token")
check("admin login", st == 200 and bool(admin), f"status={st}")
if not admin:
    proc.kill()
    sys.exit(2)

# --- S1: the public health endpoint must not carry secrets -----------------
st, h = call("GET", "/api/health")
mc = ((h or {}).get("license_summary") or {}).get("module_configs") or {}
leaked = []
for module, cfg in mc.items():
    if isinstance(cfg, dict):
        for field, value in cfg.items():
            if any(t in str(field).lower() for t in ("token", "key", "secret", "password")):
                if value and value != "__set__":
                    leaked.append(f"{module}.{field}")
check("S1 public /api/health carries no credentials", not leaked, leaked)

# --- make two non-admin users ---------------------------------------------
for name, role in (("eng-test", "engineer"), ("view-test", "viewer")):
    call("DELETE", f"/api/control-plane/users/{name}", admin)
    st, r = call("POST", "/api/control-plane/users", admin,
                 {"username": name, "password": "PhaseA-2026-xx", "role": role,
                  "status": "active", "permissions": {}, "modules": []})
    check(f"create {role}", st in (200, 201), f"status={st}")

st, b = call("POST", "/api/auth/login", body={"username": "eng-test", "password": "PhaseA-2026-xx"})
eng = (b or {}).get("token")
st, b = call("POST", "/api/auth/login", body={"username": "view-test", "password": "PhaseA-2026-xx"})
viewer = (b or {}).get("token")
check("engineer + viewer can sign in", bool(eng) and bool(viewer))

# --- S2: privilege escalation ----------------------------------------------
ESCALATE = {"users": [{"username": "eng-test", "role": "admin", "permissions": {}}]}
st, r = call("PUT", "/api/app-store/domain", eng,
             {"domain": "users_access", "payload": ESCALATE, "actor": "eng-test"})
check("S2 engineer cannot write users_access (domain)", st == 403, f"status={st}")
st, r = call("PUT", "/api/app-store/bootstrap", eng, {"data": {"users_access": ESCALATE}, "actor": "eng-test"})
check("S2 engineer cannot write users_access (bootstrap)", st == 403, f"status={st}")
st, r = call("PUT", "/api/app-store/domain", viewer,
             {"domain": "users_access", "payload": ESCALATE, "actor": "view-test"})
check("S2 viewer cannot write users_access", st == 403, f"status={st}")
st, b2 = call("POST", "/api/auth/login", body={"username": "eng-test", "password": "PhaseA-2026-xx"})
role_now = ((b2 or {}).get("user") or {}).get("role") or ""
check("S2 the engineer is still an engineer", role_now != "admin", role_now)

# an admin may still do it
st, r = call("PUT", "/api/app-store/domain", admin,
             {"domain": "users_access", "payload": {"users": []}, "actor": "admin", "allow_empty": True})
check("admin may still write users_access", st == 200, f"status={st}")

# --- S4: admin-only reads ---------------------------------------------------
for path, label in (("/api/app-store/logs?limit=5", "logs"),
                    ("/api/control-plane/users", "user list"),
                    ("/api/database/connections", "database connections")):
    st_v, _ = call("GET", path, viewer)
    st_a, _ = call("GET", path, admin)
    check(f"S4 viewer cannot read {label}", st_v == 403, f"viewer={st_v}")
    check(f"S4 admin can still read {label}", st_a in (200, 404), f"admin={st_a}")

# --- reads a View seat is sold for stay open --------------------------------
for path, label in (("/api/plc/gateways/status", "gateway status"),
                    ("/api/app-store/bootstrap", "bootstrap"),
                    ("/api/reports/templates", "report templates")):
    st_v, _ = call("GET", path, viewer)
    check(f"viewer can still read {label}", st_v == 200, f"status={st_v}")

# --- S3: loopback enforcement for non-admins --------------------------------
st, r = call("PUT", "/api/app-store/domain", viewer,
             {"domain": "interface_settings", "payload": {"theme": "dark"}, "actor": "view-test"})
check("S3 viewer mutation on LOOPBACK is denied", st == 403, f"status={st}")
st, r = call("PUT", "/api/app-store/domain", admin,
             {"domain": "interface_settings", "payload": {"theme": "dark"}, "actor": "admin"})
check("S3 admin on loopback is unaffected", st == 200, f"status={st}")

# --- exports: a viewer may export what they can see -------------------------
# The xlsx build imports openpyxl lazily on first use, which can take longer
# than a default urllib timeout on a cold process — give it room and report the
# real error rather than a bare 0.
st, r = call("POST", "/api/historian/export-xlsx", viewer,
             {"rows": [{"ts_utc": "2026-08-22 00:00:00", "tag_name": "t", "value": 1}], "columns": []},
             timeout=120)
check("viewer may export (decision 2026-08-22)", st == 200, f"status={st} {str(r)[:80]}")

# --- Intelligence per-user gate ---------------------------------------------
st_v, r_v = call("GET", "/api/intelligence/status", viewer)
st_a, _ = call("GET", "/api/intelligence/status", admin)
check("Intelligence refused for a viewer without the permission",
      st_v == 403, f"viewer={st_v} {str(r_v)[:80]}")
check("Intelligence still reachable for an admin", st_a == 200, f"admin={st_a}")
# The gate is a router-level dependency. If it is declared with an unannotated
# parameter FastAPI reads it as a required QUERY param and EVERY route answers
# 422 — a break that a 403/404-tolerant assertion cannot see.
st_c, r_c = call("GET", "/api/intelligence/chats", admin)
check("Intelligence routes are not 422 (dependency is well-formed)",
      st_c != 422, f"admin={st_c} {str(r_c)[:90]}")
# and the permission the admin ticks actually opens it for a non-admin
call("PUT", "/api/app-store/domain", admin, {"domain": "users_access", "payload": {"users": [
    {"username": "ai-test", "password": "AiTest2026*", "role": "viewer", "status": "active",
     "permissions": {"trustnode_intelligence": True}},
]}, "actor": "access-policy-test"})
st_l, b_l = call("POST", "/api/auth/login", body={"username": "ai-test", "password": "AiTest2026*"})
ai_token = (b_l or {}).get("token") if isinstance(b_l, dict) else None
st_p, r_p = call("GET", "/api/intelligence/status", ai_token) if ai_token else (0, "no token")
check("Intelligence opens for a viewer who HAS the permission", st_p == 200, f"status={st_p}")

# --- item 10: a shared dashboard may be SEEN by all, CHANGED by few ---------
# Dashboards are one set per edge, so "can open the dashboard" and "can
# rearrange everyone's dashboard" must not be the same permission.
DASH = {"profiles": [{"id": "p1", "name": "Line 1", "widgets": [{"id": "w1", "type": "value"}]}]}


def _dash_user(name, perms):
    call("DELETE", f"/api/control-plane/users/{name}", admin)
    call("POST", "/api/control-plane/users", admin,
         {"username": name, "password": "PhaseA-2026-xx", "role": "engineer",
          "status": "active", "permissions": perms, "modules": []})
    _st, _b = call("POST", "/api/auth/login", body={"username": name, "password": "PhaseA-2026-xx"})
    return (_b or {}).get("token") if isinstance(_b, dict) else None


tok = _dash_user("dash-no", {"dashboard": True, "custom_dashboards": False})
st, r = call("PUT", "/api/app-store/domain", tok,
             {"domain": "dashboard_configurations", "payload": DASH, "actor": "dash-no"})
check("item10 dashboard EDIT denied without the permission", st == 403, f"status={st}")

tok = _dash_user("dash-yes", {"dashboard": True, "custom_dashboards": True})
st, r = call("PUT", "/api/app-store/domain", tok,
             {"domain": "dashboard_configurations", "payload": DASH, "actor": "dash-yes"})
check("item10 dashboard EDIT allowed with the permission", st == 200, f"status={st} {str(r)[:80]}")

# a user document that predates the permission must not lose editing
tok = _dash_user("dash-legacy", {"dashboard": True})
st, r = call("PUT", "/api/app-store/domain", tok,
             {"domain": "dashboard_configurations", "payload": DASH, "actor": "dash-legacy"})
check("item10 a pre-permission user keeps editing", st == 200, f"status={st} {str(r)[:80]}")

# and everyone can still READ the shared set
for who, tk in (("engineer", eng), ("viewer", viewer)):
    st, r = call("GET", "/api/app-store/bootstrap", tk)
    # the bootstrap response nests every domain under `data`
    _dom = (((r or {}).get("data") or {}).get("dashboard_configurations") or {}) if isinstance(r, dict) else {}
    got = len(_dom.get("profiles") or []) if isinstance(_dom, dict) else -1
    check(f"item10 {who} still SEES the shared dashboard", st == 200 and got >= 1, f"status={st} profiles={got}")

for name in ("dash-no", "dash-yes", "dash-legacy", "eng-test", "view-test"):
    call("DELETE", f"/api/control-plane/users/{name}", admin)
proc.terminate()
try:
    proc.wait(timeout=15)
except Exception:
    proc.kill()
print()
print(f"RESULT: {'PASS' if not FAILS else 'FAIL — ' + ', '.join(FAILS)}")
sys.exit(0 if not FAILS else 2)
