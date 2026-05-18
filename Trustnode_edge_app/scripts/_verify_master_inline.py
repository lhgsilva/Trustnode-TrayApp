"""SSH to the VPS, run a verifier that reads cp_users row + checks Supabase."""
from __future__ import annotations
import io, sys
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import paramiko

HERE = Path(__file__).resolve().parent.parent

REMOTE = r'''
import sqlite3, os, sys
con = sqlite3.connect("/opt/trustnode-edge/data/trustnode_app_store.db")
con.row_factory = sqlite3.Row
r = con.execute(
    "SELECT username,tenant_id,role,status,email,modules_json,permissions_json,length(password_hash) AS plen "
    "FROM cp_users WHERE username='master'"
).fetchone()
print("EDGE ROW:")
if not r:
    print("  (not found!)")
    sys.exit(1)
d = dict(r)
for k in ("username","tenant_id","role","status","email","plen"):
    print(f"  {k}: {d.get(k)}")
print(f"  modules_json: {d.get('modules_json')}")
print(f"  permissions_json[:300]: {(d.get('permissions_json') or '')[:300]}")

# Now check Supabase
sys.path.insert(0, "/opt/trustnode-edge/app/Trustnode_edge_app/backend")
import requests, json
url = os.environ.get("TRUSTNODE_SUPABASE_URL", "")
key = os.environ.get("TRUSTNODE_SUPABASE_SERVICE_KEY", "")
if not (url and key):
    print("\nSUPABASE: env vars missing in this shell, but daemon thread reads them from systemd")
else:
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    r = requests.get(f"{url}/auth/v1/admin/users", headers=h, params={"page": 1, "per_page": 200}, timeout=15)
    users = r.json().get("users", []) if r.status_code == 200 else []
    found = [u for u in users if (u.get("user_metadata") or u.get("raw_user_meta_data") or {}).get("username") == "master"]
    print(f"\nSUPABASE auth.users (master): {len(found)} match(es), http={r.status_code}")
    for u in found:
        meta = u.get("user_metadata") or u.get("raw_user_meta_data") or {}
        print(f"  id={u.get('id')}  email={u.get('email')}  username={meta.get('username')}  tenant={meta.get('tenant_id')}  role={meta.get('role')}")
'''

env = {}
for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s: continue
    k, v = s.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(hostname=env["VPS_HOST"], port=int(env.get("VPS_PORT") or "22"),
          username=env["VPS_USER"], password=env["VPS_PASSWORD"],
          timeout=15, allow_agent=False, look_for_keys=False)
try:
    sftp = c.open_sftp()
    with sftp.file("/tmp/_verify_master.py", "w") as f:
        f.write(REMOTE)
    sftp.close()
    # Source the systemd environment so the script sees TRUSTNODE_SUPABASE_*
    cmd = (
        'set -a; source /etc/systemd/system/trustnode-backend.service.d/override.conf 2>/dev/null; set +a; '
        '/opt/trustnode-edge/app/Trustnode_edge_app/backend/.venv/bin/python /tmp/_verify_master.py'
    )
    _, out, err = c.exec_command(cmd, timeout=60)
    sout = out.read().decode("utf-8", "replace")
    serr = err.read().decode("utf-8", "replace")
    rc = out.channel.recv_exit_status()
    if sout.strip(): print(sout.rstrip())
    if serr.strip(): print("stderr:", serr.rstrip())
    print(f"exit={rc}")
    c.exec_command("rm -f /tmp/_verify_master.py")
finally:
    c.close()
