"""One-shot: push every scoped dashboard from local SQLite to the cloud.

The mirror in app_store._mirror_config_doc_to_cloud is best-effort
fire-and-forget and (visibly) leaves the cloud trailing the local
edge by tens of versions. Until that's fixed properly, this script
forces parity so Lite users see the latest dashboard the edge user
saved.

Usage from Trustnode_edge_app/:
    python scripts/push_local_dashboards_to_cloud.py        # dry-run
    python scripts/push_local_dashboards_to_cloud.py --apply
"""
from __future__ import annotations
import argparse, io, json, sqlite3, sys
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import psycopg

HERE = Path(__file__).resolve().parent.parent


def load_env(p: Path) -> dict[str, str]:
    out = {}
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s: continue
            k, v = s.split("=", 1); out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    env = load_env(HERE / ".env")
    local_db = Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db"
    if not local_db.is_file():
        print(f"local SQLite not found: {local_db}")
        return 2

    lc = sqlite3.connect(f"file:{local_db}?mode=ro", uri=True, timeout=5)
    lc.row_factory = sqlite3.Row

    cc = psycopg.connect(
        host=env["TRUSTNODE_CLOUD_DB_HOST"],
        port=int(env.get("TRUSTNODE_CLOUD_DB_PORT") or "5432"),
        dbname=env.get("TRUSTNODE_CLOUD_DB_NAME") or "postgres",
        user=env["TRUSTNODE_CLOUD_DB_USER"],
        password=env["TRUSTNODE_CLOUD_DB_PASSWORD"],
        sslmode=env.get("TRUSTNODE_CLOUD_DB_SSLMODE") or "require",
        connect_timeout=15,
    )
    try:
        rows = lc.execute(
            "SELECT scope_key, payload_json, version, updated_utc "
            "FROM config_documents_scoped WHERE domain='dashboard_configurations'"
        ).fetchall()
        print(f"Local has {len(rows)} scoped dashboard rows")

        updates = []
        for r in rows:
            scope_key = r["scope_key"]
            payload = r["payload_json"]
            version = int(r["version"] or 0)
            updated = r["updated_utc"]
            tenant_id = (scope_key.split("|") or ["default"])[0] or "default"
            # Compare with cloud
            with cc.cursor() as cur:
                cur.execute(
                    "SELECT version FROM public.dashboard_configurations "
                    "WHERE tenant_id=%s AND scope_key=%s",
                    (tenant_id, scope_key),
                )
                row = cur.fetchone()
            cloud_v = int(row[0]) if row else 0
            if cloud_v >= version:
                print(f"  {scope_key}: cloud v={cloud_v} >= local v={version}, skip")
                continue
            print(f"  {scope_key}: cloud v={cloud_v} -> local v={version}  (push)")
            updates.append((tenant_id, scope_key, payload, version, updated))

        if not updates:
            print("Nothing to push.")
            return 0
        if not args.apply:
            print(f"\n[dry-run] would push {len(updates)} row(s). Pass --apply to write.")
            return 0

        with cc.cursor() as cur:
            for tenant_id, scope_key, payload, version, updated in updates:
                cur.execute(
                    "INSERT INTO public.dashboard_configurations "
                    "  (tenant_id, scope_key, payload_json, version, updated_utc) "
                    "VALUES (%s, %s, %s::jsonb, %s, %s::timestamptz) "
                    "ON CONFLICT (tenant_id, scope_key) DO UPDATE SET "
                    "  payload_json = EXCLUDED.payload_json, "
                    "  version      = EXCLUDED.version, "
                    "  updated_utc  = EXCLUDED.updated_utc",
                    (tenant_id, scope_key, payload, version, updated),
                )
        cc.commit()
        print(f"Pushed {len(updates)} row(s).")
    finally:
        lc.close()
        cc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
