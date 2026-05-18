#!/usr/bin/env bash
# One-shot: install Supabase + cloud-DB env vars into the VPS
# trustnode-backend systemd unit so the Lite user mirror works.
#
# Reads creds from Trustnode_edge_app/.env (gitignored) and writes a
# systemd drop-in override to /etc/systemd/system/trustnode-backend.
# service.d/override.conf.
#
# Run from the dev workstation (NOT on the VPS). Uses paramiko via the
# companion Python script.

set -euo pipefail
cd "$(dirname "$0")/.."
python3 - <<'PYEOF'
import sys, os
sys.path.insert(0, 'scripts')
from push_lite_to_vps import load_env
from pathlib import Path
import paramiko

env = load_env(Path('.env'))
url = env.get('TRUSTNODE_SUPABASE_URL', '')
key = env.get('TRUSTNODE_SUPABASE_SERVICE_KEY', '')
if not (url and key):
    print('ERROR: TRUSTNODE_SUPABASE_URL / TRUSTNODE_SUPABASE_SERVICE_KEY missing from .env', file=sys.stderr)
    sys.exit(2)

c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(hostname=env['VPS_HOST'], port=int(env.get('VPS_PORT') or '22'),
          username=env['VPS_USER'], password=env['VPS_PASSWORD'],
          timeout=15, allow_agent=False, look_for_keys=False)

override = f"""[Service]
TimeoutStopSec=10
KillMode=mixed
Environment=TRUSTNODE_PREFER_CLOUD_READS=true
Environment=TRUSTNODE_DISABLE_CONFIG_PUSH=true
Environment=TRUSTNODE_DISABLE_TELEMETRY_V1=1
Environment=TRUSTNODE_CONFIG_SYNC_SECONDS=0.5
Environment=TRUSTNODE_DATA_BULK_SYNC_SECONDS=0.25
Environment=TRUSTNODE_DATA_SYNC_BATCH_SIZE=1000
Environment=TRUSTNODE_CLOUD_LIVE_SSE_MS=250
Environment=TRUSTNODE_SUPABASE_URL={url}
Environment=TRUSTNODE_SUPABASE_SERVICE_KEY={key}
Environment=TRUSTNODE_SUPABASE_USER_DOMAIN={env.get('TRUSTNODE_SUPABASE_USER_DOMAIN','trustnode.local')}
Environment=TRUSTNODE_CLOUD_DB_HOST={env.get('TRUSTNODE_CLOUD_DB_HOST','')}
Environment=TRUSTNODE_CLOUD_DB_PORT={env.get('TRUSTNODE_CLOUD_DB_PORT','5432')}
Environment=TRUSTNODE_CLOUD_DB_NAME={env.get('TRUSTNODE_CLOUD_DB_NAME','postgres')}
Environment=TRUSTNODE_CLOUD_DB_USER={env.get('TRUSTNODE_CLOUD_DB_USER','')}
Environment=TRUSTNODE_CLOUD_DB_PASSWORD={env.get('TRUSTNODE_CLOUD_DB_PASSWORD','')}
Environment=TRUSTNODE_CLOUD_DB_SCHEMA={env.get('TRUSTNODE_CLOUD_DB_SCHEMA','public')}
ExecStart=
ExecStart=/opt/trustnode-edge/app/Trustnode_edge_app/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --timeout-graceful-shutdown 5
"""

sftp = c.open_sftp()
with sftp.file('/etc/systemd/system/trustnode-backend.service.d/override.conf', 'wb') as fh:
    fh.write(override.encode('utf-8'))
sftp.chmod('/etc/systemd/system/trustnode-backend.service.d/override.conf', 0o644)
sftp.close()
print('override.conf written')

_, out, err = c.exec_command(
    'systemctl daemon-reload && systemctl restart trustnode-backend && '
    'sleep 3 && systemctl is-active trustnode-backend',
    timeout=30,
)
print('restart:', out.read().decode('utf-8','replace').strip())
c.close()
PYEOF
