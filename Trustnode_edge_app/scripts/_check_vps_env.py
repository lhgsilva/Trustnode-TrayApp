"""Check whether the running trustnode-backend process has Supabase env vars."""
from __future__ import annotations
import io, sys
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import paramiko

HERE = Path(__file__).resolve().parent.parent
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

# Multi-line bash via /tmp file to avoid escaping hell.
SH = r"""
PID=$(systemctl show -p MainPID --value trustnode-backend)
echo "MainPID=$PID"
tr '\0' '\n' < /proc/$PID/environ | grep -E '^(TRUSTNODE_SUPABASE_URL|TRUSTNODE_SUPABASE_SERVICE_KEY|TRUSTNODE_CLOUD_DB)=' | awk -F= '{
  key=$1
  val=$2
  for (i=3; i<=NF; i++) val=val"="$i
  # Redact secret keys, just show first 12 chars
  if (key ~ /(SERVICE_KEY|PASSWORD)/) {
    printf "%s=%s...(len=%d)\n", key, substr(val,1,12), length(val)
  } else {
    print key"="val
  }
}'
echo "---tail of journal---"
journalctl -u trustnode-backend --since "10 minutes ago" --no-pager | grep -iE 'lite-user-mirror|supabase|mirror.*fail|exception|traceback' | tail -30
"""

sftp = c.open_sftp()
with sftp.file("/tmp/_check_env.sh", "w") as f:
    f.write(SH)
sftp.close()

_, out, err = c.exec_command("bash /tmp/_check_env.sh", timeout=30)
print(out.read().decode("utf-8", "replace"))
e = err.read().decode("utf-8", "replace")
if e.strip(): print("stderr:", e)
c.exec_command("rm -f /tmp/_check_env.sh")
c.close()
