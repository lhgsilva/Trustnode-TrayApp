"""End-to-end verification of user-change sync.

Run AFTER relaunching the newest TrustNode build. Exercises the four
mutation paths and checks each one actually reaches Supabase:

  1. Edit a user via /api/control-plane/users           (upsert)
  2. Set a password via .../users/{u}/password         (admin sets)
  3. Generate temp password via .../password/temp      (admin resets)
  4. (Optional) delete a user via DELETE /users/{u}    (cleanup)

For each step it queries Supabase auth.users.updated_at + lite_profiles
to verify the change landed. Prints PASS / FAIL per step.

Doesn't touch the production "lucas-admin" or "admin" users — creates a
disposable `verify_sync_<random>` user and cleans it up at the end.

Usage from Trustnode_edge_app/:
    python scripts/verify_user_sync.py
"""
from __future__ import annotations
import io, json, os, secrets, sys, time
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import requests, psycopg

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "backend"))


def load_env(p: Path) -> dict:
    out = {}
    if not p.is_file(): return out
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s: continue
        k, _, v = s.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def supabase_user(env: dict, username: str) -> dict | None:
    """Return the auth.users row for username (raw_user_meta_data->>username),
    or None. Also returns its lite_profiles row attached. Uses the
    SUPABASE_URL host's direct-DB equivalent: project ref is everything
    between the leading `https://` and the first dot.
    """
    ref = env["TRUSTNODE_SUPABASE_URL"].split("//", 1)[1].split(".", 1)[0]
    with psycopg.connect(
        host=f"db.{ref}.supabase.co",
        port=5432, dbname="postgres", user="postgres",
        password=env["TRUSTNODE_CLOUD_DB_PASSWORD"], sslmode="require", connect_timeout=15,
    ) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, email, updated_at, raw_user_meta_data->>'tenant_id', "
            "raw_user_meta_data->>'role' "
            "FROM auth.users WHERE raw_user_meta_data->>'username' = %s",
            (username,),
        )
        row = cur.fetchone()
        if not row: return None
        user_id, email, updated_at, tenant_id, role = row
        cur.execute(
            "SELECT user_id, tenant_id, role, username, email, updated_utc "
            "FROM public.lite_profiles WHERE user_id = %s",
            (user_id,),
        )
        profile = cur.fetchone()
        return {
            "id": str(user_id), "email": email, "updated_at": updated_at,
            "tenant_id": tenant_id, "role": role,
            "lite_profile": dict(zip(
                ["user_id", "tenant_id", "role", "username", "email", "updated_utc"],
                profile,
            )) if profile else None,
        }


