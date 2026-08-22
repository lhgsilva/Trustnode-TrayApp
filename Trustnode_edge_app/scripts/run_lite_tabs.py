# -*- coding: utf-8 -*-
"""Boot a THROWAWAY backend with a full-module licence, then run the Lite tab
browser check against it. Never touches the live install."""
import json, os, subprocess, sys, tempfile, time, urllib.error, urllib.request

ROOT = r"D:\Trustnode\Trustnode-AB\Tray_app\Trustnode_edge_app"
SCRATCH = os.path.dirname(os.path.abspath(__file__))   # scripts/ — the harness lives beside this runner
PORT = "8049"
API = f"http://127.0.0.1:{PORT}"

tmp = tempfile.mkdtemp(prefix="tn-lite-")
env = dict(os.environ, TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=tmp,
           TRUSTNODE_APP_STORE_PATH=os.path.join(tmp, "s.db"),
           TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_PORT=PORT)
# seed the licence into the store BEFORE the server boots, so license_inspect
# reads it from the unscoped app_settings row on its very first evaluation.
seed = subprocess.run([sys.executable, os.path.join(SCRATCH, "seed_lite_store.py")],
                      cwd=os.path.join(ROOT, "backend"), env=env, capture_output=True, text=True)
print("seed:", seed.stdout.strip().splitlines()[-1] if seed.stdout.strip() else seed.stderr[-300:])

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
token = (b or {}).get("token") if isinstance(b, dict) else None
print("login:", st)

# give this throwaway edge a licence carrying every module, so the Lite
# capability flags can be true.
st, caps = call("GET", "/api/lite-local/capabilities", token)
print("capabilities:", st, json.dumps(caps if isinstance(caps, dict) else str(caps))[:400])

# --- a restricted viewer: the same Lite build must HIDE what they lack ------
VIEWER = "lite-viewer"
st, _ = call("PUT", "/api/app-store/domain", token, {"domain": "users_access", "payload": {
    "users": [{
        "username": VIEWER, "password": "Viewer2026*", "role": "viewer", "status": "active",
        "email": "viewer@example.com",
        # dashboard + historian only: no tags, no batch, no assistant
        "permissions": {"dashboard": True, "historian": True, "access_lite": True,
                        "tags": False, "batch_management": False,
                        "trustnode_intelligence": False},
    }],
}, "actor": "lite-test"})
print("viewer created:", st)
st, vb = call("POST", "/api/auth/login", body={"username": VIEWER, "password": "Viewer2026*"})
viewer_token = (vb or {}).get("token") if isinstance(vb, dict) else None
print("viewer login:", st)
viewer_caps = {}
if viewer_token:
    st, vc = call("GET", "/api/lite-local/capabilities", viewer_token)
    viewer_caps = ((vc or {}).get("capabilities") or {}) if isinstance(vc, dict) else {}
    print("viewer capabilities:", json.dumps(viewer_caps))

shots = os.path.join(SCRATCH, "lite_shots")
os.makedirs(shots, exist_ok=True)
run = subprocess.run(["node", os.path.join(SCRATCH, "test_lite_tabs.js")],
                     cwd=os.path.join(ROOT, "frontend"),
                     env=dict(os.environ, API=API, SHOT_DIR=shots.replace("\\", "/"),
                              CAPS=json.dumps((caps or {}).get("capabilities") or {})),
                     capture_output=True, text=True)
print(run.stdout)
if run.stderr.strip():
    print("STDERR:", run.stderr[-1500:])

rc_viewer = 0
if viewer_token:
    print()
    print("[LITE as a restricted viewer]")
    run2 = subprocess.run(["node", os.path.join(SCRATCH, "test_lite_tabs.js")],
                          cwd=os.path.join(ROOT, "frontend"),
                          env=dict(os.environ, API=API, SHOT_DIR=shots.replace("\\", "/"),
                                   CAPS=json.dumps(viewer_caps),
                                   LOGIN_USER=VIEWER, LOGIN_PASS="Viewer2026*",
                                   SHOT_PREFIX="viewer_"),
                          capture_output=True, text=True)
    print(run2.stdout)
    if run2.stderr.strip():
        print("STDERR:", run2.stderr[-1200:])
    rc_viewer = run2.returncode

proc.terminate()
try:
    proc.wait(timeout=15)
except Exception:
    proc.kill()
sys.exit(run.returncode or rc_viewer)
