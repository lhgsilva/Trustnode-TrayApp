"""Wipe all data from Supabase except cp_module_catalog and the
master/admin auth.users.

Schema (tables, columns, constraints, RLS policies, indexes) is left
intact. Only data is removed.

Dry-run (default) reports what would be deleted. --commit performs the wipe.

Order of operations matters because of foreign keys:
  1. Delete child rows first (cp_license_modules → cp_licenses,
     cp_edges → cp_customers, etc.)
  2. Truncate independent tables.
  3. Delete auth.users that aren't on the keep-list — this cascades
     to lite_profiles (already wiped) and any other auth-derived rows.

Idempotent: safe to re-run after a partial failure.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
import psycopg2

ROOT = Path(__file__).resolve().parents[1]
env: dict[str, str] = {}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line: continue
    k, _, v = line.partition("=")
    env[k.strip()] = v.strip().strip('"').strip("'")


KEEP_AUTH_EMAILS = {
    "master@trustnode.local",
    "admin@trustnode.local",
}

# Order: child tables first, then parents. cp_module_catalog is the only
# survivor among cp_* tables. tables not listed here are not wiped.
WIPE_PLAN: list[tuple[str, str]] = [
    # Format: (table, optional WHERE clause). Empty WHERE means truncate all rows.
    # ---- Lite & dashboard data ----
    ("lite_report_requests", ""),
    ("lite_profiles", ""),
    ("generated_reports", ""),
    ("report_templates", ""),
    ("dashboard_configurations", ""),
    ("alarms_setup", ""),
    ("triggers_limits", ""),
    # ---- Historian / live data ----
    ("plc_readings", ""),
    ("historian_readings", ""),
    ("live_latest", ""),
    ("app_logs", ""),
    ("ingest_audit_log", ""),
    ("telemetry_samples_raw", ""),
    ("latest_machine_state", ""),
    # ---- Config + sync ----
    ("config_audit", ""),
    ("config_documents", ""),
    ("collection_config_versions", ""),
    ("sync_outbox", ""),
    ("sync_targets", ""),
    # ---- Gateways ----
    ("gateway_registry", ""),
    ("gateway_credentials_metadata", ""),
    # ---- Control plane ----
    ("cp_security_audit_log", ""),
    ("cp_password_reset_events", ""),
    ("cp_user_tenant_memberships", ""),
    ("cp_edge_activation_codes", ""),
    ("cp_users", ""),
    ("cp_edge_licenses", ""),       # FK to cp_edges + cp_licenses
    ("cp_license_modules", ""),     # FK to cp_licenses
    ("cp_licenses", ""),
    ("cp_edges", ""),
    ("cp_customer_domains", ""),    # FK to cp_customers
    ("cp_customers", ""),
    ("cp_tenants", ""),              # tenants get rebuilt by portal create-customer flow
    # ---- Generic security audit ----
    ("security_audit_log", ""),
    ("tenant_users", ""),
    # ---- cp_module_catalog: PRESERVED (not in this list) ----
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true", help="Actually delete. Without this, dry-run only.")
    args = parser.parse_args()

    conn = psycopg2.connect(
        host=env["TRUSTNODE_CLOUD_DB_HOST"], port=int(env["TRUSTNODE_CLOUD_DB_PORT"]),
        dbname=env["TRUSTNODE_CLOUD_DB_NAME"], user=env["TRUSTNODE_CLOUD_DB_USER"],
        password=env["TRUSTNODE_CLOUD_DB_PASSWORD"], sslmode=env["TRUSTNODE_CLOUD_DB_SSLMODE"],
    )
    conn.autocommit = False
    cur = conn.cursor()

    # --- Audit BEFORE ---
    print("=== Wipe plan ===")
    total_before = 0
    for tbl, _where in WIPE_PLAN:
        try:
            cur.execute(f'SELECT count(*) FROM public."{tbl}"')
            n = cur.fetchone()[0]
            total_before += n
            print(f"  {tbl:42s} {n:>10}")
        except Exception as e:
            print(f"  {tbl:42s} <err {e}>")
    print(f"\nTotal rows in scope: {total_before:,}")

    # auth.users
    cur.execute("SELECT count(*) FROM auth.users")
    auth_total = cur.fetchone()[0]
    placeholders = ",".join(["%s"] * len(KEEP_AUTH_EMAILS))
    cur.execute(
        f"SELECT count(*) FROM auth.users WHERE email NOT IN ({placeholders})",
        tuple(KEEP_AUTH_EMAILS),
    )
    auth_to_delete = cur.fetchone()[0]
    print(f"\nauth.users: {auth_total} total, will delete {auth_to_delete}, keep {auth_total - auth_to_delete}")

    cur.execute(
        f"SELECT email FROM auth.users WHERE email IN ({placeholders})",
        tuple(KEEP_AUTH_EMAILS),
    )
    kept = [r[0] for r in cur.fetchall()]
    print(f"  Kept emails actually present in DB: {kept}")
    missing = KEEP_AUTH_EMAILS - set(kept)
    if missing:
        print(f"  WARNING: the following keep-list emails are NOT in auth.users: {missing}")

    # --- Execute (inside one transaction) ---
    try:
        for tbl, where in WIPE_PLAN:
            if where:
                sql = f'DELETE FROM public."{tbl}" WHERE {where}'
            else:
                # Use DELETE not TRUNCATE so FKs are honoured (TRUNCATE on
                # a referenced table fails without CASCADE; with CASCADE
                # we'd hit things we don't intend to touch).
                sql = f'DELETE FROM public."{tbl}"'
            try:
                cur.execute(sql)
                print(f"  -> {tbl}: deleted {cur.rowcount} rows")
            except Exception as e:
                print(f"  !! {tbl}: {e}")
                conn.rollback()
                return 1

        # Delete auth.users not in keep list. This cascades to anything
        # FK'd to auth.users (most of which we already wiped above).
        cur.execute(
            f"DELETE FROM auth.users WHERE email NOT IN ({placeholders})",
            tuple(KEEP_AUTH_EMAILS),
        )
        print(f"\n  -> auth.users: deleted {cur.rowcount} rows")

        if args.commit:
            conn.commit()
            print("\n[ok] COMMITTED")
        else:
            conn.rollback()
            print("\n[i] DRY-RUN: rolled back. Re-run with --commit to apply for real.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