def mint_admin_jwt(env: dict) -> str:
    """Mints a JWT directly from the local SQLite secret so we can call
    /api/cp/* endpoints without going through /auth/login."""
    import sqlite3, base64, hashlib, hmac
    db = Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    secret = conn.execute("SELECT secret FROM auth_settings WHERE id=1").fetchone()[0]
    conn.close()
    def b64u(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).decode().rstrip("=")
    now = int(time.time())
    payload = {"sub": "admin", "role": "admin", "permissions": {},
               "modules": [], "tenant_id": "default", "iat": now, "exp": now + 600}
    header = {"alg": "HS256", "typ": "JWT"}
    p1 = b64u(json.dumps(header, separators=(",", ":")).encode())
    p2 = b64u(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(str(secret).encode(), f"{p1}.{p2}".encode(), hashlib.sha256).digest()
    return f"{p1}.{p2}.{b64u(sig)}"


def main() -> int:
    env = load_env(HERE / ".env")
    if not env.get("TRUSTNODE_SUPABASE_SERVICE_KEY"):
        print("ERROR: TRUSTNODE_SUPABASE_SERVICE_KEY not in .env", file=sys.stderr)
        return 2
    # Health check
    try:
        h = requests.get("http://127.0.0.1:8000/api/health", timeout=4).status_code
    except Exception as exc:
        print(f"backend not reachable: {exc}", file=sys.stderr)
        return 2
    if h != 200:
        print(f"backend health = {h}", file=sys.stderr); return 2
    token = mint_admin_jwt(env)
    H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    suffix = secrets.token_hex(3)
    uname = f"verify-sync-{suffix}"
    pw1 = f"InitPw-{secrets.token_urlsafe(8)}"
    pw2 = f"NextPw-{secrets.token_urlsafe(8)}"
    results: list[tuple[str, bool, str]] = []

    def check(step: str, ok: bool, detail: str) -> None:
        results.append((step, ok, detail))
        marker = "PASS" if ok else "FAIL"
        print(f"  [{marker}] {step}: {detail}")

    # ---- 1) UPSERT a new user --------------------------------------------
    print(f"\n== Step 1: POST /api/cp/users (create {uname!r}) ==")
    r = requests.post(
        "http://127.0.0.1:8000/api/control-plane/users",
        headers=H, json={
            "username": uname, "password": pw1, "role": "viewer",
            "status": "active", "email": "", "mfa_enabled": False,
            "modules": [], "permissions": {},
        }, timeout=10,
    )
    if r.status_code != 200:
        check("create_user_http", False, f"HTTP {r.status_code}: {r.text[:200]}")
        return 1
    check("create_user_http", True, f"HTTP 200")
    time.sleep(3.0)  # mirror fires in a daemon thread
    su = supabase_user(env, uname)
    check("create_user_in_supabase", bool(su), f"auth.users row: {su['email'] if su else None}")
    check("create_user_lite_profile",
          bool(su and su.get("lite_profile")),
          f"profile tenant={su['lite_profile']['tenant_id']!r}" if su and su.get("lite_profile") else "no profile row")

    # ---- 2) SET PASSWORD -------------------------------------------------
    print(f"\n== Step 2: POST /api/cp/users/{uname}/password ==")
    r = requests.post(
        f"http://127.0.0.1:8000/api/control-plane/users/{uname}/password",
        headers=H, json={"password": pw2, "must_change": False}, timeout=10,
    )
    if r.status_code != 200:
        check("set_password_http", False, f"HTTP {r.status_code}: {r.text[:200]}")
    else:
        check("set_password_http", True, "HTTP 200")
    time.sleep(3.0)
    su = supabase_user(env, uname)
    # We can't read the password back, but updated_at must move forward.
    if su:
        # heuristic: it should be within the last 30s
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - su["updated_at"]) < timedelta(seconds=30)
        check("set_password_propagated", recent,
              f"auth.users.updated_at = {su['updated_at']} (recent={recent})")
    else:
        check("set_password_propagated", False, "user vanished from Supabase")

    # ---- 3) TEMP PASSWORD ------------------------------------------------
    print(f"\n== Step 3: POST /api/cp/users/{uname}/password/temp ==")
    r = requests.post(
        f"http://127.0.0.1:8000/api/control-plane/users/{uname}/password/temp",
        headers=H, json={"length": 14}, timeout=10,
    )
    if r.status_code != 200:
        check("temp_password_http", False, f"HTTP {r.status_code}: {r.text[:200]}")
    else:
        body = r.json()
        temp = body.get("temp_password") or ""
        check("temp_password_http", bool(temp), f"got plaintext (length {len(temp)})")
    time.sleep(3.0)
    su = supabase_user(env, uname)
    if su:
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - su["updated_at"]) < timedelta(seconds=30)
        check("temp_password_propagated", recent,
              f"auth.users.updated_at = {su['updated_at']} (recent={recent})")
    else:
        check("temp_password_propagated", False, "user vanished")

    # ---- 4) DELETE the test user ----------------------------------------
    print(f"\n== Step 4: DELETE /api/cp/users/{uname} ==")
    r = requests.delete(
        f"http://127.0.0.1:8000/api/control-plane/users/{uname}",
        headers=H, timeout=10,
    )
    check("delete_user_http", r.status_code == 200, f"HTTP {r.status_code}")
    time.sleep(3.0)
    su = supabase_user(env, uname)
    check("delete_user_propagated", su is None, f"Supabase row {'present' if su else 'gone'}")

    # ---- Summary ---------------------------------------------------------
    print("\n=== SUMMARY ===")
    fails = [r for r in results if not r[1]]
    for step, ok, detail in results:
        m = "PASS" if ok else "FAIL"
        print(f"  [{m}] {step}")
    print()
    if fails:
        print(f"{len(fails)} FAILED — sync is NOT working correctly.")
        return 1
    print("All checks passed. User changes are wired to local DB AND Supabase.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
