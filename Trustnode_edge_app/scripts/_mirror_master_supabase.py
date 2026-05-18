"""Run the Supabase mirror for 'master' synchronously on the VPS.

The fire-and-forget thread inside mirror_user_upsert died with the
parent process when the create script exited too fast. This script
calls the same internals but inline, so the create/profile-upsert
finishes before we return.
"""
from __future__ import annotations
import io, sys
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import paramiko

HERE = Path(__file__).resolve().parent.parent

REMOTE = r'''
import os, sys
sys.path.insert(0, "/opt/trustnode-edge/app/Trustnode_edge_app/backend")

# Env is injected via paramiko exec_command env={} below, but paramiko
# only sets allowed-listed vars on the sshd side. Prefer a local /tmp
# file we write right before invocation.
env_file = "/tmp/_mirror_env.sh"
if os.path.isfile(env_file):
    for line in open(env_file).read().splitlines():
        s = line.strip()
        if not s or "=" not in s: continue
        k, v = s.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

print("supabase_url set:", bool(os.environ.get("TRUSTNODE_SUPABASE_URL")))
print("service_key set:", bool(os.environ.get("TRUSTNODE_SUPABASE_SERVICE_KEY")))

# Call the mirror internals synchronously.
from app.services import lite_user_mirror as lum

cfg = lum._supabase_cfg()
if not cfg:
    print("ABORT: no Supabase config")
    sys.exit(1)
url, key = cfg

USERNAME = "master"
EMAIL    = "master@trustnode.local"
PASSWORD = "Apolo020@"
TENANT   = "default"
ROLE     = "admin"

email_addr = lum._email_for(USERNAME, EMAIL)
existing = lum._find_user_by_email(url, key, email_addr)
print(f"find_user_by_email({email_addr}): {existing.get('id') if existing else None}")
if existing is None:
    created = lum._create_user(url, key, email_addr, PASSWORD,
                               username=USERNAME, role=ROLE, tenant_id=TENANT)
    if not created:
        print("FAIL: _create_user returned None")
        sys.exit(2)
    user_id = str(created.get("id"))
    print(f"CREATED user_id={user_id}")
else:
    user_id = str(existing.get("id"))
    ok = lum._update_user(url, key, user_id, password=PASSWORD,
                         username=USERNAME, role=ROLE, tenant_id=TENANT)
    print(f"UPDATED user_id={user_id} ok={ok}")

ok = lum._upsert_lite_profile(user_id=user_id, tenant_id=TENANT,
                              username=USERNAME, email=email_addr, role=ROLE)
print(f"lite_profiles upsert ok={ok}")
print("DONE")
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
    with sftp.file("/tmp/_mirror_master.py", "w") as f:
        f.write(REMOTE)
    # Write the env file the remote script picks up.
    env_lines = []
    for k in ("TRUSTNODE_SUPABASE_URL", "TRUSTNODE_SUPABASE_SERVICE_KEY",
              "TRUSTNODE_CLOUD_DB_URL", "TRUSTNODE_CLOUD_DB_PASSWORD"):
        v = env.get(k, "")
        if v: env_lines.append(f"{k}={v}")
    with sftp.file("/tmp/_mirror_env.sh", "w") as f:
        f.write("\n".join(env_lines) + "\n")
    sftp.chmod("/tmp/_mirror_env.sh", 0o600)
    sftp.close()
    cmd = ("/opt/trustnode-edge/app/Trustnode_edge_app/backend/.venv/bin/python "
           "/tmp/_mirror_master.py")
    print(f"$ {cmd}\n")
    _, out, err = c.exec_command(cmd, timeout=90)
    sout = out.read().decode("utf-8", "replace")
    serr = err.read().decode("utf-8", "replace")
    rc = out.channel.recv_exit_status()
    if sout.strip(): print(sout.rstrip())
    if serr.strip(): print("stderr:", serr.rstrip())
    print(f"exit={rc}")
    c.exec_command("rm -f /tmp/_mirror_master.py /tmp/_mirror_env.sh")
finally:
    c.close()
