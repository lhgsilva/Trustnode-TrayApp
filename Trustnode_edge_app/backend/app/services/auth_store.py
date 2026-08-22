"""Dedicated auth + users SQLite store (operator 2026-06-18).

Why this exists
---------------
The old auth path reached into app_store on every login: get_bootstrap()
to read users_access, get_or_create_auth_secret() to sign the JWT. Both
acquire app_store._lock. app_store has 5+ background threads doing
cloud config sync; when Supabase is slow or unreachable, those threads
hold the lock for seconds-to-minutes. Login hangs. Customers see
"Failed to fetch" or an infinite "Signing in..." spinner. That is the
single most painful failure mode of the desktop app today.

This module owns its OWN SQLite file, its OWN connection pool, its
OWN (very rarely contended) lock. It has zero dependency on app_store
or any cloud component. Auth on a perfectly-disconnected machine is
the same code path as auth on a connected one.

Schema
------
  users(
    username        TEXT PRIMARY KEY,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'viewer',
    permissions_json TEXT NOT NULL DEFAULT '{}',
    modules_json    TEXT NOT NULL DEFAULT '[]',
    tenant_id       TEXT NOT NULL DEFAULT 'default',
    status          TEXT NOT NULL DEFAULT 'active',
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created_utc     TEXT NOT NULL,
    updated_utc     TEXT NOT NULL,
    last_login_utc  TEXT
  )
  auth_settings(id INTEGER PRIMARY KEY, secret TEXT, updated_utc TEXT)
  schema_meta(key TEXT PRIMARY KEY, value TEXT)

WAL mode is enabled so concurrent readers (auth checks during a write)
never block. Connections are short-lived (open per call, close after).

Public API
----------
  AuthStore() — singleton, instantiated from state.py
    db_path : Path of the SQLite file actually used
    get_or_create_secret() -> str
    list_users() -> list[dict]
    get_user(username) -> dict | None
    upsert_user(...) -> dict
    delete_user(username) -> bool
    set_user_password(username, new_hash) -> bool
    record_login(username, ok=True) -> None
    is_empty() -> bool
    migrate_from_app_store_payload(payload, actor) -> dict[str, int]

All methods are sync and complete in <5 ms on a healthy disk. No
thread, no cloud, no app_store dependency.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


_AUTH_DB_NAME = "trustnode_auth.db"


def _resolve_db_path() -> Path:
    """Pick the same data directory the app_store uses, so backups,
    workspace export/import, and the workspace detector all see the
    auth DB alongside the main app_store.

    Resolution order:
      1. TRUSTNODE_AUTH_DB_PATH (explicit override)
      2. TRUSTNODE_DATA_DIR / trustnode_auth.db
      3. Windows: %ProgramData%\\TrustNode\\edge\\trustnode_auth.db
      4. Other: ~/.trustnode_edge/data/trustnode_auth.db
    """
    explicit = os.environ.get("TRUSTNODE_AUTH_DB_PATH", "").strip()
    if explicit:
        p = Path(explicit)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    data_dir_env = os.environ.get("TRUSTNODE_DATA_DIR", "").strip()
    if data_dir_env:
        data_dir = Path(data_dir_env)
    elif sys.platform == "win32":
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        data_dir = Path(program_data) / "TrustNode" / "edge"
    else:
        data_dir = Path.home() / ".trustnode_edge" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / _AUTH_DB_NAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuthStore:
    """Local auth + users database. See module docstring for design."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path: Path = Path(db_path) if db_path else _resolve_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # The write lock guards the small UPDATE/INSERT paths. Reads
        # (the hot path: login matching) DO NOT take this lock — SQLite
        # WAL handles concurrent readers natively.
        self._write_lock = threading.Lock()
        self._ensure_schema()

    # ---- internal helpers ----------------------------------------------------

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            uri = f"file:{self.db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=3.0)
        else:
            conn = sqlite3.connect(str(self.db_path), timeout=3.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._write_lock:
            with self._connect() as conn:
                # WAL: concurrent readers never block on a writer. NORMAL
                # sync mode is the right trade-off for auth: a hard
                # power-cut might lose the last few login_audit rows but
                # never the user rows themselves (we fsync on commit).
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                      username             TEXT PRIMARY KEY,
                      password_hash        TEXT NOT NULL,
                      role                 TEXT NOT NULL DEFAULT 'viewer',
                      permissions_json     TEXT NOT NULL DEFAULT '{}',
                      modules_json         TEXT NOT NULL DEFAULT '[]',
                      tenant_id            TEXT NOT NULL DEFAULT 'default',
                      status               TEXT NOT NULL DEFAULT 'active',
                      must_change_password INTEGER NOT NULL DEFAULT 0,
                      created_utc          TEXT NOT NULL,
                      updated_utc          TEXT NOT NULL,
                      last_login_utc       TEXT
                    );
                    CREATE INDEX IF NOT EXISTS ix_users_tenant ON users(tenant_id);

                    CREATE TABLE IF NOT EXISTS auth_settings (
                      id          INTEGER PRIMARY KEY,
                      secret      TEXT NOT NULL,
                      updated_utc TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS schema_meta (
                      key   TEXT PRIMARY KEY,
                      value TEXT
                    );

                    CREATE TABLE IF NOT EXISTS login_audit (
                      id          INTEGER PRIMARY KEY AUTOINCREMENT,
                      ts_utc      TEXT NOT NULL,
                      username    TEXT,
                      ok          INTEGER NOT NULL,
                      remote_ip   TEXT,
                      detail      TEXT
                    );
                    CREATE INDEX IF NOT EXISTS ix_login_audit_ts ON login_audit(ts_utc DESC);
                    """
                )
                # Operator 2026-06-24: idempotent migration for the
                # password-reset feature. ALTER ADD COLUMN throws
                # OperationalError if the column already exists; we
                # swallow it so the migration is safe on repeat runs.
                for stmt in (
                    "ALTER TABLE users ADD COLUMN email TEXT",
                    "ALTER TABLE users ADD COLUMN reset_token TEXT",
                    "ALTER TABLE users ADD COLUMN reset_token_expires_utc TEXT",
                    # Operator 2026-08-21 (Remote Access): per-user token version
                    # (bumped on revoke -> every older JWT is rejected) and
                    # per-account lockout state for LAN-exposed logins.
                    "ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0",
                    # 2026-08-22 (named licence seats): which products this user
                    # consumes, and which UI a View LAN seat is served.
                    "ALTER TABLE users ADD COLUMN seats_json TEXT NOT NULL DEFAULT '[]'",
                    "ALTER TABLE users ADD COLUMN view_ui TEXT NOT NULL DEFAULT ''",
                    "CREATE INDEX IF NOT EXISTS ix_users_email ON users(LOWER(email))",
                    "CREATE TABLE IF NOT EXISTS login_lockouts (username TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0, first_failure_utc TEXT, locked_until_utc TEXT, updated_utc TEXT)",
                    "CREATE TABLE IF NOT EXISTS token_versions (username TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 0, updated_utc TEXT)",
                ):
                    try:
                        conn.execute(stmt)
                    except sqlite3.OperationalError:
                        pass
                try:
                    conn.execute("CREATE INDEX IF NOT EXISTS ix_users_email ON users(email)")
                    conn.execute("CREATE INDEX IF NOT EXISTS ix_users_reset_token ON users(reset_token)")
                except Exception:
                    pass
                conn.commit()

    # ---- secret -------------------------------------------------------------

    def get_or_create_secret(self) -> str:
        # Fast read path: WAL allows concurrent SELECTs without the lock.
        with self._connect(read_only=True) as conn:
            row = conn.execute("SELECT secret FROM auth_settings WHERE id = 1").fetchone()
            if row and str(row["secret"] or "").strip():
                return str(row["secret"])
        # Create path: serialized under the lock.
        with self._write_lock:
            with self._connect() as conn:
                # Re-check inside lock to avoid duplicate creates.
                row = conn.execute("SELECT secret FROM auth_settings WHERE id = 1").fetchone()
                if row and str(row["secret"] or "").strip():
                    return str(row["secret"])
                secret = secrets.token_hex(32)
                conn.execute(
                    "INSERT OR REPLACE INTO auth_settings(id, secret, updated_utc) VALUES(1, ?, ?)",
                    (secret, _utc_now()),
                )
                conn.commit()
                return secret

    # ---- users --------------------------------------------------------------

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> Dict[str, Any]:
        try:
            permissions = json.loads(row["permissions_json"] or "{}")
        except Exception:
            permissions = {}
        try:
            modules = json.loads(row["modules_json"] or "[]")
        except Exception:
            modules = []
        try:
            seats = json.loads((row["seats_json"] if "seats_json" in row.keys() else "[]") or "[]")
        except Exception:
            seats = []
        return {
            "username": str(row["username"]),
            "password_hash": str(row["password_hash"]),
            "email": str((row["email"] if "email" in row.keys() else "") or ""),
            "seats": [str(x) for x in seats] if isinstance(seats, list) else [],
            "view_ui": str((row["view_ui"] if "view_ui" in row.keys() else "") or ""),
            "role": str(row["role"] or "viewer"),
            "permissions": permissions if isinstance(permissions, dict) else {},
            "modules": modules if isinstance(modules, list) else [],
            "tenant_id": str(row["tenant_id"] or "default"),
            "status": str(row["status"] or "active"),
            "must_change_password": bool(row["must_change_password"]),
            "created_utc": str(row["created_utc"] or ""),
            "updated_utc": str(row["updated_utc"] or ""),
            "last_login_utc": str(row["last_login_utc"] or ""),
        }

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        uname = str(username or "").strip()
        if not uname:
            return None
        with self._connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (uname,)
            ).fetchone()
            if not row:
                return None
            return self._row_to_user(row)

    def find_by_login(self, identifier: str) -> Optional[Dict[str, Any]]:
        """Resolve a login identifier: username first, then e-mail.

        2026-08-22: licensed seats are handed to people by e-mail, so the
        address has to work as a login. Username is tried first so an existing
        account can never be shadowed by someone else's e-mail, and the internal
        identity (JWT `sub`, audit, revocation) remains the username."""
        ident = str(identifier or "").strip()
        if not ident:
            return None
        hit = self.get_user(ident)
        if hit:
            return hit
        if "@" not in ident:
            return None
        with self._connect(read_only=True) as conn:
            rows = conn.execute(
                "SELECT * FROM users WHERE email IS NOT NULL AND LOWER(email) = LOWER(?) "
                "ORDER BY username LIMIT 2",
                (ident,),
            ).fetchall()
        if len(rows) != 1:
            # 0 = unknown; >1 = ambiguous, which must never authenticate.
            return None
        return self._row_to_user(rows[0])

    def email_owner(self, email: str) -> str:
        """Username that already owns this e-mail ('' when free)."""
        addr = str(email or "").strip()
        if not addr:
            return ""
        with self._connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT username FROM users WHERE email IS NOT NULL AND LOWER(email) = LOWER(?) LIMIT 1",
                (addr,),
            ).fetchone()
        return str(row["username"]) if row else ""

    def count_seat_assignments(self) -> Dict[str, int]:
        """How many ACTIVE users hold each seat product."""
        out: Dict[str, int] = {}
        for u in self.list_users():
            if str(u.get("status") or "active").lower() != "active":
                continue
            for seat in (u.get("seats") or []):
                key = str(seat or "").strip().lower()
                if key:
                    out[key] = out.get(key, 0) + 1
        return out

    def list_users(self) -> List[Dict[str, Any]]:
        with self._connect(read_only=True) as conn:
            return [self._row_to_user(r) for r in conn.execute("SELECT * FROM users ORDER BY username").fetchall()]

    def is_empty(self) -> bool:
        with self._connect(read_only=True) as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
            return int(row["c"] if row else 0) == 0

    def upsert_user(
        self,
        *,
        username: str,
        password_hash: str,
        role: str = "viewer",
        permissions: Optional[Dict[str, Any]] = None,
        modules: Optional[List[Any]] = None,
        tenant_id: str = "default",
        status: str = "active",
        must_change_password: bool = False,
        email: Optional[str] = None,
        seats: Optional[List[Any]] = None,
        view_ui: Optional[str] = None,
    ) -> Dict[str, Any]:
        uname = str(username or "").strip()
        if not uname:
            raise ValueError("username is required")
        if not str(password_hash or "").strip():
            raise ValueError("password_hash is required")
        now = _utc_now()
        perms_json = json.dumps(permissions or {}, ensure_ascii=False)
        mods_json = json.dumps(modules or [], ensure_ascii=False)
        # None = "leave whatever is stored" so existing callers are untouched.
        email_txt = None if email is None else str(email or "").strip()
        seats_json = None if seats is None else json.dumps(
            [str(x).strip().lower() for x in (seats or []) if str(x).strip()], ensure_ascii=False)
        view_ui_txt = None if view_ui is None else str(view_ui or "").strip().lower()
        if email_txt:
            owner = self.email_owner(email_txt)
            if owner and owner.lower() != uname.lower():
                raise ValueError(
                    f"that e-mail address is already the login of user '{owner}'")
        with self._write_lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO users(
                        username, password_hash, role, permissions_json,
                        modules_json, tenant_id, status, must_change_password,
                        created_utc, updated_utc
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(username) DO UPDATE SET
                      password_hash       = excluded.password_hash,
                      role                = excluded.role,
                      permissions_json    = excluded.permissions_json,
                      modules_json        = excluded.modules_json,
                      tenant_id           = excluded.tenant_id,
                      status              = excluded.status,
                      must_change_password = excluded.must_change_password,
                      updated_utc         = excluded.updated_utc
                    """,
                    (
                        uname,
                        str(password_hash),
                        str(role or "viewer"),
                        perms_json,
                        mods_json,
                        str(tenant_id or "default"),
                        str(status or "active"),
                        1 if must_change_password else 0,
                        now,
                        now,
                    ),
                )
                # Seat / e-mail fields are written separately and only when the
                # caller supplied them, so every pre-2026-08-22 call site keeps
                # its exact behaviour (None = leave stored value alone).
                if email_txt is not None:
                    conn.execute("UPDATE users SET email=?, updated_utc=? WHERE username=?",
                                 (email_txt, now, uname))
                if seats_json is not None:
                    conn.execute("UPDATE users SET seats_json=?, updated_utc=? WHERE username=?",
                                 (seats_json, now, uname))
                if view_ui_txt is not None:
                    conn.execute("UPDATE users SET view_ui=?, updated_utc=? WHERE username=?",
                                 (view_ui_txt, now, uname))
                conn.commit()
        return self.get_user(uname) or {}

    def delete_user(self, username: str) -> bool:
        uname = str(username or "").strip()
        if not uname:
            return False
        with self._write_lock:
            with self._connect() as conn:
                cur = conn.execute("DELETE FROM users WHERE LOWER(username) = LOWER(?)", (uname,))
                conn.commit()
                return int(cur.rowcount or 0) > 0

    def set_user_password(self, username: str, password_hash: str) -> bool:
        uname = str(username or "").strip()
        if not uname or not str(password_hash or "").strip():
            return False
        now = _utc_now()
        with self._write_lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE users SET password_hash = ?, must_change_password = 0, updated_utc = ?, reset_token = NULL, reset_token_expires_utc = NULL WHERE LOWER(username) = LOWER(?)",
                    (str(password_hash), now, uname),
                )
                conn.commit()
                return int(cur.rowcount or 0) > 0

    # ---- password reset helpers (Operator 2026-06-24) ------------------
    def find_user_by_email_or_username(self, identifier: str) -> Optional[Dict[str, Any]]:
        """Look up a user by either their email OR username. Used by the
        forgot-password endpoint so the operator can type whichever they
        remember. Case-insensitive on both fields."""
        ident = str(identifier or "").strip()
        if not ident:
            return None
        with self._connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE LOWER(username) = LOWER(?) OR LOWER(COALESCE(email,'')) = LOWER(?) LIMIT 1",
                (ident, ident),
            ).fetchone()
            return self._row_to_user(row) if row else None

    def issue_reset_token(self, username: str, ttl_seconds: int = 1800) -> Optional[str]:
        """Generate a one-time password-reset token for the user and
        persist it with an expiry. Returns the raw token to be embedded
        in the email link, or None if the user doesn't exist. TTL
        defaults to 30 minutes — long enough for the email to arrive,
        short enough to limit damage if intercepted."""
        uname = str(username or "").strip()
        if not uname:
            return None
        token = secrets.token_urlsafe(32)
        now_iso = _utc_now()
        try:
            expires_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00")) + timedelta(seconds=max(60, int(ttl_seconds or 1800)))
            expires_iso = expires_dt.isoformat().replace("+00:00", "Z")
        except Exception:
            expires_iso = now_iso
        with self._write_lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE users SET reset_token = ?, reset_token_expires_utc = ?, updated_utc = ? WHERE LOWER(username) = LOWER(?)",
                    (token, expires_iso, now_iso, uname),
                )
                conn.commit()
                if int(cur.rowcount or 0) == 0:
                    return None
        return token

    def consume_reset_token(self, token: str) -> Optional[str]:
        """Verify a reset token and return the username it belongs to
        if it's valid and unexpired. The token is NOT cleared here —
        set_user_password() does that on the successful new-password
        commit so the operator can't get stuck in a 'token used but
        password not set' window."""
        tok = str(token or "").strip()
        if not tok:
            return None
        with self._connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT username, reset_token_expires_utc FROM users WHERE reset_token = ? LIMIT 1",
                (tok,),
            ).fetchone()
            if not row:
                return None
            expires = str(row["reset_token_expires_utc"] or "").strip()
            if expires:
                try:
                    exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                    now_dt = datetime.fromisoformat(_utc_now().replace("Z", "+00:00"))
                    if exp_dt < now_dt:
                        return None
                except Exception:
                    pass
            return str(row["username"])

    def set_user_email(self, username: str, email: str) -> bool:
        """Set or update a user's email address. Used by Settings →
        Users so operators can register an email to enable forgot-
        password recovery."""
        uname = str(username or "").strip()
        if not uname:
            return False
        clean_email = str(email or "").strip()
        now = _utc_now()
        with self._write_lock:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE users SET email = ?, updated_utc = ? WHERE LOWER(username) = LOWER(?)",
                    (clean_email or None, now, uname),
                )
                conn.commit()
                return int(cur.rowcount or 0) > 0

    def record_login(self, username: str, *, ok: bool = True, remote_ip: str = "", detail: str = "") -> None:
        # Best-effort. A failure here must NEVER block login.
        try:
            now = _utc_now()
            with self._write_lock:
                with self._connect() as conn:
                    if ok and username:
                        conn.execute(
                            "UPDATE users SET last_login_utc = ? WHERE LOWER(username) = LOWER(?)",
                            (now, str(username)),
                        )
                    conn.execute(
                        "INSERT INTO login_audit(ts_utc, username, ok, remote_ip, detail) VALUES(?,?,?,?,?)",
                        (now, str(username or ""), 1 if ok else 0, str(remote_ip or ""), str(detail or "")),
                    )
                    conn.commit()
        except Exception:
            pass

    # ---- Remote Access (2026-08-21): token revocation + account lockout ------
    def get_token_version(self, username: str) -> int:
        """Current token version for a user — kept in its own table so revocation
        works for users that live only in cp_users / the portal mirror."""
        try:
            with self._connect(read_only=True) as conn:
                row = conn.execute(
                    "SELECT version FROM token_versions WHERE LOWER(username) = LOWER(?)", (str(username or ""),)
                ).fetchone()
                return int(row[0] or 0) if row else 0
        except Exception:
            return 0

    def bump_token_version(self, username: str) -> int:
        """Invalidate every JWT issued so far for `username`."""
        try:
            with self._write_lock:
                with self._connect() as conn:
                    conn.execute(
                        "INSERT INTO token_versions(username, version, updated_utc) VALUES(?, 1, ?) "
                        "ON CONFLICT(username) DO UPDATE SET version = token_versions.version + 1, updated_utc = excluded.updated_utc",
                        (str(username or "").lower(), _utc_now()),
                    )
                    conn.commit()
            return self.get_token_version(username)
        except Exception:
            return 0

    LOCKOUT_THRESHOLD = 5
    LOCKOUT_WINDOW_S = 15 * 60
    LOCKOUT_DURATION_S = 15 * 60

    def locked_until(self, username: str) -> str:
        """ISO timestamp while the account is locked, else ''."""
        try:
            with self._connect(read_only=True) as conn:
                row = conn.execute(
                    "SELECT locked_until_utc FROM login_lockouts WHERE LOWER(username) = LOWER(?)",
                    (str(username or ""),),
                ).fetchone()
            until = str(row[0] or "") if row else ""
            if not until:
                return ""
            import datetime as _dt
            try:
                u = _dt.datetime.fromisoformat(until.replace("Z", "+00:00"))
                if u.tzinfo is None:
                    u = u.replace(tzinfo=_dt.timezone.utc)
                return until if u > _dt.datetime.now(_dt.timezone.utc) else ""
            except Exception:
                return ""
        except Exception:
            return ""

    def record_failed_attempt(self, username: str) -> str:
        """Count a failed login; returns locked_until (ISO) if this attempt
        tripped the threshold (5 failures within 15 min -> 15 min lock)."""
        if not username:
            return ""
        try:
            import datetime as _dt
            now_dt = _dt.datetime.now(_dt.timezone.utc)
            now = _utc_now()
            with self._write_lock:
                with self._connect() as conn:
                    row = conn.execute(
                        "SELECT failures, first_failure_utc FROM login_lockouts WHERE LOWER(username) = LOWER(?)",
                        (str(username),),
                    ).fetchone()
                    failures, first = (int(row[0] or 0), str(row[1] or "")) if row else (0, "")
                    window_open = False
                    if first:
                        try:
                            f_dt = _dt.datetime.fromisoformat(first.replace("Z", "+00:00"))
                            if f_dt.tzinfo is None:
                                f_dt = f_dt.replace(tzinfo=_dt.timezone.utc)
                            window_open = (now_dt - f_dt).total_seconds() < self.LOCKOUT_WINDOW_S
                        except Exception:
                            window_open = False
                    failures = failures + 1 if window_open else 1
                    first = first if window_open else now
                    locked_until = ""
                    if failures >= self.LOCKOUT_THRESHOLD:
                        locked_until = (now_dt + _dt.timedelta(seconds=self.LOCKOUT_DURATION_S)).strftime("%Y-%m-%dT%H:%M:%SZ")
                        failures = 0
                        first = ""
                    conn.execute(
                        "INSERT INTO login_lockouts(username, failures, first_failure_utc, locked_until_utc, updated_utc) "
                        "VALUES(?,?,?,?,?) ON CONFLICT(username) DO UPDATE SET failures=excluded.failures, "
                        "first_failure_utc=excluded.first_failure_utc, locked_until_utc=excluded.locked_until_utc, "
                        "updated_utc=excluded.updated_utc",
                        (str(username), failures, first, locked_until, now),
                    )
                    conn.commit()
            return locked_until
        except Exception:
            return ""

    def clear_failed_attempts(self, username: str) -> None:
        try:
            with self._write_lock:
                with self._connect() as conn:
                    conn.execute("DELETE FROM login_lockouts WHERE LOWER(username) = LOWER(?)", (str(username or ""),))
                    conn.commit()
        except Exception:
            pass

    # ---- migration ----------------------------------------------------------

    def migrate_from_app_store_payload(
        self,
        users_access_payload: Any,
        scoped_payloads: Optional[Iterable[Any]] = None,
        actor: str = "boot_migration",
    ) -> Dict[str, int]:
        """Copy users from the legacy app_store users_access docs into
        the AuthStore. Idempotent — uses INSERT OR REPLACE on username.

        Customer's existing data is the source of truth. We re-hash on
        the fly only if a plaintext password is encountered (legacy
        format some old configs still carry).

        Returns counts so the boot path can log what moved.
        """
        out = {"unscoped": 0, "scoped": 0, "skipped": 0}

        def _consume(payload: Any, bucket: str) -> None:
            if not isinstance(payload, dict):
                return
            users = payload.get("users")
            if not isinstance(users, list):
                return
            for u in users:
                if not isinstance(u, dict):
                    out["skipped"] += 1
                    continue
                uname = str(u.get("username") or "").strip()
                if not uname:
                    out["skipped"] += 1
                    continue
                raw_pwd = str(u.get("password") or "").strip()
                stored_hash = ""
                if raw_pwd.startswith("pbkdf2_sha256$") or raw_pwd.startswith("$2"):
                    # Already hashed (PBKDF2 or bcrypt). Take verbatim.
                    stored_hash = raw_pwd
                elif raw_pwd:
                    # Plaintext. Hash with the project's hasher so the
                    # auth router can verify it normally afterwards.
                    try:
                        from app.auth import hash_password as _hp
                        stored_hash = _hp(raw_pwd)
                    except Exception:
                        # No hasher? Store as-is — verify_password falls
                        # back to constant-time string compare for these.
                        stored_hash = raw_pwd
                else:
                    out["skipped"] += 1
                    continue
                try:
                    self.upsert_user(
                        username=uname,
                        password_hash=stored_hash,
                        role=str(u.get("role") or "viewer"),
                        permissions=u.get("permissions") if isinstance(u.get("permissions"), dict) else None,
                        modules=u.get("modules") if isinstance(u.get("modules"), list) else None,
                        tenant_id=str(u.get("tenant_id") or "default"),
                        status=str(u.get("status") or "active"),
                        must_change_password=bool(u.get("must_change_password")),
                    )
                    out[bucket] += 1
                except Exception:
                    out["skipped"] += 1

        _consume(users_access_payload, "unscoped")
        for sp in (scoped_payloads or []):
            _consume(sp, "scoped")
        return out
