# -*- coding: utf-8 -*-
"""Dashboards must (a) keep profiles across a restart and (b) be visible to every
surface. Runs against a THROWAWAY backend; never touches the live install."""
import json, os, subprocess, sys, tempfile, time, urllib.error, urllib.request
ROOT = r"D:\Trustnode\Trustnode-AB\Tray_app\Trustnode_edge_app"
PORT = "8071"; API = f"http://127.0.0.1:{PORT}"
FAILS = []
def check(n, ok, d=""):
    print(f"  {n:58s}: {'PASS' if ok else 'FAIL'}{(' - ' + str(d)[:120]) if d else ''}")
    if not ok: FAILS.append(n)

tmp = tempfile.mkdtemp(prefix="tn-dash-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)

def boot():
    p = subprocess.Popen([sys.executable, "-m", "app"], cwd=os.path.join(ROOT, "backend"),
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen(API + "/api/health", timeout=3).read(); return p
        except Exception: time.sleep(2)
    return p

def call(method, path, token=None, body=None, timeout=60):
    h = {"Content-Type": "application/json"}
    if token: h["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300]
    except Exception as e:
        return 0, str(e)[:300]

proc = boot()
st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
admin = (b or {}).get("token") if isinstance(b, dict) else None
check("admin login", st == 200 and bool(admin), f"status={st}")
if not admin:
    proc.kill(); sys.exit(2)

# give the edge an identity so a per-edge shared scope exists at all
st, bs = call("GET", "/api/app-store/bootstrap", admin)
settings = dict((((bs or {}).get("data") or {}).get("app_settings") or {}))
settings.update({"edge_id": "edge-dash-test", "customer_id": "cust-dash", "tenant_id": "default",
                 "edge_profile": {"edge_id": "edge-dash-test", "linked_customer_id": "cust-dash"}})
call("PUT", "/api/app-store/domain", admin, {"domain": "app_settings", "payload": settings, "actor": "t"})

WIDGETS = [{"id": "w1", "type": "value_kpi", "title": "Temp", "x": 0, "y": 0, "w": 17, "h": 15},
           {"id": "w2", "type": "line_area_chart", "title": "Trend", "x": 17, "y": 0, "w": 23, "h": 18}]
PROFILES = [{"name": "test3", "saved_utc": "2026-08-23T10:00:00Z", "widgets": WIDGETS,
             "mode": "kpi", "per_row": 2, "tag_colors": {"Temp": "#14a89a"}}]
st, r = call("PUT", "/api/app-store/domain", admin,
             {"domain": "dashboard_configurations",
              "payload": {"widgets": WIDGETS, "mode": "kpi", "per_row": 2,
                          "tag_colors": {}, "profiles": PROFILES},
              "actor": "admin"})
check("dashboard + profiles saved", st == 200, f"status={st} {str(r)[:90]}")
scope = (r or {}).get("scope_key") if isinstance(r, dict) else ""
print(f"     saved to scope: {scope}")

def read_full():
    _s, _b = call("GET", "/api/app-store/bootstrap", admin)
    return ((_b or {}).get("data") or {}).get("dashboard_configurations") or {}

d = read_full()
check("widgets read back", len(d.get("widgets") or []) == 2, len(d.get("widgets") or []))
check("PROFILES read back (bug 1)", len(d.get("profiles") or []) == 1, d.get("profiles"))
w0 = (d.get("widgets") or [{}])[0]
check("widget size/layout preserved", w0.get("w") == 17 and w0.get("h") == 15 and w0.get("x") == 0,
      {k: w0.get(k) for k in ("x", "y", "w", "h")})
p0 = (d.get("profiles") or [{}])[0]
check("profile keeps its own widget geometry",
      len(p0.get("widgets") or []) == 2 and (p0.get("widgets") or [{}])[0].get("w") == 17,
      p0.get("name"))

# --- surfaces: Lite must see the SAME dashboard (bug 2) --------------------
st, lb = call("GET", "/api/lite-local/bootstrap", admin)
ldash = ((lb or {}).get("data") or {}).get("dashboard_configurations") or {}
check("LITE sees the same widgets (bug 2)", len(ldash.get("widgets") or []) == 2,
      f"lite widgets={len(ldash.get('widgets') or [])}")
check("LITE sees the profiles too", len(ldash.get("profiles") or []) == 1,
      f"lite profiles={len(ldash.get('profiles') or [])}")
for dom, n in (("gateway_configurations", 0), ("tags", 0)):
    check(f"LITE overlay did not damage '{dom}'", dom in ((lb or {}).get('data') or {}), "")

# --- survives a RESTART, which is what the operator actually hit ----------
proc.terminate()
try: proc.wait(timeout=20)
except Exception: proc.kill()
proc = boot()
st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
admin = (b or {}).get("token") if isinstance(b, dict) else None
d2 = read_full()
check("AFTER RESTART widgets still there", len(d2.get("widgets") or []) == 2, len(d2.get("widgets") or []))
check("AFTER RESTART profiles still there (bug 1)", len(d2.get("profiles") or []) == 1, d2.get("profiles"))
w = (d2.get("widgets") or [{}])[0]
check("AFTER RESTART sizes still there", w.get("w") == 17 and w.get("h") == 15,
      {k: w.get(k) for k in ("w", "h")})

# An older client / Local View / Lite saves the dashboard WITHOUT a profiles
# key. The whole document is replaced on write, so without a carry-forward that
# one save destroys every saved layout on the edge.
st, r = call("PUT", "/api/app-store/domain", admin,
             {"domain": "dashboard_configurations",
              "payload": {"widgets": WIDGETS, "mode": "kpi", "per_row": 2, "tag_colors": {}},
              "actor": "legacy-client"})
d3 = read_full()
check("a client that OMITS profiles does not wipe them",
      len(d3.get("profiles") or []) == 1, f"profiles={len(d3.get('profiles') or [])}")
check("  and that client's own widgets still saved", len(d3.get("widgets") or []) == 2,
      len(d3.get("widgets") or []))

# ...but an EXPLICIT empty list is an instruction, not an omission.
call("PUT", "/api/app-store/domain", admin,
     {"domain": "dashboard_configurations",
      "payload": {"widgets": WIDGETS, "mode": "kpi", "per_row": 2, "tag_colors": {}, "profiles": []},
      "actor": "admin"})
d4 = read_full()
check("an EXPLICIT empty profiles list still clears them",
      len(d4.get("profiles") or []) == 0, f"profiles={len(d4.get('profiles') or [])}")

proc.terminate()
try: proc.wait(timeout=15)
except Exception: proc.kill()
print()
print(f"RESULT: {'PASS' if not FAILS else 'FAIL - ' + ', '.join(FAILS)}")
sys.exit(0 if not FAILS else 2)
