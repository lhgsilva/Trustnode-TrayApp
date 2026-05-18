"""Backfill VPS local SQLite: re-tag every customer that's on tenant_id='default'
(or any other shared tenant) to 'tenant-<customer_id>', and cascade the new
tenant to every child resource (cp_edges, cp_licenses,
cp_edge_activation_codes, cp_users).

Reads /opt/trustnode-edge/data/trustnode_app_store.db on the VPS.

Idempotent. --dry-run (default) shows what would change without writing;
--commit applies.

The cloud Supabase backfill is a separate concern — we already did that
via 20260518_per_customer_tenant.sql when the customers there were
already on their correct tenants. The VPS local SQLite has many more
rows on 'default' (smoke tests + real customers created via portal
before per-customer enforcement landed).
"""
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
parser.add_argument("--commit", action="store_true")
parser.add_argument("--include-smoke", action="store_true",
                    help="Also re-tag smoke-* customers. Default skips them so we don't clutter the production cp_tenants.")
args = parser.parse_args()


client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(env["VPS_HOST"], port=int(env.get("VPS_PORT") or "22"),
               username=env["VPS_USER"], password=env["VPS_PASSWORD"], timeout=15)


PY = f"""
import sys, sqlite3, json

DRY_RUN = {repr(not args.commit)}
INCLUDE_SMOKE = {repr(bool(args.include_smoke))}

DB = '/opt/trustnode-edge/data/trustnode_app_store.db'
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
conn.execute("BEGIN")

# 1) Find every customer on tenant_id='default' (or any tenant that's
#    NOT 'tenant-<customer_id>' / their own per-customer tenant).
rows = conn.execute(
  "SELECT customer_id, tenant_id, company_name FROM cp_customers WHERE tenant_id = 'default'"
).fetchall()
targets = []
for r in rows:
    cid = r['customer_id']
    if not cid: continue
    if not INCLUDE_SMOKE and cid.startswith('smoke-'):
        continue
    targets.append(cid)

print(f"customers on default: {{len(rows)}}")
print(f"after smoke filter: {{len(targets)}} will be re-tagged")
print()
for cid in targets[:30]:
    print(f"  - {{cid}}")
if len(targets) > 30:
    print(f"  ... and {{len(targets)-30}} more")

if not targets:
    conn.rollback(); conn.close()
    print("nothing to do.")
    sys.exit(0)

print()
print("=== applying changes (in TX) ===")
moved_customers = 0
moved_edges = 0
moved_licenses = 0
moved_codes = 0
moved_users = 0
created_tenants = 0

for cid in targets:
    new_tenant = f"tenant-{{cid}}"
    # Ensure cp_tenants row exists
    existing = conn.execute("SELECT tenant_id FROM cp_tenants WHERE tenant_id=?", (new_tenant,)).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO cp_tenants(tenant_id, name, status, primary_domain, timezone, metadata_json, created_utc, updated_utc) "
            "VALUES(?, ?, 'active', '', 'UTC', ?, datetime('now'), datetime('now'))",
            (new_tenant, cid, json.dumps({{"source": "vps_backfill_2026-05-18", "customer_id": cid}})),
        )
        created_tenants += 1

    # Update child resources first (no FK on these columns to cp_customers.tenant_id)
    cur = conn.execute("UPDATE cp_edges SET tenant_id=?, updated_utc=datetime('now') WHERE customer_id=? AND tenant_id='default'", (new_tenant, cid))
    moved_edges += cur.rowcount
    cur = conn.execute("UPDATE cp_licenses SET tenant_id=?, updated_utc=datetime('now') WHERE customer_id=? AND tenant_id='default'", (new_tenant, cid))
    moved_licenses += cur.rowcount
    cur = conn.execute("UPDATE cp_edge_activation_codes SET tenant_id=? WHERE customer_id=? AND tenant_id='default'", (new_tenant, cid))
    moved_codes += cur.rowcount
    # cp_users have no customer_id column historically, skip silently
    # (portal-level user-to-tenant binding is via cp_user_tenant_memberships)
    try:
        cur = conn.execute("UPDATE cp_users SET tenant_id=?, updated_utc=datetime('now') WHERE tenant_id='default' AND username IN (SELECT username FROM cp_user_tenant_memberships WHERE tenant_id=?)", (new_tenant, new_tenant))
        moved_users += cur.rowcount
    except Exception:
        pass

    # Finally the customer row itself (must be last to keep FK happy)
    cur = conn.execute("UPDATE cp_customers SET tenant_id=?, updated_utc=datetime('now') WHERE customer_id=? AND tenant_id='default'", (new_tenant, cid))
    moved_customers += cur.rowcount

print(f"  cp_tenants created:                 {{created_tenants}}")
print(f"  cp_customers moved off default:     {{moved_customers}}")
print(f"  cp_edges moved off default:         {{moved_edges}}")
print(f"  cp_licenses moved off default:      {{moved_licenses}}")
print(f"  cp_edge_activation_codes moved:     {{moved_codes}}")
print(f"  cp_users moved (via memberships):   {{moved_users}}")

# Sanity check
print()
print("=== post-tx state (still inside TX) ===")
left = conn.execute("SELECT count(*) FROM cp_customers WHERE tenant_id='default'").fetchone()[0]
print(f"  cp_customers still on default: {{left}}")
left = conn.execute("SELECT count(*) FROM cp_edges WHERE tenant_id='default' AND customer_id IS NOT NULL").fetchone()[0]
print(f"  cp_edges with customer_id still on default: {{left}}")

if DRY_RUN:
    conn.rollback()
    print()
    print("DRY-RUN: rolled back. Re-run with --commit to apply.")
else:
    conn.commit()
    print()
    print("COMMITTED.")
conn.close()
"""

sftp = client.open_sftp()
with sftp.open("/tmp/_tn_backfill.py", "w") as f:
    f.write(PY)
sftp.close()
stdin, stdout, stderr = client.exec_command("/opt/trustnode-edge/app/Trustnode_edge_app/backend/.venv/bin/python /tmp/_tn_backfill.py 2>&1", timeout=60)
out = stdout.read().decode(errors="replace")
err = stderr.read().decode(errors="replace")
_ps(out)
if err: _ps("[stderr] " + err)
client.exec_command("rm -f /tmp/_tn_backfill.py")
client.close()
