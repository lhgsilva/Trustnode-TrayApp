# -*- coding: utf-8 -*-
"""Phase G: prove the portal can ISSUE seats and that an edge reads them back.

Runs against a THROWAWAY backend + throwaway store. Never touches the live install.
"""
import json, os, subprocess, sys, tempfile, time, urllib.error, urllib.request

ROOT = r"D:\Trustnode\Trustnode-AB\Tray_app\Trustnode_edge_app"
PORT = "8047"
API = f"http://127.0.0.1:{PORT}"
FAILS = []


def check(name, ok, detail=""):
    print(f"  {name:58s}: {'PASS' if ok else 'FAIL'}{(' - ' + str(detail)[:120]) if detail else ''}")
    if not ok:
        FAILS.append(name)


tmp = tempfile.mkdtemp(prefix="tn-seats-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=os.path.join(ROOT, "backend"),
                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(60):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read(); break
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
        return e.code, e.read()[:300]
    except Exception as e:
        return 0, str(e)[:300]


st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
admin = (b or {}).get("token") if isinstance(b, dict) else None
check("admin login", st == 200 and bool(admin), f"status={st}")
if not admin:
    proc.kill(); sys.exit(2)

CUST = "cust-seats-1"
st, _ = call("POST", "/api/control-plane/customers", admin,
             {"customer_id": CUST, "company_name": "Seat Co", "contact_email": "a@b.c"})
check("customer created", st == 200, f"status={st}")

SEATS = {"edge_runtime": 2, "studio": 3, "view_lan": 10, "cloud_view": 5}
st, b = call("POST", "/api/control-plane/licenses", admin, {
    "license_id": "lic-seats-1", "customer_id": CUST, "plan_code": "enterprise",
    "status": "active", "start_utc": "2026-01-01 00:00:00", "end_utc": "2030-01-01 00:00:00",
    "max_edges": 5, "max_users": 25, "package_key": "enterprise",
    "seats": SEATS, "limits": {"max_view_users": 10},
})
check("licence issued WITH seats", st == 200, f"status={st} {b if st != 200 else ''}")
row = ((b or {}).get("row") or {}) if isinstance(b, dict) else {}
try:
    stored = json.loads(row.get("seats_json") or "{}")
except Exception:
    stored = {}
check("seats persisted on the licence row", stored == SEATS, stored)
check("package_key persisted", str(row.get("package_key") or "") == "enterprise", row.get("package_key"))
try:
    stored_limits = json.loads(row.get("limits_json") or "{}")
except Exception:
    stored_limits = {}
check("limits persisted", stored_limits == {"max_view_users": 10}, stored_limits)

st, b = call("GET", "/api/control-plane/licenses", admin)
rows = ((b or {}).get("rows") or []) if isinstance(b, dict) else []
hit = next((r for r in rows if str(r.get("license_id")) == "lic-seats-1"), None)
check("licence list returns the seat column", bool(hit) and "seats_json" in (hit or {}),
      sorted((hit or {}).keys())[:6])

st, b = call("POST", "/api/control-plane/licenses", admin, {
    "license_id": "lic-seats-1", "customer_id": CUST, "plan_code": "enterprise",
    "status": "active", "max_edges": 5, "max_users": 25,
})
row2 = ((b or {}).get("row") or {}) if isinstance(b, dict) else {}
try:
    kept = json.loads(row2.get("seats_json") or "{}")
except Exception:
    kept = {}
check("a seat-less upsert leaves seats intact", kept == SEATS, kept)

# --- the edge side ---------------------------------------------------------
probe = os.path.join(tmp, "probe.py")
open(probe, "w", encoding="utf-8").write(
    "import json, sys\n"
    "sys.path.insert(0, r'%s')\n" % os.path.join(ROOT, "backend") +
    "from app.services import license_inspect as li\n"
    "lic = json.load(open(sys.argv[1], encoding='utf-8'))\n"
    "seats, explicit = li._parse_seats(lic, lic.get('limits') or {})\n"
    "print(json.dumps({'seats': seats, 'explicit': explicit}))\n"
)

bundle_path = os.path.join(tmp, "lic.json")
sys.path.insert(0, os.path.join(ROOT, "backend"))
from app.services.control_plane_store import _with_tier_fields  # noqa: E402
bundle = _with_tier_fields({"license_id": "lic-seats-1", "modules": []}, row)
json.dump(bundle, open(bundle_path, "w", encoding="utf-8"))
check("bundle carries a parsed seats dict", bundle.get("seats") == SEATS, bundle.get("seats"))
check("bundle carries package_key", bundle.get("package_key") == "enterprise", bundle.get("package_key"))

out = subprocess.run([sys.executable, probe, bundle_path], capture_output=True, text=True,
                     cwd=os.path.join(ROOT, "backend"), env=env)
try:
    parsed = json.loads(out.stdout.strip().splitlines()[-1])
except Exception:
    parsed = {"err": (out.stdout[-200:] + out.stderr[-300:])}
check("the edge parses the seats as EXPLICIT", parsed.get("explicit") is True, parsed)
check("the edge reads the same seat counts", parsed.get("seats") == SEATS, parsed.get("seats"))

legacy = _with_tier_fields({"license_id": "old", "modules": []},
                           {"seats_json": "{}", "limits_json": "", "package_key": ""})
check("a pre-seat licence carries no seats block", "seats" not in legacy, legacy)
json.dump({"license_id": "old", "modules": [], "limits": {"max_view_users": 4}},
          open(bundle_path, "w", encoding="utf-8"))
out = subprocess.run([sys.executable, probe, bundle_path], capture_output=True, text=True,
                     cwd=os.path.join(ROOT, "backend"), env=env)
try:
    parsed = json.loads(out.stdout.strip().splitlines()[-1])
except Exception:
    parsed = {"err": out.stdout[-200:]}
check("a pre-seat licence stays IMPLICIT (no behaviour change)", parsed.get("explicit") is False, parsed)
check("  and still derives view_lan from max_view_users",
      (parsed.get("seats") or {}).get("view_lan") == 4, parsed.get("seats"))

proc.terminate()
try:
    proc.wait(timeout=15)
except Exception:
    proc.kill()
print()
print(f"RESULT: {'PASS' if not FAILS else 'FAIL - ' + ', '.join(FAILS)}")
sys.exit(0 if not FAILS else 2)
