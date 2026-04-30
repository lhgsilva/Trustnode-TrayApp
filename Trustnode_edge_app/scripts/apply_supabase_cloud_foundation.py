import argparse
from pathlib import Path

import psycopg


def apply_migration(conn: psycopg.Connection, path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    if sql.startswith("\ufeff"):
        sql = sql.lstrip("\ufeff")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply Trustnode Supabase foundation migrations.")
    parser.add_argument("--database-url", required=True, help="PostgreSQL connection URL")
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Trustnode_edge_app root path",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    migrations = [
        root / "backend" / "sql" / "migrations" / "2026-04-11_telemetry_v1_core.sql",
        root / "backend" / "sql" / "migrations" / "2026-04-22_control_plane_core.sql",
        root / "backend" / "sql" / "migrations" / "2026-04-30_supabase_control_plane_hardening.sql",
    ]

    for m in migrations:
        if not m.exists():
            raise FileNotFoundError(f"Migration file not found: {m}")

    with psycopg.connect(args.database_url, autocommit=False) as conn:
        for m in migrations:
            print(f"Applying: {m}")
            apply_migration(conn, m)

    print("Supabase cloud foundation is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
