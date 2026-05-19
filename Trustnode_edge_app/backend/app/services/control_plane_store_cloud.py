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
import os
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
        # CRITICAL: raw_connection() returns a SQLAlchemy _ConnectionFairy
        # that holds a connection from the pool. Calling .close() on it
        # RELEASES it back to the pool; without that, every _connect()
        # leaks a pooled connection and the pool drains within seconds
        # under burst load (causing psycopg.errors.ConnectionTimeout
        # because new TCP connects to the Supabase Pooler can't keep up
        # with the leak rate).
        if self._raw is None:
            return
        try:
            self._raw.close()
        except Exception:
            pass
        finally:
            self._raw = None

    # Context-manager protocol so `with self._connect() as conn:` works
    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()


# ---------------------------------------------------------------------------
# Cloud-backed control plane store
# ---------------------------------------------------------------------------

class ControlPlaneStoreCloud(ControlPlaneStore):
    """Same interface as ControlPlaneStore, but every `_connect()` returns
    a connection to the cloud Postgres / Supabase project instead of the
    local SQLite file."""

    # How long (seconds) the burst-shield read cache holds list_* /
    # tenant_summary results before going back to Supabase. A few seconds
    # is enough for the portal's first-page-load burst (the same browser
    # hits 10+ endpoints in parallel) to land mostly on cache. Per-page
    # navigation invalidates naturally since each click re-acquires under
    # the same TTL window.
    _BURST_CACHE_TTL_SECONDS = 5.0

    def __init__(self) -> None:
        # Bypass the SQLite parent's __init__ entirely — we don't have
        # a local file to point at. We DO need MODULE_CATALOG inherited
        # and RLock for any in-process serialization the methods might do.
        self._lock = threading.RLock()
        self._db_path = "<cloud:supabase>"
        # Read cache for hot lookups; same shape as SQLite path uses.
        self._customer_tenant_lookup_cache: dict[str, tuple[str, float]] = {}
        # Burst-shield cache for list_* and summary endpoints. Keys are
        # (method_name, frozen_kwargs); values are (timestamp, result).
        self._burst_cache: dict[tuple, tuple[float, Any]] = {}
        self._burst_cache_lock = threading.Lock()
        # Engine init is lazy AND serialized — but we proactively prime it
        # on the first __init__ call if env vars are present so the first
        # request doesn't hit cold engine creation under burst load.
        self._engine = None
        self._engine_init_lock = threading.Lock()
        # Eager prime so 12 parallel first-requests don't all race to
        # build the engine at once.
        try:
            self._get_engine()
        except Exception:
            # Failing here is fine — we'll retry on first real call.
            pass

    # -- Burst-shield cache helpers --------------------------------------

    def _cache_get(self, key: tuple) -> Any | None:
        import time as _t
        now = _t.time()
        with self._burst_cache_lock:
            entry = self._burst_cache.get(key)
            if entry and (now - entry[0]) < self._BURST_CACHE_TTL_SECONDS:
                return entry[1]
        return None

    def _cache_put(self, key: tuple, value: Any) -> None:
        import time as _t
        now = _t.time()
        with self._burst_cache_lock:
            self._burst_cache[key] = (now, value)
            # Bound the cache size at ~256 entries so a flood of distinct
            # tenant_ids can't grow it forever.
            if len(self._burst_cache) > 256:
                # Drop the oldest 64 entries
                oldest = sorted(self._burst_cache.items(), key=lambda kv: kv[1][0])[:64]
                for k, _ in oldest:
                    self._burst_cache.pop(k, None)

    def _cache_invalidate(self, *method_names: str) -> None:
        """Drop all entries for the given method names. Called after writes
        so the next read sees fresh data instead of stale cached rows."""
        with self._burst_cache_lock:
            doomed = [k for k in self._burst_cache.keys() if k[0] in method_names]
            for k in doomed:
                self._burst_cache.pop(k, None)

    # -- Connection management ---------------------------------------------

    def _get_engine(self) -> Any:
        """Return a dedicated SQLAlchemy engine for cp_* operations.

        Critical: do NOT share with app_store._get_or_create_cloud_engine.
        That engine is held long by the data-sync worker (lock_timeout
        1200ms + statement_timeout 4500ms + frequent multi-second batch
        inserts), so the portal's burst of cp_* reads ends up queueing
        behind data-sync writes and times out at the Pooler connect.

        We own our own engine with our own pool sizing tuned for short
        bursty reads.
        """
        if self._engine is not None:
            return self._engine
        # Serialize engine creation: under burst, many threads may hit
        # _connect at once; without this lock they'd all try to build the
        # engine in parallel (and likely all fail in the same way).
        with self._engine_init_lock:
            if self._engine is not None:
                return self._engine
            # Read cloud target from env DIRECTLY rather than going through
            # app_store._get_cloud_database_target — the latter touches the
            # local SQLite which adds startup risk and isn't necessary here.
            host = str(os.environ.get("TRUSTNODE_CLOUD_DB_HOST", "") or "").strip()
            port = int(os.environ.get("TRUSTNODE_CLOUD_DB_PORT", "5432") or "5432")
            database = str(os.environ.get("TRUSTNODE_CLOUD_DB_NAME", "postgres") or "postgres").strip() or "postgres"
            username = str(os.environ.get("TRUSTNODE_CLOUD_DB_USER", "") or "").strip()
            password = str(os.environ.get("TRUSTNODE_CLOUD_DB_PASSWORD", "") or "")
            sslmode = str(os.environ.get("TRUSTNODE_CLOUD_DB_SSLMODE", "require") or "require").strip().lower() or "require"
            if not host or not username:
                raise RuntimeError(
                    "ControlPlaneStoreCloud: missing TRUSTNODE_CLOUD_DB_HOST or _USER "
                    "in the process environment."
                )
            from sqlalchemy import create_engine  # type: ignore
            from urllib.parse import quote_plus as _q
            url = f"postgresql+psycopg://{_q(username)}:{_q(password)}@{host}:{port}/{database}"
            connect_args = {
                "sslmode": sslmode,
                "connect_timeout": int(os.environ.get("TRUSTNODE_CP_DB_CONNECT_TIMEOUT_SECONDS", "8") or "8"),
                "prepare_threshold": None,
                "options": "-c lock_timeout=400ms -c statement_timeout=2500ms",
            }
            self._engine = create_engine(
                url,
                pool_pre_ping=True,
                pool_size=int(os.environ.get("TRUSTNODE_CP_DB_POOL_SIZE", "6") or "6"),
                max_overflow=int(os.environ.get("TRUSTNODE_CP_DB_MAX_OVERFLOW", "10") or "10"),
                pool_recycle=300,
                pool_timeout=5,
                connect_args=connect_args,
            )
            return self._engine

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

    # -- Cached list reads (burst shield) --------------------------------

    def list_tenants(self, *, include_suspended: bool = True) -> list[dict[str, Any]]:  # type: ignore[override]
        key = ("list_tenants", bool(include_suspended))
        hit = self._cache_get(key)
        if hit is not None:
            return hit
        rows = super().list_tenants(include_suspended=include_suspended)
        self._cache_put(key, rows)
        return rows

    def list_customers(self, *, tenant_id: str | None = None,  # type: ignore[override]
                       all_tenants: bool = False) -> list[dict[str, Any]]:
        key = ("list_customers", str(tenant_id or ""), bool(all_tenants))
        hit = self._cache_get(key)
        if hit is not None:
            return hit
        rows = super().list_customers(tenant_id=tenant_id, all_tenants=all_tenants)
        self._cache_put(key, rows)
        return rows

    def list_edges(self, *, tenant_id: str | None = None,  # type: ignore[override]
                   all_tenants: bool = False) -> list[dict[str, Any]]:
        key = ("list_edges", str(tenant_id or ""), bool(all_tenants))
        hit = self._cache_get(key)
        if hit is not None:
            return hit
        rows = super().list_edges(tenant_id=tenant_id, all_tenants=all_tenants)
        self._cache_put(key, rows)
        return rows

    def list_licenses(self, *, tenant_id: str | None = None,  # type: ignore[override]
                      all_tenants: bool = False) -> list[dict[str, Any]]:
        key = ("list_licenses", str(tenant_id or ""), bool(all_tenants))
        hit = self._cache_get(key)
        if hit is not None:
            return hit
        rows = super().list_licenses(tenant_id=tenant_id, all_tenants=all_tenants)
        self._cache_put(key, rows)
        return rows

    def list_users(self, *, tenant_id: str | None = None,  # type: ignore[override]
                   all_tenants: bool = False) -> list[dict[str, Any]]:
        key = ("list_users", str(tenant_id or ""), bool(all_tenants))
        hit = self._cache_get(key)
        if hit is not None:
            return hit
        rows = super().list_users(tenant_id=tenant_id, all_tenants=all_tenants)
        self._cache_put(key, rows)
        return rows

    def tenant_summary(self, *, tenant_id: str) -> dict[str, Any]:  # type: ignore[override]
        key = ("tenant_summary", str(tenant_id or ""))
        hit = self._cache_get(key)
        if hit is not None:
            return hit
        result = super().tenant_summary(tenant_id=tenant_id)
        self._cache_put(key, result)
        return result

    # Writes: invalidate cache so the next read sees fresh data.

    def upsert_customer(self, *, tenant_id: str, customer_id: str, company_name: str,  # type: ignore[override]
                        contact_email: str = "", status: str = "active",
                        metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        result = super().upsert_customer(
            tenant_id=tenant_id, customer_id=customer_id, company_name=company_name,
            contact_email=contact_email, status=status, metadata=metadata,
        )
        self._cache_invalidate("list_customers", "tenant_summary")
        # Also invalidate the customer_tenant_id cache for this id since
        # the row was just rewritten.
        with self._lock:
            self._customer_tenant_lookup_cache.pop(str(customer_id or "").strip(), None)
        return result

    def delete_customer(self, *, tenant_id: str, customer_id: str) -> bool:  # type: ignore[override]
        out = super().delete_customer(tenant_id=tenant_id, customer_id=customer_id)
        self._cache_invalidate("list_customers", "tenant_summary", "list_edges", "list_licenses")
        with self._lock:
            self._customer_tenant_lookup_cache.pop(str(customer_id or "").strip(), None)
        return out

    def upsert_edge(self, *, tenant_id: str, edge_id: str, edge_name: str,  # type: ignore[override]
                    customer_id: str = "", site: str = "", area: str = "",
                    equipment: str = "", status: str = "inactive",
                    metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        result = super().upsert_edge(
            tenant_id=tenant_id, edge_id=edge_id, edge_name=edge_name,
            customer_id=customer_id, site=site, area=area, equipment=equipment,
            status=status, metadata=metadata,
        )
        self._cache_invalidate("list_edges", "tenant_summary")
        return result

    def delete_edge(self, *, tenant_id: str, edge_id: str) -> bool:  # type: ignore[override]
        out = super().delete_edge(tenant_id=tenant_id, edge_id=edge_id)
        self._cache_invalidate("list_edges", "tenant_summary")
        return out

    def delete_license(self, *, tenant_id: str, license_id: str) -> bool:  # type: ignore[override]
        out = super().delete_license(tenant_id=tenant_id, license_id=license_id)
        self._cache_invalidate("list_licenses", "tenant_summary")
        return out

    def delete_user(self, *, tenant_id: str, username: str) -> bool:  # type: ignore[override]
        out = super().delete_user(tenant_id=tenant_id, username=username)
        self._cache_invalidate("list_users", "tenant_summary")
        return out

    def issue_activation_code(self, **kwargs) -> dict[str, Any]:  # type: ignore[override]
        result = super().issue_activation_code(**kwargs)
        self._cache_invalidate("list_activation_codes")
        return result

    def list_activation_codes(self, *, tenant_id: str | None = None,  # type: ignore[override]
                              customer_id: str = "",
                              all_tenants: bool = False) -> list[dict[str, Any]]:
        """The Postgres `cp_edge_activation_codes` PK is `code_hash`, not
        `id` (the SQLite parent uses rowid → id). Use code_hash AS id so
        downstream code that expects an `id` column still works.

        Also cached against the burst-shield TTL — the portal Activation
        Codes page hits this on every page load."""
        cache_key = ("list_activation_codes", str(tenant_id or ""),
                     str(customer_id or ""), bool(all_tenants))
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        result = self._list_activation_codes_uncached(
            tenant_id=tenant_id, customer_id=customer_id, all_tenants=all_tenants,
        )
        self._cache_put(cache_key, result)
        return result

    def _list_activation_codes_uncached(self, *, tenant_id: str | None,
                                        customer_id: str, all_tenants: bool) -> list[dict[str, Any]]:
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
