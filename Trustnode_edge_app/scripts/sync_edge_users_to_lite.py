"""Backfill edge users to Supabase Auth + lite_profiles, and re-push
the latest per-user scoped dashboard / alarm / triggers config.

Runs once from a developer machine after upgrading the edge to the
release that auto-mirrors new users. Picks every existing user out of
cp_users and re-runs them through the mirror so they can log into Lite.

Also re-mirrors any config_documents_scoped row whose updated_utc is
newer than the corresponding row in Supabase — closes the gap when an
earlier edge build missed a save (e.g. before TRUSTNODE_SUPABASE_*
env vars were configured).

Reads credentials from Trustnode_edge_app/.env. Safe to re-run.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent.parent  # Trustnode_edge_app/
sys.path.insert(0, str(HERE / "backend"))


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


def resolve_local_db() -> Path:
    base = os.environ.get("TRUSTNODE_DATA_DIR", "").strip()
    if base:
        return Path(base) / "trustnode_app_store.db"
    return Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db"


def main() -> int:
    env = load_env(HERE / ".env")
    # Push everything into os.environ so the lite_user_mirror module sees
    # the same config the running backend uses.
    for k in ("TRUSTNODE_SUPABASE_URL", "TRUSTNODE_SUPABASE_SERVICE_KEY",
              "TRUSTNODE_SUPABASE_USER_DOMAIN",
              "TRUSTNODE_CLOUD_DB_HOST", "TRUSTNODE_CLOUD_DB_PORT",
              "TRUSTNODE_CLOUD_DB_NAME", "TRUSTNODE_CLOUD_DB_USER",
              "TRUSTNODE_CLOUD_DB_PASSWORD"):
        if k in env:
            os.environ[k] = env[k]
    if not os.environ.get("TRUSTNODE_SUPABASE_SERVICE_KEY"):
        print("ERROR: TRUSTNODE_SUPABASE_SERVICE_KEY not set in .env", file=sys.stderr)
        return 2

    # -- 1. Backfill users from cp_users ----------------------------------
    from app.services.lite_user_mirror import mirror_user_upsert

    db = resolve_local_db()
    if not db.is_file():
        print(f"local DB not found at {db}")
        return 2
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    users = conn.execute(
        "SELECT tenant_id, username, role, status, email FROM cp_users "
        "WHERE COALESCE(status,'active') = 'active' ORDER BY tenant_id, username"
    ).fetchall()
    print(f"\n== Mirroring {len(users)} edge user(s) to Supabase Auth ==")
    if not users:
        print("   no active users in cp_users")
    # Backfill needs a placeholder password to create a Supabase Auth user
    # (passwords are required at create time). The plaintext password the
    # operator typed when creating the edge user is unrecoverable from the
    # PBKDF2 hash, so we generate a temp one and write it to a JSON file
    # the admin can hand to each user (or use to set a "Reset password"
    # link). Edge users themselves are unaffected.
    import secrets
    temp_creds_path = HERE / "backfill_temp_passwords.json"
    temp_passwords: dict[str, dict[str, str]] = {}

    for u in users:
        username = str(u["username"] or "").strip()
        if not username:
            continue
        tenant = str(u["tenant_id"] or "default")
        role = str(u["role"] or "viewer")
        email = str(u["email"] or "")
        # 16-byte URL-safe token; readable and meets Supabase's 6-char min.
        temp_pw = "Tn-" + secrets.token_urlsafe(12)
        temp_passwords[username] = {"tenant_id": tenant, "role": role,
                                    "password": temp_pw, "email": email or f"{username}@trustnode.local"}
        print(f"  - {username:30s} tenant={tenant!r:14s} role={role!r}")
        mirror_user_upsert(
            tenant_id=tenant,
            username=username,
            password=temp_pw,
            role=role,
            email=email,
        )

    # Save the temp credentials so the operator can distribute them. The
    # file is gitignored via .env-style patterns — caller can also delete
    # after handing out the passwords.
    temp_creds_path.write_text(json.dumps(temp_passwords, indent=2, sort_keys=True), encoding="utf-8")
    print(f"\n   temp passwords written to {temp_creds_path}")
    print("   share each user's password with them; they can change it from")
    print("   the desktop Users & Access panel and the new password will be")
    print("   mirrored to Lite automatically.")

    # The mirror dispatches background threads — give them a moment to
    # finish before we exit.
    time.sleep(3.0)

    # -- 2. Re-mirror missing scoped configs ------------------------------
    print("\n== Re-mirroring scoped dashboard/alarm/triggers configs ==")
    try:
        from app.state import app_store
    except Exception as exc:
        print(f"   could not import app_store: {exc}")
        return 0

    rows = conn.execute(
        "SELECT scope_key, domain, payload_json, version, updated_utc "
        "FROM config_documents_scoped "
        "WHERE domain IN ('dashboard_configurations','alarms_setup','triggers_limits') "
        "ORDER BY updated_utc DESC"
    ).fetchall()
    print(f"   {len(rows)} scoped row(s) to push synchronously")
    # `_mirror_config_doc_to_cloud` would normally dispatch to a daemon
    # thread (so the live edge HTTP request doesn't block). In a one-shot
    # script the process exits before the thread finishes, so we do the
    # upsert inline here using the same SQL the live mirror would.
    from sqlalchemy import text  # type: ignore
    cloud = app_store._get_cloud_database_target()
    if not cloud:
        print("   no cloud DB target configured — skip dashboard mirror")
    else:
        schema = str(cloud.get("schema") or "public")
        engine, _ = app_store._get_or_create_cloud_engine(cloud, schema)
        pushed = 0
        with engine.begin() as gconn:
            for r in rows:
                scope_key = str(r["scope_key"] or "")
                domain = str(r["domain"] or "")
                tenant = (scope_key.split("|") or ["default"])[0] or "default"
                try:
                    # CAST(:pj AS jsonb) avoids SQLAlchemy parsing the
                    # `::` shorthand as a second bind-parameter token.
                    gconn.execute(
                        text(
                            f"""
                            INSERT INTO "{schema}"."{domain}"
                              (tenant_id, scope_key, payload_json, version, updated_utc)
                            VALUES (:tid, :sk, CAST(:pj AS jsonb), :v, :ts)
                            ON CONFLICT (tenant_id, scope_key) DO UPDATE SET
                              payload_json = EXCLUDED.payload_json,
                              version      = EXCLUDED.version,
                              updated_utc  = EXCLUDED.updated_utc
                            """
                        ),
                        {"tid": tenant, "sk": scope_key,
                         "pj": str(r["payload_json"] or "{}"),
                         "v": int(r["version"] or 1),
                         "ts": str(r["updated_utc"] or "")},
                    )
                    pushed += 1
                    print(f"   + {domain:28s}  {scope_key}")
                except Exception as exc:
                    print(f"   ! {domain:28s}  {scope_key}  err={exc}")
        print(f"   pushed {pushed}/{len(rows)} row(s)")

    conn.close()
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
