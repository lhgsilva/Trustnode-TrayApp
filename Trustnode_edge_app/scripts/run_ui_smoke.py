# -*- coding: utf-8 -*-
"""Run the UI smoke test against a THROWAWAY backend serving the freshly built
frontend bundle. Never touches the live install."""
import json, os, subprocess, sys, tempfile, time, urllib.error, urllib.request
ROOT = r"D:\Trustnode\Trustnode-AB\Tray_app\Trustnode_edge_app"
SCRATCH = os.path.dirname(os.path.abspath(__file__))   # scripts/ — the node harness sits beside this runner
PORT = "8055"; API = f"http://127.0.0.1:{PORT}"
tmp = tempfile.mkdtemp(prefix="tn-uismoke-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
seed = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "seed_lite_store.py")],
                      cwd=os.path.join(ROOT, "backend"), env=env, capture_output=True, text=True)
print("seed:", (seed.stdout.strip().splitlines() or [seed.stderr[-200:]])[-1])
proc = subprocess.Popen([sys.executable, "-m", "app"], cwd=os.path.join(ROOT, "backend"),
                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(60):
    try: urllib.request.urlopen(API + "/api/health", timeout=3).read(); break
    except Exception: time.sleep(2)
run = subprocess.run(["node", os.path.join(ROOT, "scripts", "ui_smoke.js")],
                     cwd=r"D:\Trustnode\Trustnode-AB\Tray_app",
                     env=dict(os.environ, API=API, VAL_USER="admin", VAL_PASS="admin"),
                     capture_output=True, text=True)
print(run.stdout)
if run.stderr.strip(): print("STDERR:", run.stderr[-1200:])
proc.terminate()
try: proc.wait(timeout=15)
except Exception: proc.kill()
sys.exit(run.returncode)
