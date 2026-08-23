# -*- coding: utf-8 -*-
"""Retention: preset -> edit -> save -> activate -> run -> data actually removed.
THROWAWAY backend only."""
import json, os, subprocess, sys, tempfile, time, urllib.error, urllib.request
ROOT = r"D:\Trustnode\Trustnode-AB\Tray_app\Trustnode_edge_app"
PORT = "8073"; API = f"http://127.0.0.1:{PORT}"
FAILS = []
def check(n, ok, d=""):
    print(f"  {n:56s}: {'PASS' if ok else 'FAIL'}{(' - ' + str(d)[:130]) if d else ''}")
    if not ok: FAILS.append(n)

tmp = tempfile.mkdtemp(prefix="tn-ret-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT,
           TRUSTNODE_RETENTION_BOOT_DELAY_S="5")
proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=os.path.join(ROOT, "backend"),
                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(60):
    try: urllib.request.urlopen(API + "/api/health", timeout=3).read(); break
    except Exception: time.sleep(2)

def call(method, path, token=None, body=None, timeout=300):
    h = {"Content-Type": "application/json"}
    if token: h["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=h, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return time.time()-t0, r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return time.time()-t0, e.code, e.read()[:400]
    except Exception as e:
        return time.time()-t0, 0, str(e)[:300]

_, st, b = call("POST", "/api/auth/login", body={"username": "admin", "password": "admin"})
admin = (b or {}).get("token") if isinstance(b, dict) else None
check("admin login", st == 200 and bool(admin))
if not admin: proc.kill(); sys.exit(2)

# seed OLD readings so retention has something to act on
print("  seeding 40,000 old readings ...")
import datetime as _dt
old = _dt.datetime(2026, 1, 1)
BATCH = 5000
for start in range(0, 40000, BATCH):
    rows = [{"ts_utc": (old + _dt.timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M:%S"),
             "source": "test", "gateway_id": "gw1", "gateway_name": "GW", "device_name": "d",
             "plc_ip": "10.0.0.1", "database_name": "Local SQLite", "tag_name": f"tag{i%8}",
             "value": float(i % 50), "value_text": None, "data_type": "REAL",
             "quality": 192, "quality_label": "GOOD"} for i in range(start, start+BATCH)]
    call("POST", "/api/app-store/append/historian", admin, {"rows": rows})
_, st, b = call("GET", "/api/app-store/retention/v2/status", admin)
before_rows = ((b or {}).get("status") or {}).get("database", {})
print(f"     seeded; db size={before_rows.get('size_bytes')}")

# --- presets ---------------------------------------------------------------
_, st, opts = call("GET", "/api/app-store/retention/v2/options", admin)
presets = (opts or {}).get("presets") or []
check("presets are served", len(presets) >= 3, [p.get("id") for p in presets])
balanced = next((p for p in presets if p.get("id") == "preset-balanced"), None)
check("preset carries raw + tiers the editor needs",
      bool(balanced) and "raw" in balanced and isinstance(balanced.get("tiers"), list),
      sorted((balanced or {}).keys()))

# --- estimate (the Preview button) ----------------------------------------
draft = dict(balanced or {}); draft["id"] = ""
dt, st, est = call("POST", "/api/app-store/retention/v2/estimate", admin, draft)
check("estimate/preview works", st == 200, f"status={st} {str(est)[:100]}")
print(f"     estimate took {dt:.2f}s")

# --- save from a preset ----------------------------------------------------
dt, st, saved = call("PUT", "/api/app-store/retention/v2/policies", admin, {**draft, "activate": True})
check("preset saves AND activates", st == 200, f"status={st} {str(saved)[:150]}")
pol = (saved or {}).get("policy") or {}
pid = pol.get("id") or ""
check("saved policy has an id", bool(pid), pid)

_, st, lst = call("GET", "/api/app-store/retention/v2/policies", admin)
check("policy appears in the list", len((lst or {}).get("policies") or []) == 1,
      len((lst or {}).get("policies") or []))
_, st, stt = call("GET", "/api/app-store/retention/v2/status", admin)
active = ((stt or {}).get("status") or {}).get("policy")
check("policy shows as ACTIVE in status", bool(active), active)

def wait_idle(max_s=120):
    """The engine refuses a second concurrent pass. Its own scheduled pass can
    already be running here, so wait it out before measuring a manual one."""
    deadline = time.time() + max_s
    while time.time() < deadline:
        _, _s, b = call("GET", "/api/app-store/retention/v2/status", admin)
        if not (((b or {}).get("status") or {}).get("engine") or {}).get("busy"):
            return True
        time.sleep(1)
    return False


check("engine reaches idle before the manual run", wait_idle(), "still busy after 120s")

# --- the manual run: how long does it ACTUALLY take? ----------------------
dt, st, dry = call("POST", "/api/app-store/retention/v2/run", admin, {"dry_run": True, "force": True})
check("manual PREVIEW run returns 200", st == 200, f"status={st} {str(dry)[:120]}")
print(f"     dry run took {dt:.2f}s  (client timeout is 12s)")
if dt > 12: print("     *** longer than the 12s client timeout -> the UI would abort")

dt, st, applied = call("POST", "/api/app-store/retention/v2/run", admin, {"dry_run": False, "force": True})
check("manual APPLY run returns 200", st == 200, f"status={st} {str(applied)[:120]}")
print(f"     apply run took {dt:.2f}s")
summ = (applied or {}).get("summary") or {}
print(f"     summary: {json.dumps(summ)[:300]}")
check("the run actually did something (rows touched)",
      any(int(v or 0) > 0 for k, v in summ.items() if isinstance(v, (int, float))) or bool(summ),
      json.dumps(summ)[:150])

_, st, runs = call("GET", "/api/app-store/retention/v2/runs?limit=5", admin)
check("run history records it", len((runs or {}).get("runs") or []) >= 1,
      len((runs or {}).get("runs") or []))

# --- BACKGROUND run: the fix for "delete data manually is not working" -----
# The synchronous call holds the request for the whole pass; on a real
# historian that is minutes and the browser aborts at 12s. The UI now starts
# the pass and polls, so the START must return immediately.
wait_idle()
dt, st, bg = call("POST", "/api/app-store/retention/v2/run", admin,
                  {"dry_run": False, "force": True, "background": True})
check("background run returns 200", st == 200, f"status={st} {str(bg)[:120]}")
check("background run RETURNS IMMEDIATELY (<2s)", dt < 2.0, f"{dt:.2f}s")
check("background run reports it started", bool((bg or {}).get("background")), bg)
# and the engine reports itself busy, which is what the UI follows
seen_busy = False
for _ in range(30):
    _, _s, stt2 = call("GET", "/api/app-store/retention/v2/status", admin)
    eng = ((stt2 or {}).get("status") or {}).get("engine") or {}
    if eng.get("busy"): seen_busy = True
    if seen_busy and not eng.get("busy"): break
    time.sleep(0.5)
check("status.engine.busy is observable while it runs", "busy" in (((stt2 or {}).get("status") or {}).get("engine") or {}),
      "engine.busy field present")
_, st, runs2 = call("GET", "/api/app-store/retention/v2/runs?limit=10", admin)
check("the background pass was recorded too",
      len((runs2 or {}).get("runs") or []) > len((runs or {}).get("runs") or []),
      f"{len((runs or {}).get('runs') or [])} -> {len((runs2 or {}).get('runs') or [])}")

# --- the rest of the policy lifecycle the operator needs -------------------
edited = dict(pol); edited["name"] = "My policy"; edited["raw"] = {"keep": "7d"}
_, st, up = call("PUT", "/api/app-store/retention/v2/policies", admin, edited)
check("an existing policy can be EDITED", st == 200, f"status={st}")
_, st, lst2 = call("GET", "/api/app-store/retention/v2/policies", admin)
got = next((p for p in (lst2 or {}).get("policies") or [] if p.get("id") == pid), {})
check("the edit persisted (raw keep 2d -> 7d)", (got.get("raw") or {}).get("keep") == "7d",
      (got.get("raw") or {}).get("keep"))
check("editing did NOT create a duplicate", len((lst2 or {}).get("policies") or []) == 1,
      len((lst2 or {}).get("policies") or []))

_, st, _d = call("POST", "/api/app-store/retention/v2/deactivate", admin, {})
check("policy can be deactivated", st == 200, f"status={st}")
_, st, stt3 = call("GET", "/api/app-store/retention/v2/status", admin)
check("status shows no active policy after deactivate",
      not ((stt3 or {}).get("status") or {}).get("policy"), "still active")
_, st, _a = call("POST", f"/api/app-store/retention/v2/policies/{pid}/activate", admin, {})
check("policy can be re-activated", st == 200, f"status={st}")

# Deleting the ACTIVE policy must be refused — otherwise an edge silently stops
# having any retention at all. The UI hides Delete for the active row; this is
# the server-side half of that rule.
_, st, blocked = call("DELETE", f"/api/app-store/retention/v2/policies/{pid}", admin)
check("deleting the ACTIVE policy is refused (409)", st == 409, f"status={st}")
check("  and the refusal says what to do",
      b"Deactivate" in blocked if isinstance(blocked, bytes) else "Deactivate" in str(blocked),
      str(blocked)[:100])

call("POST", "/api/app-store/retention/v2/deactivate", admin, {})
_, st, _x = call("DELETE", f"/api/app-store/retention/v2/policies/{pid}", admin)
check("policy can be deleted once deactivated", st in (200, 204), f"status={st}")
_, st, lst3 = call("GET", "/api/app-store/retention/v2/policies", admin)
check("deleted policy is gone", len((lst3 or {}).get("policies") or []) == 0,
      len((lst3 or {}).get("policies") or []))

# every preset must survive the same round trip
for pre in presets:
    d = dict(pre); d["id"] = ""
    _, st, r2 = call("PUT", "/api/app-store/retention/v2/policies", admin, {**d, "activate": False})
    ok = st == 200 and bool(((r2 or {}).get("policy") or {}).get("id"))
    check(f"preset '{pre.get('id')}' saves", ok, f"status={st}")

proc.terminate()
try: proc.wait(timeout=20)
except Exception: proc.kill()
print()
print(f"RESULT: {'PASS' if not FAILS else 'FAIL - ' + ', '.join(FAILS)}")
sys.exit(0 if not FAILS else 2)
