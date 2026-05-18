"""Apply db/migrations/20260518_lite_global_admin.sql to the cloud DB.

Idempotent. Uses the cloud DB credentials in .env."""
from __future__ import annotations
import io, sys
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import psycopg

HERE = Path(__file__).resolve().parent.parent
env = {}
for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s: continue
    k, v = s.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")

sql_path = HERE / "db" / "migrations" / "20260518_lite_global_admin.sql"
sql = sql_path.read_text(encoding="utf-8")
print(f"applying {sql_path.name} ({len(sql)} bytes)")

con = psycopg.connect(
    host=env["TRUSTNODE_CLOUD_DB_HOST"],
    port=int(env.get("TRUSTNODE_CLOUD_DB_PORT") or "5432"),
    dbname=env.get("TRUSTNODE_CLOUD_DB_NAME") or "postgres",
    user=env["TRUSTNODE_CLOUD_DB_USER"],
    password=env["TRUSTNODE_CLOUD_DB_PASSWORD"],
    sslmode=env.get("TRUSTNODE_CLOUD_DB_SSLMODE") or "require",
    connect_timeout=15,
    autocommit=False,
)
try:
    with con.cursor() as cur:
        cur.execute(sql)
    con.commit()
    print("OK — migration applied")
    # Quick verification
    with con.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_policies WHERE policyname LIKE '%_global_admin_select'")
        n = cur.fetchone()[0]
        print(f"global-admin policies in place: {n}")
finally:
    con.close()
