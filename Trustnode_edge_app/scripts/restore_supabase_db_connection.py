"""Restore the Supabase cloud DB entry in database_configurations.

The edge syncs the historian to Supabase via TRUSTNODE_CLOUD_DB_* env
vars regardless of what's saved in `database_configurations`. But the
desktop UI only shows DB connections that ARE in that domain — so when
the configuration list was reset (or when a fresh install reseeded the
defaults), the Supabase entry disappeared from the UI.

This script adds (or updates) a Supabase entry in every existing
per-edge `database_configurations` row AND in the global one, reading
the connection details from Trustnode_edge_app/.env. The cloud sync
isn't actually affected — but operators can now see and edit the
connection from the desktop.

Safe to re-run.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


SUPABASE_ID = "cloud-supabase-default"


def load_env(p: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def resolve_local_db() -> Path:
    base = os.environ.get("TRUSTNODE_DATA_DIR", "").strip()
    if base:
        return Path(base) / "trustnode_app_store.db"
    return Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db"


def build_supabase_entry(env: dict[str, str]) -> dict | None:
    host = env.get("TRUSTNODE_CLOUD_DB_HOST", "").strip()
    if not host:
        return None
    return {
        "id": SUPABASE_ID,
        "name": "Cloud Supabase",
        "engine": "postgresql",
        "host": host,
        "port": int(env.get("TRUSTNODE_CLOUD_DB_PORT") or "5432"),
        "database": env.get("TRUSTNODE_CLOUD_DB_NAME", "postgres"),
        "username": env.get("TRUSTNODE_CLOUD_DB_USER", ""),
        "password": env.get("TRUSTNODE_CLOUD_DB_PASSWORD", ""),
        "schema": env.get("TRUSTNODE_CLOUD_DB_SCHEMA", "public"),
        "table": env.get("TRUSTNODE_CLOUD_DB_TABLE", "plc_readings"),
        "tls": str(env.get("TRUSTNODE_CLOUD_DB_SSLMODE", "require")).lower() != "disable",
        "enabled": True,
        "location": "cloud",
        "use_gateway": True,
        "use_app": True,
        "use_backup": False,
        "cloud_sync_enabled": True,
    }


def merge_supabase(db_list: list[dict], supabase_entry: dict) -> tuple[list[dict], bool]:
    """Return (new_list, changed)."""
    if not isinstance(db_list, list):
        db_list = []
    out = []
    seen = False
    for d in db_list:
        if not isinstance(d, dict):
            out.append(d)
            continue
        if str(d.get("id") or "") == SUPABASE_ID:
            # Keep the existing entry but refresh credentials from .env.
            merged = dict(d)
            merged.update(supabase_entry)
            out.append(merged)
            seen = True
        else:
            out.append(d)
    if not seen:
        out.append(supabase_entry)
    return out, True


def main() -> int:
    here = Path(__file__).resolve().parent.parent
    env = load_env(here / ".env")
    supabase_entry = build_supabase_entry(env)
    if not supabase_entry:
        print("TRUSTNODE_CLOUD_DB_HOST not set in .env — nothing to do.", file=sys.stderr)
        return 2

    db_path = resolve_local_db()
    if not db_path.is_file():
        print(f"local DB not found at {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    now = "now-restore-supabase"

    print(f"-- inserting Supabase entry id={SUPABASE_ID!r} ({supabase_entry['host']}) --")

    # Per-edge scoped rows
    scoped = conn.execute(
        "SELECT scope_key, payload_json, version FROM config_documents_scoped WHERE domain='database_configurations'"
    ).fetchall()
    print(f"\n== scoped rows: {len(scoped)} ==")
    with conn:
        for r in scoped:
            scope_key = str(r["scope_key"] or "")
            try:
                cur_list = json.loads(str(r["payload_json"] or "[]"))
            except Exception:
                cur_list = []
            new_list, _ = merge_supabase(cur_list, supabase_entry)
            payload = json.dumps(new_list, separators=(",", ":"))
            new_version = int(r["version"] or 1) + 1
            conn.execute(
                "UPDATE config_documents_scoped SET payload_json=?, version=?, "
                "updated_utc=strftime('%Y-%m-%d %H:%M:%f','now') "
                "WHERE scope_key=? AND domain='database_configurations'",
                (payload, new_version, scope_key),
            )
            print(f"   + {scope_key!r:55s} -> {len(new_list)} DB row(s)")

    # Global row
    print("\n== global row ==")
    g = conn.execute(
        "SELECT payload_json, version FROM config_documents WHERE domain='database_configurations'"
    ).fetchone()
    if g:
        try:
            cur_list = json.loads(str(g["payload_json"] or "[]"))
        except Exception:
            cur_list = []
        new_list, _ = merge_supabase(cur_list, supabase_entry)
        with conn:
            conn.execute(
                "UPDATE config_documents SET payload_json=?, version=?, "
                "updated_utc=strftime('%Y-%m-%d %H:%M:%f','now') "
                "WHERE domain='database_configurations'",
                (json.dumps(new_list, separators=(",", ":")), int(g["version"] or 1) + 1),
            )
        print(f"   + global -> {len(new_list)} DB row(s)")

    conn.close()
    print("\nDone. Restart the edge backend to pick up the refreshed list.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
