# -*- coding: utf-8 -*-
"""Prove /api/health.workspace names a substituted workspace instead of letting
it look like data loss. Uses a THROWAWAY store; never writes to the real one."""
import json, os, subprocess, sys, tempfile, time, urllib.request
ROOT = r"D:\Trustnode\Trustnode-AB\Tray_app\Trustnode_edge_app"
FAILS = []
def check(n, ok, d=""):
    print(f"  {n:56s}: {'PASS' if ok else 'FAIL'}{(' - ' + str(d)[:130]) if d else ''}")
    if not ok: FAILS.append(n)

def boot(port, store_path, data_dir, home=None):
    env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=data_dir,
               TRUSTNODE_APP_STORE_PATH=store_path,
               TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=str(port))
    if home:
        # Move the fake machine's HOME so default_data_dir() resolves inside the
        # throwaway tree: this is how the "normal install" case is exercised
        # without ever opening the operator's real 9.8 GB store.
        env["USERPROFILE"] = home
        env["HOME"] = home
    p = subprocess.Popen([sys.executable, "-m", "app"], cwd=os.path.join(ROOT, "backend"),
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3).read(); return p
        except Exception: time.sleep(2)
    return p

def health(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=15) as r:
        return json.loads(r.read().decode())

# A: a substitute EMPTY workspace, while the machine's real one has data.
tmp = tempfile.mkdtemp(prefix="tn-wsA-")
p = boot(8061, os.path.join(tmp, "s.db"), tmp)
t0 = time.time(); h = health(8061); dt = time.time() - t0
ws = h.get("workspace") or {}
print("  workspace:", json.dumps(ws)[:300])
check("health carries a workspace block", bool(ws))
check("a substitute workspace is NOT reported as default", ws.get("is_default") is False, ws.get("data_dir"))
check("names the override source", bool(ws.get("override_source")), ws.get("override_source"))
check("flags that it is hiding real data", ws.get("hiding_real_data") is True,
      f"has_data_here={ws.get('has_data_here')} default_has_data={ws.get('default_has_data')}")
check("the warning names both directories",
      bool(ws.get("warning")) and str(ws.get("default_data_dir") or "") in str(ws.get("warning")),
      (ws.get("warning") or "")[:120])
check("health stays fast (workspace probe is cached)", dt < 2.0, f"{dt:.2f}s")
t1 = time.time(); health(8061); check("second health call is instant", time.time() - t1 < 1.0, f"{time.time()-t1:.2f}s")
p.terminate()
try: p.wait(timeout=15)
except Exception: p.kill()

# B: a NORMAL install — the default workspace for its machine. Must stay silent.
home = tempfile.mkdtemp(prefix="tn-wsB-home-")
default_dir = os.path.join(home, ".trustnode_edge", "data")
os.makedirs(default_dir, exist_ok=True)
print()
p = boot(8062, os.path.join(default_dir, "trustnode_app_store.db"), default_dir, home=home)
ws = (health(8062).get("workspace") or {})
print("  workspace:", json.dumps(ws)[:220])
check("a default workspace reports is_default", ws.get("is_default") is True, ws.get("data_dir"))
check("a default workspace raises NO warning", not ws.get("hiding_real_data") and not ws.get("warning"),
      ws.get("warning") or "silent")
check("no override source on a normal install", not ws.get("override_source"), ws.get("override_source"))
p.terminate()
try: p.wait(timeout=15)
except Exception: p.kill()
print()
print(f"RESULT: {'PASS' if not FAILS else 'FAIL - ' + ', '.join(FAILS)}")
sys.exit(0 if not FAILS else 2)
