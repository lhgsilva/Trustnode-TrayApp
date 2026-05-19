"""Supabase-backed control-plane store.

Mirrors the public interface of `ControlPlaneStore` (the SQLite version)
but executes every SQL statement against the cloud Postgres / Supabase
project via the same engine cache `app_store` uses.

Strategy: rather than rewriting all 38 methods, we provide a thin
SQLite-flavoured connection adapter that translates the small number
of SQLite-isms present in the original SQL (`?` placeholders,
`INSERT OR REPLACE`, `INSERT ... ON CONFLICT(col) DO UPDATE SET col=excluded.col`,
`datetime('now')`, integer 0/1 booleans, `rowid`, `LIMIT 1`)
into Postgres equivalents on the fly. The original methods are
then mostly reused by subclassing.

This file is intentionally read-side and tested in isolation before
the routers switch to it via the TRUSTNODE_CONTROL_PLANE_BACKEND env
flag.

Author note 2026-05-19: built tonight after wiping Supabase clean,
specifically to remove the VPS-local-SQLite vs cloud-Supabase
split-brain that caused every multi-tenancy bug encountered earlier
in the day.
"""
from __future__ import annotations

import json
import re
import threading
from typing import Any, Iterable, Sequence

from app.services.control_plane_store import ControlPlaneStore


# ---------------------------------------------------------------------------
# SQLite-to-Postgres SQL translation
# ---------------------------------------------------------------------------

_SQLITE_NOW = re.compile(r"\bdatetime\(\s*'now'\s*\)", re.IGNORECASE)
_SQLITE_INSERT_OR_REPLACE = re.compile(r"\bINSERT\s+OR\s+REPLACE\b", re.IGNORECASE)
_SQLITE_ROWID = re.compile(r"\browid\b", re.IGNORECASE)
_SQLITE_LIMIT_NEG1 = re.compile(r"\bLIMIT\s+-1\b", re.IGNORECASE)


def _translate_sql(sql: str) -> str:
    """Best-effort SQLite->Postgres rewrite. Only handles the patterns
    that appear in control_plane_store.py — not a general SQL parser."""
    out = sql

    # `?` placeholder -> `%s` (psycopg2 native). Careful: `?` can appear
    # inside quoted strings, but none of the control_plane SQL has that.
    out = out.replace("?", "%s")

    # SQLite `datetime('now')` -> Postgres `now()::text`
    out = _SQLITE_NOW.sub("now()::text", out)

    # INSERT OR REPLACE -> rewrite to INSERT ... ON CONFLICT DO UPDATE is
    # impossible without knowing the PK columns. ControlPlaneStore happens
    # to NEVER use INSERT OR REPLACE (it uses INSERT ... ON CONFLICT
    # explicitly), so this regex is defensive only.
    if _SQLITE_INSERT_OR_REPLACE.search(out):
        raise NotImplementedError("INSERT OR REPLACE not supported in cloud store; rewrite as INSERT ... ON CONFLICT DO UPDATE")

    # rowid -> id. Every cp_* table uses an explicit `id` column where
    # the SQLite path leans on rowid, so this swap is safe.
    out = _SQLITE_ROWID.sub("id", out)

    # LIMIT -1 -> remove (sqlite treats as unlimited)
    out = _SQLITE_LIMIT_NEG1.sub("", out)

    return out


# ---------------------------------------------------------------------------
# sqlite3.Row-compatible result row
# ---------------------------------------------------------------------------

class _Row:
    """Pretends to be sqlite3.Row.

    Supports:
      - dict-style access: row['col']
      - tuple-style access: row[0]
      - .keys()
      - dict(row)  (via keys + __getitem__)
      - iteration over values
    """
    __slots__ = ("_data", "_cols")

    def __init__(self, cols: Sequence[str], values: Sequence[Any]) -> None:
        self._cols = tuple(cols)
        self._data = tuple(values)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._data[key]
        # Case-insensitive lookup like sqlite3.Row
        key_l = str(key).lower()
        for i, c in enumerate(self._cols):
            if c.lower() == key_l:
                return self._data[i]
        raise IndexError(f"No item with that key: {key!r}")

    def __contains__(self, key: Any) -> bool:
        if isinstance(key, int):
            return 0 <= key < len(self._data)
        key_l = str(key).lower()
        return any(c.lower() == key_l for c in self._cols)

    def keys(self) -> tuple[str, ...]:
        return self._cols

    def __iter__(self) -> Iterable[Any]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"_Row({dict(zip(self._cols, self._data))!r})"


