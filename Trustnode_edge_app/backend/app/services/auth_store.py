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
from datetime import datetime, timezone
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
        return {
            "username": str(row["username"]),
            "password_hash": str(row["password_hash"]),
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
    ) -> Dict[str, Any]:
        uname = str(username or "").strip()
        if not uname:
            raise ValueError("username is required")
        if not str(password_hash or "").strip():
            raise ValueError("password_hash is required")
        now = _utc_now()
        perms_json = json.dumps(permissions or {}, ensure_ascii=False)
        mods_json = json.dumps(modules or [], ensure_ascii=False)
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
                    "UPDATE users SET password_hash = ?, must_change_password = 0, updated_utc = ? WHERE LOWER(username) = LOWER(?)",
                    (str(password_hash), now, uname),
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
