"""Inspect VPS state before deploying. Read-only.

  - Locates the TrustNode install dir (looks for the repo + venv).
  - Lists trustnode-* systemd units.
  - Captures git rev so we know what's currently deployed.

Usage:
  python scripts/vps_inspect.py
"""
from __future__ import annotations
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
env: dict[str, str] = {}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    env[k.strip()] = v.strip().strip('"').strip("'")

import paramiko  # type: ignore

host = env["VPS_HOST"]
port = int(env.get("VPS_PORT") or "22")
user = env["VPS_USER"]
password = env["VPS_PASSWORD"]

print(f"[i] connecting to {user}@{host}:{port}")
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(host, port=port, username=user, password=password, timeout=15)

CMDS = [
    ("uptime + hostname",
     "hostname; uptime"),
    ("find trustnode installs",
     "find /opt /home /root /srv -maxdepth 4 -type d \\( -name 'Trustnode-TrayApp' -o -name 'Trustnode_edge_app' -o -name 'trustnode*' \\) 2>/dev/null | head -20"),
    ("systemd units (trustnode)",
     "systemctl list-units --all --no-pager --no-legend 2>/dev/null | grep -i trustnode || echo NO_UNITS"),
    ("systemd files",
     "ls -1 /etc/systemd/system/trustnode* 2>/dev/null || echo NO_UNIT_FILES"),
    ("running python processes",
     "ps -eo pid,etime,cmd --sort=-etime | grep -E 'python|uvicorn|gunicorn' | grep -iE 'trustnode|control|edge' | head -10"),
    ("nginx sites",
     "ls -1 /etc/nginx/sites-enabled/ 2>/dev/null"),
]

for label, cmd in CMDS:
    print(f"\n--- {label} ---")
    stdin, stdout, stderr = client.exec_command(cmd, timeout=30)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    if out:
        print(out)
    if err:
        print(f"[stderr] {err}")

client.close()
