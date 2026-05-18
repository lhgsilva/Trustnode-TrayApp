"""Smoke-test the per-customer tenant logic ON the VPS by invoking
control_plane_store directly. No HTTP, no auth flow. Read-only by
default; --apply creates a smoke-test customer and then deletes it.
"""
import sys
from pathlib import Path
import argparse
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
    try:
        print(s)
    except UnicodeEncodeError:
        print(s.encode("ascii", errors="replace").decode("ascii"))


# Python script we'll send to the VPS to run inside the backend venv.
# It exercises:
#   1. control_plane_store.get_customer_tenant_id("cust-a") => existing slug "customer_a"
#   2. control_plane_store.get_customer_tenant_id("smoke-test-x") => "" (does not exist)
#   3. (--apply only) upsert a brand-new customer via the same path the
#      POST /customers endpoint takes (_customer_tenant_id resolver +
#      control_plane_store.upsert_customer) and verify the assigned
#      tenant is 'tenant-smoke-test-x'.
#   4. (--apply only) delete the smoke-test customer afterwards.
PY_SNIPPET = r"""
import os, sys, json
sys.path.insert(0, "/opt/trustnode-edge/app/Trustnode_edge_app/backend")
from app.services import control_plane_store as cps_mod
from app.state import control_plane_store  # the singleton

print("=== existing customer tenant lookup ===")
for cid in ("cust-a", "cust-b", "cust-c", "does-not-exist", "smoke-test-x"):
    t = control_plane_store.get_customer_tenant_id(customer_id=cid)
    print(f"  customer_id={cid:20s} -> tenant_id={'<empty>' if not t else t}")

# Simulate the POST /customers logic. Doing it here without going
# through HTTP means we don't need a JWT.
def _customer_tenant_id(customer_id):
    cid = str(customer_id or "").strip()
    if not cid:
        raise ValueError("missing customer_id")
    existing = control_plane_store.get_customer_tenant_id(customer_id=cid)
    if existing:
        return existing
    return f"tenant-{cid}"

print()
print("=== _customer_tenant_id() resolution ===")
for cid in ("cust-a", "smoke-test-x"):
    t = _customer_tenant_id(cid)
    print(f"  customer_id={cid:20s} -> resolved tenant_id={t}")
"""

PY_APPLY_SNIPPET = r"""
print()
print("=== APPLY: create smoke-test-x customer ===")
SMOKE = "smoke-test-x"
new_tenant = _customer_tenant_id(SMOKE)
print(f"  resolved tenant for new customer: {new_tenant}")

# Ensure the per-customer tenant exists in cp_tenants (POST /customers
# does this).
control_plane_store.upsert_tenant(
    tenant_id=new_tenant,
    name="Smoke Test X",
    status="active",
    primary_domain="",
    timezone_name="UTC",
    metadata={"source": "smoke-test"},
)
print(f"  cp_tenants row upserted for {new_tenant}")

row = control_plane_store.upsert_customer(
    tenant_id=new_tenant,
    customer_id=SMOKE,
    company_name="Smoke Test X",
    contact_email="",
    status="active",
    metadata={"source": "smoke-test"},
)
print(f"  cp_customers row: tenant_id={row.get('tenant_id')} customer_id={row.get('customer_id')}")
assert row.get("tenant_id") == new_tenant, f"expected tenant {new_tenant}, got {row.get('tenant_id')}"

# Read back via the lookup path that production code uses
re_lookup = control_plane_store.get_customer_tenant_id(customer_id=SMOKE)
print(f"  re-lookup of {SMOKE} -> {re_lookup}")
assert re_lookup == new_tenant

# Same call again should now hit the existing-tenant branch (idempotency)
again = _customer_tenant_id(SMOKE)
print(f"  re-resolve of {SMOKE} -> {again} (should equal {new_tenant})")
assert again == new_tenant

print()
print("=== APPLY: delete smoke-test-x customer ===")
control_plane_store.delete_customer(tenant_id=new_tenant, customer_id=SMOKE)
# verify deletion
gone = control_plane_store.get_customer_tenant_id(customer_id=SMOKE)
print(f"  post-delete lookup of {SMOKE} -> {gone or '<empty (deleted)>'}")
assert gone == ""

# Cleanup the smoke tenant row too so we don't leave litter behind.
# control_plane_store doesn't expose delete_tenant, but we can drop the
# row directly. (Safe: this tenant has no other resources since we just
# created and deleted the only customer that used it.)
import sqlite3
conn = sqlite3.connect(control_plane_store._db_path)
try:
    conn.execute("DELETE FROM cp_tenants WHERE tenant_id=?", (new_tenant,))
    conn.commit()
finally:
    conn.close()
print(f"  cp_tenants row {new_tenant} removed")
print()
print("=== SMOKE TEST PASSED ===")
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Actually create+delete the smoke-test customer (touches cp_tenants/cp_customers).")
    args = parser.parse_args()

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(env["VPS_HOST"], port=int(env.get("VPS_PORT") or "22"),
                   username=env["VPS_USER"], password=env["VPS_PASSWORD"], timeout=15)

    snippet = PY_SNIPPET
    if args.apply:
        snippet += PY_APPLY_SNIPPET

    # Push the snippet to /tmp on VPS and run it inside the backend venv.
    sftp = client.open_sftp()
    remote_path = "/tmp/_tn_smoke.py"
    with sftp.open(remote_path, "w") as f:
        f.write(snippet)
    sftp.close()

    cmd = "/opt/trustnode-edge/app/Trustnode_edge_app/backend/.venv/bin/python /tmp/_tn_smoke.py 2>&1"
    stdin, stdout, stderr = client.exec_command(cmd, timeout=60)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    _print_safe(out)
    if err:
        _print_safe(f"[stderr] {err}")
    # cleanup the snippet
    client.exec_command(f"rm -f {remote_path}")
    client.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
