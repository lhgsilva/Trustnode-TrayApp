"""Tiny one-off: dump the nginx vhost so we know how /lite/ gets served."""
from __future__ import annotations
import io, sys
from pathlib import Path
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import paramiko

env = {}
for line in (Path(__file__).resolve().parent.parent / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line: continue
    k, v = line.split("=", 1); env[k.strip()] = v.strip()
pwd = env["VPS_PASSWORD"]

CMDS = [
    ("Full nginx vhost for trustnode.lsapps.app",
     "cat /etc/nginx/conf.d/trustnode-edge.conf 2>&1"),
    ("Listing of VPS web root", "ls -la ${VPS_WEB_ROOT:-/var/www/trustnode}/ 2>&1 | head -30"),
    ("VPS_WEB_ROOT guess (where nginx serves from)",
     "grep -E '^\\s*root ' /etc/nginx/conf.d/trustnode-edge.conf 2>&1"),
]
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(hostname=env["VPS_HOST"], port=int(env.get("VPS_PORT") or "22"),
          username=env["VPS_USER"], password=pwd, timeout=15,
          banner_timeout=15, auth_timeout=15, allow_agent=False, look_for_keys=False)
try:
    for label, cmd in CMDS:
        print(f"\n========== {label} ==========")
        print(f"$ {cmd}")
        _, sout, serr = c.exec_command(cmd, timeout=15)
        print(sout.read().decode("utf-8", "replace").replace(pwd, "<REDACTED>").rstrip())
        e = serr.read().decode("utf-8", "replace").replace(pwd, "<REDACTED>").rstrip()
        if e: print("--- stderr ---"); print(e)
finally:
    c.close()
