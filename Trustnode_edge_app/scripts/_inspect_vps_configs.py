"""Survey the VPS edge SQLite + cloud DB to find gateway_id -> plc_endpoint mappings."""
from __future__ import annotations
import io, sys, json
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

REMOTE = r'''
import sqlite3, json
db = "/opt/trustnode-edge/data/trustnode_app_store.db"
con = sqlite3.connect(db); con.row_factory = sqlite3.Row
print("--- config_documents domains ---")
for r in con.execute("SELECT DISTINCT domain FROM config_documents ORDER BY domain"):
    print(" ", r[0])
print()
print("--- gateway_configurations (default unscoped) ---")
r = con.execute("SELECT payload_json FROM config_documents WHERE domain='gateway_configurations' LIMIT 1").fetchone()
if r:
    p = json.loads(r[0]) if isinstance(r[0], str) else r[0]
    if isinstance(p, list):
        for g in p:
            gid = g.get("id"); typ = g.get("gateway_type")
            print("  id=", gid, "type=", typ, "plc_ip=", g.get("plc_ip"), "opc_url=", g.get("opc_url"))
print()
print("--- scoped gateway_configurations rows ---")
try:
    for r in con.execute("SELECT scope_key, length(payload_json) FROM config_documents_scoped WHERE domain='gateway_configurations'"):
        print(" ", r[0], "bytes:", r[1])
except sqlite3.OperationalError as e:
    print("  (no config_documents_scoped table:", e, ")")
print()
print("--- scoped dashboard_configurations rows ---")
try:
    n = 0
    for r in con.execute("SELECT scope_key, length(payload_json) FROM config_documents_scoped WHERE domain='dashboard_configurations'"):
        print(" ", r[0], "bytes:", r[1])
        n += 1
    if n == 0: print("  (none)")
except sqlite3.OperationalError as e:
    print("  err:", e)
'''

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(hostname=env["VPS_HOST"], port=int(env.get("VPS_PORT") or "22"),
          username=env["VPS_USER"], password=env["VPS_PASSWORD"],
          timeout=15, allow_agent=False, look_for_keys=False)
sftp = c.open_sftp()
with sftp.file("/tmp/_inspect.py", "w") as f: f.write(REMOTE)
sftp.close()
_, out, err = c.exec_command("python3 /tmp/_inspect.py", timeout=60)
print(out.read().decode("utf-8", "replace"))
e = err.read().decode("utf-8", "replace")
if e.strip(): print("stderr:", e)
c.exec_command("rm -f /tmp/_inspect.py")
c.close()
