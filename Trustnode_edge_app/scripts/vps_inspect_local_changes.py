"""Read VPS local uncommitted changes BEFORE we touch anything. Read-only."""
from pathlib import Path
import paramiko  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
env: dict[str, str] = {}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    env[k.strip()] = v.strip().strip('"').strip("'")

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(env["VPS_HOST"], port=int(env.get("VPS_PORT") or "22"),
               username=env["VPS_USER"], password=env["VPS_PASSWORD"], timeout=15)

repo = "/opt/trustnode-edge/app"

print("=" * 70)
print("CURRENT HEAD on VPS:")
stdin,stdout,_ = client.exec_command(f"cd {repo} && git log --oneline -5")
print(stdout.read().decode())

print("=" * 70)
print("UNCOMMITTED DIFF: writing to /tmp/vps_local_diff.patch on VPS, then pulling down")
client.exec_command(f"cd {repo} && git diff > /tmp/vps_local_diff.patch")
# Re-establish exec_command to be safe
stdin,stdout,_ = client.exec_command(f"wc -l /tmp/vps_local_diff.patch")
print(stdout.read().decode())
# SFTP it down
import os
sftp = client.open_sftp()
local_path = str(ROOT / "scripts" / "vps_local_diff.patch")
sftp.get("/tmp/vps_local_diff.patch", local_path)
sftp.close()
print(f"saved to {local_path}")

print("=" * 70)
print("UNTRACKED FILES:")
stdin,stdout,_ = client.exec_command(f"cd {repo} && git ls-files --others --exclude-standard")
print(stdout.read().decode())

client.close()
