"""Check what's in the VPS-side control_plane_store SQLite."""
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


def _print_safe(s: str) -> None:
    try: print(s)
    except UnicodeEncodeError: print(s.encode("ascii", errors="replace").decode("ascii"))


client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(env["VPS_HOST"], port=int(env.get("VPS_PORT") or "22"),
               username=env["VPS_USER"], password=env["VPS_PASSWORD"], timeout=15)

PY = r"""
import sys
sys.path.insert(0, "/opt/trustnode-edge/app/Trustnode_edge_app/backend")
from app.state import control_plane_store
import sqlite3, os

print("control_plane_store db_path:", control_plane_store._db_path)
print("file exists:", os.path.exists(control_plane_store._db_path))
print()
conn = sqlite3.connect(control_plane_store._db_path)
conn.row_factory = sqlite3.Row
print("=== cp_tenants ===")
for r in conn.execute("SELECT tenant_id, name, status FROM cp_tenants").fetchall():
    print(f"  tenant_id={r['tenant_id']:30s} name={r['name']!r:30s} status={r['status']}")
print()
print("=== cp_customers ===")
for r in conn.execute("SELECT customer_id, tenant_id, company_name FROM cp_customers").fetchall():
    print(f"  customer_id={r['customer_id']:30s} tenant_id={r['tenant_id']:30s} name={r['company_name']!r}")
print()
print("=== cp_edges ===")
for r in conn.execute("SELECT edge_id, customer_id, tenant_id, status FROM cp_edges").fetchall():
    print(f"  edge_id={r['edge_id']:30s} customer_id={r['customer_id'] or '<NULL>':20s} tenant_id={r['tenant_id']:25s} status={r['status']}")
"""

sftp = client.open_sftp()
with sftp.open("/tmp/_tn_check.py", "w") as f:
    f.write(PY)
sftp.close()
stdin, stdout, stderr = client.exec_command(
    "/opt/trustnode-edge/app/Trustnode_edge_app/backend/.venv/bin/python /tmp/_tn_check.py 2>&1", timeout=30)
out = stdout.read().decode(errors="replace")
err = stderr.read().decode(errors="replace")
_print_safe(out)
if err: _print_safe(f"[stderr] {err}")
client.close()