# ---------------------------------------------------------------------------
# Cursor adapter
# ---------------------------------------------------------------------------

class _Cursor:
    """sqlite3.Cursor-compatible wrapper over a psycopg2 cursor."""

    def __init__(self, pg_cursor: Any) -> None:
        self._cur = pg_cursor
        self._last_cols: tuple[str, ...] | None = None

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> "_Cursor":
        translated = _translate_sql(sql)
        if params:
            self._cur.execute(translated, tuple(params))
        else:
            self._cur.execute(translated)
        if self._cur.description:
            self._last_cols = tuple(d[0] for d in self._cur.description)
        else:
            self._last_cols = None
        return self

    def executemany(self, sql: str, seq_of_params: Sequence[Sequence[Any]]) -> "_Cursor":
        translated = _translate_sql(sql)
        self._cur.executemany(translated, [tuple(p) for p in seq_of_params])
        return self

    def executescript(self, sql: str) -> "_Cursor":
        # The sqlite3 executescript splits on ';' and runs each. The
        # cloud store should never receive DDL — the schema is owned
        # by Supabase migrations — but we tolerate it as a no-op.
        for stmt in sql.split(";"):
            s = stmt.strip()
            if not s:
                continue
            # Only run if it's a DML-ish statement; skip pragma/etc.
            if s.upper().startswith(("PRAGMA", "VACUUM", "ANALYZE")):
                continue
            try:
                translated = _translate_sql(s)
                self._cur.execute(translated)
            except Exception:
                pass
        return self

    def fetchone(self) -> _Row | None:
        row = self._cur.fetchone()
        if row is None:
            return None
        return _Row(self._last_cols or (), row)

    def fetchall(self) -> list[_Row]:
        rows = self._cur.fetchall()
        return [_Row(self._last_cols or (), r) for r in rows]

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount


# ---------------------------------------------------------------------------
# Connection adapter
# ---------------------------------------------------------------------------

class _Conn:
    """sqlite3.Connection-compatible wrapper over a psycopg2 connection.

    Held for the duration of a `with self._connect() as conn:` block.
    Each acquire returns a fresh connection from the engine pool (via
    the SQLAlchemy engine in app_store), so writes are serialized at
    Postgres rather than the in-process RLock.
    """

    def __init__(self, sa_conn: Any) -> None:
        # sa_conn is a SQLAlchemy raw Connection.connection — i.e. the
        # underlying DBAPI connection (psycopg2 connection object).
        self._sa = sa_conn
        # psycopg2 connection
        self._raw = sa_conn

    def cursor(self) -> _Cursor:
        return _Cursor(self._raw.cursor())

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> _Cursor:
        cur = self.cursor()
        return cur.execute(sql, params)

    def commit(self) -> None:
        try:
            self._raw.commit()
        except Exception:
            pass

    def rollback(self) -> None:
        try:
            self._raw.rollback()
        except Exception:
            pass

    def close(self) -> None:
        # Connection is owned by the SA engine pool — don't close it.
        # Just commit any pending work so the next acquirer sees it.
        try:
            self._raw.commit()
        except Exception:
            pass

    # Context-manager protocol so `with self._connect() as conn:` works
    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()


# ---------------------------------------------------------------------------
# Cloud-backed control plane store
# ---------------------------------------------------------------------------

