"""Apply the alarms + reports + report-queue migration to Supabase.

Creates these public tables (idempotent, safe to re-run):
  - alarms_setup        (configured alarms — mirror)
  - triggers_limits     (configured alarm rules — mirror; read by Lite Alarms)
  - generated_reports   (mirror of edge SQLite rows; has storage_path)
  - lite_report_requests (queue Lite writes to; edge poller drains)
  - lite-reports Storage bucket + deny-anon policy

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
    migration = here / "db" / "migrations" / "20260518_alarms_reports_queue.sql"
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
             WHERE relname IN (
                'alarms_setup','triggers_limits','generated_reports','lite_report_requests'
             )
             ORDER BY relname
        """)
        print("\nRLS state after migration:")
        for r in cur.fetchall():
            print(f"  {r[0]:24s} rowsecurity={r[1]}")
        cur.execute("""
            SELECT tablename, policyname
              FROM pg_policies
             WHERE tablename IN (
               'alarms_setup','triggers_limits','generated_reports','lite_report_requests'
             )
             ORDER BY tablename, policyname
        """)
        print("\nPolicies present:")
        for r in cur.fetchall():
            print(f"  {r[0]:24s} {r[1]}")
        cur.execute("SELECT id FROM storage.buckets WHERE id = 'lite-reports'")
        bucket = cur.fetchone()
        print(f"\nStorage bucket lite-reports: {'present' if bucket else 'MISSING'}")
    print("\nMigration applied OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
