"""Insert the lite_profiles row for the 'master' user directly via psycopg.

The fire-and-forget mirror thread in lite_user_mirror.py creates the
Supabase Auth user fine, but the lite_profiles upsert path goes through
the app_store cloud engine which needs a live backend process to source
its DB config. This script connects to the cloud DB directly using the
pooler credentials in .env.
"""
from __future__ import annotations
import sys, io
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

USER_ID  = "cb42be29-cd8a-4c96-b725-83100db83bd7"
TENANT   = "default"
USERNAME = "master"
EMAIL    = "master@trustnode.local"
ROLE     = "admin"

conn = psycopg.connect(
    host=env["TRUSTNODE_CLOUD_DB_HOST"],
    port=int(env.get("TRUSTNODE_CLOUD_DB_PORT") or "5432"),
    dbname=env.get("TRUSTNODE_CLOUD_DB_NAME") or "postgres",
    user=env["TRUSTNODE_CLOUD_DB_USER"],
    password=env["TRUSTNODE_CLOUD_DB_PASSWORD"],
    sslmode=env.get("TRUSTNODE_CLOUD_DB_SSLMODE") or "require",
    connect_timeout=15,
)
try:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.lite_profiles
              (user_id, tenant_id, username, email, role, created_utc, updated_utc)
            VALUES (%s, %s, %s, %s, %s, now(), now())
            ON CONFLICT (user_id) DO UPDATE SET
              tenant_id = EXCLUDED.tenant_id,
              username  = EXCLUDED.username,
              email     = EXCLUDED.email,
              role      = EXCLUDED.role,
              updated_utc = now()
            RETURNING user_id, tenant_id, username, role, updated_utc
            """,
            (USER_ID, TENANT, USERNAME, EMAIL, ROLE),
        )
        row = cur.fetchone()
        conn.commit()
        print("UPSERT OK:", row)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT user_id, tenant_id, username, role, email, updated_utc "
            "FROM public.lite_profiles WHERE user_id=%s",
            (USER_ID,),
        )
        print("VERIFY:", cur.fetchone())
finally:
    conn.close()
