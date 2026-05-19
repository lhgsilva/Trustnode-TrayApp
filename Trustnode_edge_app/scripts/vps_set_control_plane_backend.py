"""Set TRUSTNODE_CONTROL_PLANE_BACKEND in the VPS systemd drop-in,
pull latest code, and restart the backend service.

Reads VPS creds from .env. Read-only by default; --apply executes."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
import paramiko  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
env: dict[str, str] = {}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line: continue
    k, _, v = line.partition("=")
    env[k.strip()] = v.strip().strip('"').strip("'")


def _ps(s):
    try: print(s)
    except UnicodeEncodeError: print(s.encode("ascii", errors="replace").decode("ascii"))


parser = argparse.ArgumentParser()
parser.add_argument("--mode", choices=["local", "cloud"], default="cloud",
                    help="Value to set TRUSTNODE_CONTROL_PLANE_BACKEND to. Default: cloud.")
parser.add_argument("--apply", action="store_true",
                    help="Actually edit + restart. Without --apply: preview only.")
args = parser.parse_args()

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(env["VPS_HOST"], port=int(env.get("VPS_PORT") or "22"),
               username=env["VPS_USER"], password=env["VPS_PASSWORD"], timeout=15)


def run(cmd: str, *, label: str | None = None, allow_fail: bool = False):
    if label:
        _ps(f"\n--- {label} ---")
    stdin, stdout, stderr = client.exec_command(cmd, timeout=120)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace").rstrip()
    err = stderr.read().decode(errors="replace").rstrip()
    if out: _ps(out)
    if err: _ps(f"[stderr] {err}")
    if rc != 0 and not allow_fail:
        _ps(f"[FATAL] command failed (rc={rc}): {cmd}")
        sys.exit(1)
    return rc, out, err


# 1. Preview current state
run("grep -nE 'TRUSTNODE_CONTROL_PLANE_BACKEND' /etc/systemd/system/trustnode-backend.service.d/*.conf 2>&1 || echo NOT_SET",
    label="current TRUSTNODE_CONTROL_PLANE_BACKEND in drop-in")

if not args.apply:
    _ps("\n[i] preview only. Re-run with --apply to flip the flag, pull, and restart.")
    client.close()
    sys.exit(0)

# 2. Set or update the env in 10-secrets.conf. Idempotent: add a [Service]
#    Environment= line if missing, else replace existing value.
new_env_line = f'Environment="TRUSTNODE_CONTROL_PLANE_BACKEND={args.mode}"'
# Use python on the VPS to do the edit safely (no shell quoting hell).
PY_EDIT = f"""
import os, re, sys
target = "/etc/systemd/system/trustnode-backend.service.d/10-secrets.conf"
new_line = '''{new_env_line}'''
with open(target) as f:
    txt = f.read()
# Match any existing TRUSTNODE_CONTROL_PLANE_BACKEND line and replace it,
# OR append a fresh Environment= line just after [Service].
pat = re.compile(r'^Environment="TRUSTNODE_CONTROL_PLANE_BACKEND=[^"]*"$', re.MULTILINE)
if pat.search(txt):
    new_txt = pat.sub(new_line, txt)
else:
    # Insert after first [Service] header
    if "[Service]" in txt:
        new_txt = txt.replace("[Service]", "[Service]\\n" + new_line, 1)
    else:
        new_txt = txt.rstrip() + "\\n\\n[Service]\\n" + new_line + "\\n"
if new_txt != txt:
    with open(target, "w") as f:
        f.write(new_txt)
    print("UPDATED")
else:
    print("ALREADY_SET")
"""
sftp = client.open_sftp()
with sftp.open("/tmp/_tn_setbackend.py", "w") as f:
    f.write(PY_EDIT)
sftp.close()
run("python3 /tmp/_tn_setbackend.py", label=f"set TRUSTNODE_CONTROL_PLANE_BACKEND={args.mode}")
run("rm -f /tmp/_tn_setbackend.py", label="cleanup", allow_fail=True)

# 3. Show resulting drop-in content (names only, no secrets dumped).
run("grep -nE 'TRUSTNODE_CONTROL_PLANE_BACKEND' /etc/systemd/system/trustnode-backend.service.d/*.conf",
    label="post-edit verification")

# 4. systemd daemon-reload + restart
run("systemctl daemon-reload", label="systemctl daemon-reload")
run("systemctl restart trustnode-backend", label="systemctl restart trustnode-backend")

# 5. Health probe
import time; time.sleep(2)
run("systemctl is-active trustnode-backend", label="service status")
run("curl -fsS -m 5 http://127.0.0.1:8000/api/health 2>&1 || echo HEALTH_FAILED",
    label="health probe", allow_fail=True)
run("journalctl -u trustnode-backend -n 25 --no-pager 2>&1 | tail -25",
    label="last 25 log lines (look for cloud-backend init)", allow_fail=True)

client.close()
_ps("\n[ok] done.")
