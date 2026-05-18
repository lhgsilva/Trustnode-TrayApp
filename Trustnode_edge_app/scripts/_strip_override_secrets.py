"""Rewrite override.conf on the VPS to the CI-style (no secrets).

After F1 the secret env vars live in /etc/systemd/system/
trustnode-backend.service.d/10-secrets.conf. override.conf should
only carry perf knobs + ExecStart. Run this once to bring the
current VPS into the final F1 state without waiting for the next
CI deploy.
"""
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

OVERRIDE_CONTENT = """[Service]
TimeoutStopSec=10
KillMode=mixed
Environment=TRUSTNODE_PREFER_CLOUD_READS=true
Environment=TRUSTNODE_DISABLE_CONFIG_PUSH=true
Environment=TRUSTNODE_DISABLE_TELEMETRY_V1=1
Environment=TRUSTNODE_CONFIG_SYNC_SECONDS=0.5
Environment=TRUSTNODE_DATA_BULK_SYNC_SECONDS=0.25
Environment=TRUSTNODE_DATA_SYNC_BATCH_SIZE=1000
Environment=TRUSTNODE_CLOUD_LIVE_SSE_MS=250
ExecStart=
ExecStart=/opt/trustnode-edge/app/Trustnode_edge_app/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --timeout-graceful-shutdown 5
"""

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(hostname=env["VPS_HOST"], port=int(env.get("VPS_PORT") or "22"),
          username=env["VPS_USER"], password=env["VPS_PASSWORD"],
          timeout=15, allow_agent=False, look_for_keys=False)
sftp = c.open_sftp()
path = "/etc/systemd/system/trustnode-backend.service.d/override.conf"
with sftp.file(path, "wb") as fh:
    fh.write(OVERRIDE_CONTENT.encode("utf-8"))
sftp.chmod(path, 0o644)
sftp.close()
print(f"wrote {path} ({len(OVERRIDE_CONTENT)} bytes, no secrets)")

_, out, err = c.exec_command(
    "systemctl daemon-reload && systemctl restart trustnode-backend && "
    "sleep 3 && systemctl is-active trustnode-backend",
    timeout=30,
)
print("restart:", out.read().decode("utf-8", "replace").strip())
e = err.read().decode("utf-8", "replace").strip()
if e: print("stderr:", e)
c.close()