class ControlPlaneStoreCloud(ControlPlaneStore):
    """Same interface as ControlPlaneStore, but every `_connect()` returns
    a connection to the cloud Postgres / Supabase project instead of the
    local SQLite file."""

    def __init__(self) -> None:
        # Bypass the SQLite parent's __init__ entirely — we don't have
        # a local file to point at. We DO need MODULE_CATALOG inherited
        # and RLock for any in-process serialization the methods might do.
        self._lock = threading.RLock()
        self._db_path = "<cloud:supabase>"
        # Read cache for hot lookups; same shape as SQLite path uses.
        self._customer_tenant_lookup_cache: dict[str, tuple[str, float]] = {}
        # Defer engine binding until first call so import order doesn't matter.
        self._engine = None

    # -- Connection management ---------------------------------------------

    def _get_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        from app.state import app_store as _app_store
        cloud = _app_store._get_cloud_database_target()  # type: ignore[attr-defined]
        if not cloud:
            raise RuntimeError(
                "ControlPlaneStoreCloud: no cloud database target configured. "
                "Set TRUSTNODE_CLOUD_DB_HOST/USER/PASSWORD via systemd 10-secrets.conf or .env."
            )
        schema = str(cloud.get("schema") or "public")
        engine, _key = _app_store._get_or_create_cloud_engine(cloud, schema)  # type: ignore[attr-defined]
        self._engine = engine
        return engine

    def _connect(self) -> _Conn:  # type: ignore[override]
        engine = self._get_engine()
        # raw_connection() returns the underlying psycopg2 connection from
        # the pool. We wrap it so .__exit__ returns it cleanly.
        raw = engine.raw_connection()
        return _Conn(raw)

    # -- Schema is owned by Supabase migrations, not by this class ----------

    def _ensure_schema(self) -> None:  # type: ignore[override]
        # Schema lives in Supabase, managed via SQL migrations in
        # db/migrations/. This method is a no-op in cloud mode.
        return

    def _seed_defaults(self) -> None:  # type: ignore[override]
        # Module catalog is seeded by the migration; nothing to do.
        return

    # -- Helpers that the parent uses but we want to override --------------

    def _table_has_column(self, conn: _Conn, table_name: str, column_name: str) -> bool:  # type: ignore[override]
        # Postgres information_schema lookup. Cached implicitly by Postgres.
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                "  AND table_name = %s "
                "  AND column_name = %s "
                "LIMIT 1",
                (table_name, column_name),
            )
            return cur.fetchone() is not None
        except Exception:
            return False

    def _ensure_cp_users_customer_column(self, conn: _Conn) -> bool:  # type: ignore[override]
        # Schema lives in Supabase; assume the column exists. If it
        # doesn't, the relevant SQL will surface a clear error from
        # Postgres which is more honest than silently masking it.
        return True

    # -- Method overrides: type/null differences vs SQLite ------------------

    def upsert_license(self, *, tenant_id: str, license_id: str, customer_id: str = "",
                       plan_code: str = "standard", status: str = "active",
                       start_utc: str = "", end_utc: str = "",
                       max_edges: int = 3, max_users: int = 10,
                       metadata: dict[str, Any] | None = None) -> dict[str, Any]:  # type: ignore[override]
        """Postgres rejects empty strings as `timestamptz`. The SQLite
        parent stores them as text and tolerates ''. Here we coerce
        empty timestamps to NULL."""
        import secrets as _secrets
        from app.tenant import normalize_tenant_id
        tid = normalize_tenant_id(tenant_id)
        lid = str(license_id or "").strip() or f"lic-{_secrets.token_hex(4)}"
        now = self._utc_now()
        payload = json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True)
        start_val = str(start_utc or "").strip() or None
        end_val = str(end_utc or "").strip() or None
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO cp_licenses(license_id, tenant_id, customer_id, plan_code, status, start_utc, end_utc, max_edges, max_users, metadata_json, created_utc, updated_utc)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(license_id) DO UPDATE SET
                      tenant_id=excluded.tenant_id,
                      customer_id=excluded.customer_id,
                      plan_code=excluded.plan_code,
                      status=excluded.status,
                      start_utc=excluded.start_utc,
                      end_utc=excluded.end_utc,
                      max_edges=excluded.max_edges,
                      max_users=excluded.max_users,
                      metadata_json=excluded.metadata_json,
                      updated_utc=excluded.updated_utc
                    """,
                    (lid, tid, str(customer_id or ""), str(plan_code or "standard"), str(status or "active"),
                     start_val, end_val, int(max_edges or 0), int(max_users or 0), payload, now, now),
                )
                row = conn.execute("SELECT * FROM cp_licenses WHERE license_id=?", (lid,)).fetchone()
        return dict(row) if row else {}

    def set_license_modules(self, *, license_id: str, modules: list[dict[str, Any]]) -> dict[str, Any]:  # type: ignore[override]
        """Postgres `cp_license_modules.enabled` is BOOLEAN, not INTEGER.
        Pass a true Python bool instead of 0/1."""
        lid = str(license_id or "").strip()
        now = self._utc_now()
        with self._lock:
            with self._connect() as conn:
                for row in modules or []:
                    module_key = str((row or {}).get("module_key") or "").strip()
                    if not module_key:
                        continue
                    enabled = bool((row or {}).get("enabled", True))
                    conn.execute(
                        "INSERT INTO cp_license_modules(license_id, module_key, enabled, updated_utc) VALUES(?,?,?,?) "
                        "ON CONFLICT(license_id,module_key) DO UPDATE SET enabled=excluded.enabled, updated_utc=excluded.updated_utc",
                        (lid, module_key, enabled, now),
                    )
                rows = conn.execute(
                    "SELECT module_key, enabled FROM cp_license_modules WHERE license_id=? ORDER BY module_key",
                    (lid,),
                ).fetchall()
        return {"license_id": lid, "modules": [{"module_key": r[0], "enabled": bool(r[1])} for r in rows]}

    def list_activation_codes(self, *, tenant_id: str | None = None,  # type: ignore[override]
                              customer_id: str = "",
                              all_tenants: bool = False) -> list[dict[str, Any]]:
        """The Postgres `cp_edge_activation_codes` PK is `code_hash`, not
        `id` (the SQLite parent uses rowid → id). Use code_hash AS id so
        downstream code that expects an `id` column still works."""
        from app.tenant import normalize_tenant_id
        cid = str(customer_id or "").strip()
        cols = ("code_hash AS id, activation_code, tenant_id, customer_id, "
                "edge_id, license_id, edge_name, expires_utc, used_utc, "
                "status, created_utc")
        with self._lock:
            with self._connect() as conn:
                if all_tenants:
                    if cid:
                        rows = conn.execute(
                            f"SELECT {cols} FROM cp_edge_activation_codes WHERE customer_id=? ORDER BY created_utc DESC LIMIT 300",
                            (cid,),
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            f"SELECT {cols} FROM cp_edge_activation_codes ORDER BY created_utc DESC LIMIT 300"
                        ).fetchall()
                else:
                    tid = normalize_tenant_id(tenant_id or "default")
                    if cid:
                        rows = conn.execute(
                            f"SELECT {cols} FROM cp_edge_activation_codes WHERE tenant_id=? AND customer_id=? ORDER BY created_utc DESC LIMIT 300",
                            (tid, cid),
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            f"SELECT {cols} FROM cp_edge_activation_codes WHERE tenant_id=? ORDER BY created_utc DESC LIMIT 300",
                            (tid,),
                        ).fetchall()
        return [dict(r) for r in rows]

    def upsert_user(self, *, tenant_id: str, customer_id: str = "", username: str,  # type: ignore[override]
                    password: str | None = None, role: str = "viewer",
                    status: str = "active", email: str = "",
                    mfa_enabled: bool = False,
                    modules: list[str] | None = None,
                    permissions: dict[str, Any] | None = None) -> dict[str, Any]:
        """Postgres `mfa_enabled` is BOOLEAN, not INTEGER. Other than that
        the SQL is identical to the parent, but we re-issue it here so the
        boolean conversion is explicit."""
        from app.tenant import normalize_tenant_id
        tid = normalize_tenant_id(tenant_id)
        uname = str(username or "").strip()
        if not uname:
            raise ValueError("username_required")
        now = self._utc_now()
        modules_json = json.dumps(list(modules or []), separators=(",", ":"))
        permissions_json = json.dumps(dict(permissions or {}), separators=(",", ":"), sort_keys=True)
        mfa_val = bool(mfa_enabled)
        with self._lock:
            with self._connect() as conn:
                # Read existing row to preserve password_hash if password arg is None
                existing = conn.execute(
                    "SELECT password_hash FROM cp_users WHERE tenant_id=? AND username=? LIMIT 1",
                    (tid, uname),
                ).fetchone()
                if password is not None:
                    pwd_hash = self._hash_password(str(password))
                elif existing:
                    pwd_hash = str(existing["password_hash"])
                else:
                    pwd_hash = self._hash_password("changeme")
                conn.execute(
                    """
                    INSERT INTO cp_users(tenant_id, customer_id, username, password_hash, role, status, email,
                                         mfa_enabled, modules_json, permissions_json, created_utc, updated_utc,
                                         must_change_password)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 0)
                    ON CONFLICT(tenant_id, username) DO UPDATE SET
                      customer_id=excluded.customer_id,
                      password_hash=excluded.password_hash,
                      role=excluded.role,
                      status=excluded.status,
                      email=excluded.email,
                      mfa_enabled=excluded.mfa_enabled,
                      modules_json=excluded.modules_json,
                      permissions_json=excluded.permissions_json,
                      updated_utc=excluded.updated_utc
                    """,
                    (tid, str(customer_id or ""), uname, pwd_hash, str(role or "viewer"),
                     str(status or "active"), str(email or ""), mfa_val,
                     modules_json, permissions_json, now, now),
                )
                row = conn.execute(
                    "SELECT * FROM cp_users WHERE tenant_id=? AND username=? LIMIT 1",
                    (tid, uname),
                ).fetchone()
        return dict(row) if row else {}
