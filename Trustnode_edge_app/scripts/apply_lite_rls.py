"""Apply the TrustNode Lite RLS migration to Supabase.

Reads connection details from Trustnode_edge_app/.env (gitignored) and
executes the migration file via psycopg. Idempotent — safe to re-run.
"""
from __future__ import annotations
import io
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import psycopg


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main() -> int:
    here = Path(__file__).resolve().parent.parent  # Trustnode_edge_app/
    env = load_env(here / ".env")
    migration = here / "db" / "migrations" / "20260517_lite_rls.sql"
    if not migration.is_file():
        print(f"missing migration: {migration}", file=sys.stderr)
        return 2

    sql = migration.read_text(encoding="utf-8")
    print(f"== Applying {migration.name} ({len(sql):,} bytes) ==")

    with psycopg.connect(
        host=env["TRUSTNODE_CLOUD_DB_HOST"],
        port=int(env.get("TRUSTNODE_CLOUD_DB_PORT") or "5432"),
        dbname=env["TRUSTNODE_CLOUD_DB_NAME"],
        user=env["TRUSTNODE_CLOUD_DB_USER"],
        password=env["TRUSTNODE_CLOUD_DB_PASSWORD"],
        sslmode="require",
        connect_timeout=15,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()

    # Quick verification.
    with psycopg.connect(
        host=env["TRUSTNODE_CLOUD_DB_HOST"],
        port=int(env.get("TRUSTNODE_CLOUD_DB_PORT") or "5432"),
        dbname=env["TRUSTNODE_CLOUD_DB_NAME"],
        user=env["TRUSTNODE_CLOUD_DB_USER"],
        password=env["TRUSTNODE_CLOUD_DB_PASSWORD"],
        sslmode="require",
        connect_timeout=15,
    ) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT relname, relrowsecurity
              FROM pg_class
             WHERE relname IN (
                'historian_readings','live_latest','app_logs','plc_readings',
                'lite_profiles','cp_users','cp_user_tenant_memberships',
                'cp_edges','cp_customers'
             )
             ORDER BY relname
        """)
        print("\nRLS state after migration:")
        for r in cur.fetchall():
            print(f"  {r[0]:38s} rowsecurity={r[1]}")
        cur.execute("""
            SELECT tablename, policyname
              FROM pg_policies
             WHERE policyname LIKE '%lite%' OR tablename = 'lite_profiles'
             ORDER BY tablename, policyname
        """)
        print("\nLite policies present:")
        for r in cur.fetchall():
            print(f"  {r[0]:38s} {r[1]}")
    print("\nMigration applied OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
