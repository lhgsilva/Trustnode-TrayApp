"""Apply the gateway_configurations + devices mirror migration to Supabase.

Creates these public tables (idempotent, safe to re-run):
  - gateway_configurations
  - devices

Both get RLS (lite_select policy: row tenant_id must match the caller's
tenant, or caller is global admin) and are added to supabase_realtime
publication so Lite charts can refresh when the operator renames a
gateway at the edge.

Reads connection details from Trustnode_edge_app/.env.
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
    migration = here / "db" / "migrations" / "20260610_gateway_devices_mirror.sql"
    if not migration.is_file():
        print(f"missing migration: {migration}", file=sys.stderr)
        return 2

    sql = migration.read_text(encoding="utf-8")
    print(f"== Applying {migration.name} ({len(sql):,} bytes) ==")

    conn_kwargs = dict(
        host=env["TRUSTNODE_CLOUD_DB_HOST"],
        port=int(env.get("TRUSTNODE_CLOUD_DB_PORT") or "5432"),
        dbname=env["TRUSTNODE_CLOUD_DB_NAME"],
        user=env["TRUSTNODE_CLOUD_DB_USER"],
        password=env["TRUSTNODE_CLOUD_DB_PASSWORD"],
        sslmode="require",
        connect_timeout=15,
    )

    with psycopg.connect(**conn_kwargs) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()

    # Verification.
    with psycopg.connect(**conn_kwargs) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT relname, relrowsecurity
              FROM pg_class
             WHERE relname IN ('gateway_configurations','devices')
             ORDER BY relname
        """)
        print("\nRLS state after migration:")
        for r in cur.fetchall():
            print(f"  {r[0]:24s} rowsecurity={r[1]}")
        cur.execute("""
            SELECT tablename, policyname
              FROM pg_policies
             WHERE tablename IN ('gateway_configurations','devices')
             ORDER BY tablename, policyname
        """)
        print("\nPolicies present:")
        for r in cur.fetchall():
            print(f"  {r[0]:24s} {r[1]}")
        cur.execute("""
            SELECT schemaname, tablename
              FROM pg_publication_tables
             WHERE pubname='supabase_realtime'
               AND tablename IN ('gateway_configurations','devices')
             ORDER BY tablename
        """)
        print("\nRealtime publication membership:")
        for r in cur.fetchall():
            print(f"  {r[0]}.{r[1]}")
    print("\nMigration applied OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
