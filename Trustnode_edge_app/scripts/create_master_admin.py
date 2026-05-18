"""One-off: create a global/master admin on the VPS.

Runs on the VPS itself, using the backend's own ControlPlaneStore so the
password gets bcrypt-hashed the way the auth path expects. Then fires
mirror_user_upsert so the same login works in the cloud Lite app.

Usage (locally, will SSH and run remotely):
    python scripts/create_master_admin.py
"""
from __future__ import annotations
import io, sys
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import paramiko

HERE = Path(__file__).resolve().parent.parent

REMOTE_PY = r'''
import os, sys, json, time
os.environ.setdefault("TRUSTNODE_DATA_DIR", "/opt/trustnode-edge/data")
sys.path.insert(0, "/opt/trustnode-edge/app/Trustnode_edge_app/backend")

# Pre-load Supabase env from systemd-injected vars (already present via override.conf).
from app.services.control_plane_store import ControlPlaneStore

USERNAME = "master"
PASSWORD = "Apolo020@"
TENANT   = "default"          # required for _is_global_admin
ROLE     = "admin"

# Mirrors buildRolePermissions("admin") in App.jsx, lines 1689-1716.
ADMIN_PERMS = {
    "dashboard": True, "power_overview": True, "historian": True,
    "client_module_alarms": True, "client_module_reporting": True,
    "client_module_interface": True,
    "devices": True, "tags": True, "triggers_and_limits": True,
    "alarms": True, "reporting": True, "data_log": True,
    "gateway_configuration": True, "gateway_runtime_control": True,
    "interface": True, "database": True, "database_overview": True,
    "database_inspector": True, "backup_and_retention": True,
    "control_plane": True, "website_and_env": True,
    "email_and_notifications": True, "scheduled_reports": True,
    "frontend_source": True, "users_and_access_control": True,
}
# Modules: enable all from MODULE_REGISTRY.
ADMIN_MODULES = ["dashboard", "historian", "reporting", "alarms",
                 "interface", "tags", "gateway_configuration",
                 "gateway_runtime_control", "database",
                 "users_and_access_control"]

store = ControlPlaneStore()
row = store.upsert_user(
    tenant_id=TENANT,
    customer_id="",
    username=USERNAME,
    password=PASSWORD,
    role=ROLE,
    status="active",
    email="master@trustnode.local",
    mfa_enabled=False,
    modules=ADMIN_MODULES,
    permissions=ADMIN_PERMS,
)
row.pop("password_hash", None)
print("EDGE UPSERT:")
print(json.dumps({k: row.get(k) for k in
    ["id","tenant_id","customer_id","username","role","status","email","modules","permissions"]},
    indent=2, default=str)[:1200])

# Mirror to Supabase Auth + lite_profiles
try:
    from app.services.lite_user_mirror import mirror_user_upsert
    mirror_user_upsert(
        tenant_id=TENANT,
        username=USERNAME,
        password=PASSWORD,
        role=ROLE,
        email="master@trustnode.local",
    )
    print("\nSUPABASE MIRROR: requested (fire-and-forget)")
except Exception as exc:
    print(f"\nSUPABASE MIRROR FAILED: {exc!r}")

time.sleep(2.5)

# Read back from cp_users to confirm row exists with role=admin/tenant=default
import sqlite3
con = sqlite3.connect("/opt/trustnode-edge/data/trustnode_app_store.db")
con.row_factory = sqlite3.Row
r = con.execute(
    "SELECT username,tenant_id,role,status,email,length(password_hash) AS plen "
    "FROM cp_users WHERE username=? AND tenant_id=?",
    (USERNAME, TENANT),
).fetchone()
print("\nVERIFY EDGE:")
print(dict(r) if r else "  (not found)")
'''


def main() -> int:
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
        remote_path = "/tmp/create_master_admin_remote.py"
        with sftp.file(remote_path, "w") as f:
            f.write(REMOTE_PY)
        sftp.close()
        venv_py = "/opt/trustnode-edge/app/Trustnode_edge_app/backend/.venv/bin/python"
        cmd = f"{venv_py} {remote_path}"
        print(f"$ {cmd}\n")
        _, out, err = c.exec_command(cmd, timeout=60)
        sout = out.read().decode("utf-8", "replace")
        serr = err.read().decode("utf-8", "replace")
        rc = out.channel.recv_exit_status()
        if sout.strip(): print(sout.rstrip())
        if serr.strip(): print("stderr:", serr.rstrip())
        print(f"  exit={rc}")
        # Clean up
        c.exec_command(f"rm -f {remote_path}")
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
