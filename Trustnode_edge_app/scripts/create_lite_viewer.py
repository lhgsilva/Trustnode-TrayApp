"""Create one Lite viewer in Supabase Auth + map it to a tenant.

Inserts directly into auth.users + auth.identities + public.lite_profiles
using the database superuser. Returns the credentials the operator will
use to log into TrustNode Lite.

Idempotent: if the email already exists, we update the password instead
of duplicating.

Usage:
    python create_lite_viewer.py <email> <password> [tenant_id]
"""
from __future__ import annotations
import io
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import psycopg
import bcrypt  # type: ignore


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: create_lite_viewer.py <email> <password> [tenant_id]", file=sys.stderr)
        return 2
    email = sys.argv[1].strip().lower()
    password = sys.argv[2]
    tenant_id = sys.argv[3].strip() if len(sys.argv) >= 4 else "default"
    here = Path(__file__).resolve().parent.parent  # Trustnode_edge_app/
    env = load_env(here / ".env")

    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    now = datetime.now(timezone.utc)

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
        # Look up existing user by email so we don't duplicate.
        cur.execute("SELECT id FROM auth.users WHERE lower(email) = %s", (email,))
        row = cur.fetchone()
        if row:
            user_id = row[0]
            print(f"User already exists: {user_id}; updating password and profile.")
            cur.execute("""
                UPDATE auth.users
                   SET encrypted_password = %s,
                       updated_at         = %s,
                       email_confirmed_at = COALESCE(email_confirmed_at, %s)
                 WHERE id = %s
            """, (pw_hash, now, now, user_id))
        else:
            user_id = uuid.uuid4()
            # Minimum columns Supabase Auth needs to allow password sign-in.
            cur.execute("""
                INSERT INTO auth.users (
                    instance_id,
                    id,
                    aud,
                    role,
                    email,
                    encrypted_password,
                    email_confirmed_at,
                    raw_app_meta_data,
                    raw_user_meta_data,
                    created_at,
                    updated_at,
                    is_sso_user,
                    is_anonymous
                ) VALUES (
                    '00000000-0000-0000-0000-000000000000',
                    %s, 'authenticated', 'authenticated',
                    %s, %s, %s,
                    %s::jsonb, %s::jsonb,
                    %s, %s,
                    false, false
                )
            """, (
                str(user_id), email, pw_hash, now,
                json.dumps({"provider": "email", "providers": ["email"]}),
                json.dumps({}),
                now, now,
            ))
            # auth.identities row so Supabase considers the password identity
            # valid. provider_id must equal the user id for email/password.
            cur.execute("""
                INSERT INTO auth.identities (
                    id,
                    user_id,
                    provider_id,
                    identity_data,
                    provider,
                    last_sign_in_at,
                    created_at,
                    updated_at
                ) VALUES (
                    gen_random_uuid(), %s, %s,
                    %s::jsonb, 'email',
                    NULL, %s, %s
                )
            """, (
                str(user_id), str(user_id),
                json.dumps({"sub": str(user_id), "email": email, "email_verified": True}),
                now, now,
            ))
            print(f"Created auth.users: {user_id}")

        # GoTrue's password-grant SQL is strict: several token columns must be
        # empty strings, not NULL, or sign-in fails with "Database error
        # querying schema". Force the empty-string defaults on every run so a
        # re-created user starts in a clean state.
        cur.execute("""
            UPDATE auth.users
               SET confirmation_token         = COALESCE(confirmation_token, ''),
                   recovery_token             = COALESCE(recovery_token, ''),
                   email_change_token_current = COALESCE(email_change_token_current, ''),
                   email_change_token_new     = COALESCE(email_change_token_new, ''),
                   reauthentication_token     = COALESCE(reauthentication_token, ''),
                   email_change               = COALESCE(email_change, ''),
                   phone_change               = COALESCE(phone_change, ''),
                   phone_change_token         = COALESCE(phone_change_token, '')
             WHERE id = %s
        """, (str(user_id),))

        # Map to tenant in lite_profiles. UPSERT pattern.
        cur.execute("""
            INSERT INTO public.lite_profiles (user_id, tenant_id, email, role, updated_utc)
            VALUES (%s, %s, %s, 'viewer', %s)
            ON CONFLICT (user_id) DO UPDATE
              SET tenant_id   = EXCLUDED.tenant_id,
                  email       = EXCLUDED.email,
                  role        = EXCLUDED.role,
                  updated_utc = EXCLUDED.updated_utc
        """, (str(user_id), tenant_id, email, now))
        conn.commit()

        print()
        print("Lite viewer ready:")
        print(f"  user_id   = {user_id}")
        print(f"  email     = {email}")
        print(f"  tenant_id = {tenant_id}")
        print(f"  password  = (the one you passed on the command line)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
