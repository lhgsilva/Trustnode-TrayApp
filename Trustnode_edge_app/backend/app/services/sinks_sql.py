"""Customer-DB schema + shared writers (operator 2026-06-17, M3).

Single source of truth for every table the edge maintains in the
customer's Postgres. Other modules (M4 parallel sink, M5 config
mirror, M7 view-link writer) call into this module rather than each
issuing their own CREATE TABLE.

Why this module exists
======================
Today three different code paths bootstrap Postgres schema in different
places:

  * `plc_manager.py:1820-1997` — historian_readings + live_latest for
    the operator-configured "primary" PLC sink.
  * `app_store.py:660-700` — Supabase cloud mirror schema.
  * `customer_sql.py` (M2) — connection pool only, no schema.

The "Customer DB mode" milestone needs ALL of the schema to live in one
place because:

  * Lite (M7) reads from these tables and can't tolerate drift.
  * M5 needs identical column names across SQLite and customer DB so
    the mirror loop is trivially symmetric.
  * MSSQL support (future) means swapping one helper, not three.

So this module defines:

  * Idempotent ``CREATE TABLE IF NOT EXISTS`` for every table the edge
    owns in the customer DB.
  * Schema version tracking, so future migrations bump
    `schema_meta.version` and run forward steps in order.
  * Run-once orchestration: `bootstrap_customer_db(engine)` runs every
    pending step under a single transaction and is safe to call on
    every boot.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Current schema version. Bump when you ADD a step.
SCHEMA_VERSION = 1

# Module-level guard so the bootstrap doesn't fire 100 times during a
# poll burst. Reset by `force_rebootstrap()` for tests / mode flips.
_bootstrap_lock = threading.Lock()
_bootstrap_done_for_cache_key: str = ""
_bootstrap_last_error: str = ""


def _table(schema: str, name: str) -> str:
    """Quoted schema-qualified table name. Postgres-flavoured today;
    the same shape works for MSSQL once we swap the dialect later.
    """
    schema = (schema or "public").strip() or "public"
    return f'"{schema}"."{name}"'


def _ddl_steps(schema: str) -> List[Tuple[str, str]]:
    """Returns an ordered list of (label, sql) tuples. Each step is
    idempotent — re-running is a no-op.

    Label is purely diagnostic; the schema_meta version is what gates
    whether a step has run before, but we always issue the
    `IF NOT EXISTS` form so an existing customer DB never raises.
    """
    s = schema or "public"
    steps: List[Tuple[str, str]] = []

    # ------------------------------------------------------------------
    # Schema-meta. Tracks the running version + last bootstrap time.
    # ------------------------------------------------------------------
    steps.append((
        "schema_meta",
        f"""
        CREATE TABLE IF NOT EXISTS {_table(s, "schema_meta")} (
            id INTEGER PRIMARY KEY,
            version INTEGER NOT NULL,
            last_bootstrap_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            note TEXT NULL
        )
        """,
    ))

    # ------------------------------------------------------------------
    # historian_readings. Mirrors the column shape that
    # plc_manager.py:1840-1873 already produces, so the existing PLC
    # primary-sink writer can keep using the same SQL without changes.
    # ------------------------------------------------------------------
    steps.append((
        "historian_readings",
        f"""
        CREATE TABLE IF NOT EXISTS {_table(s, "historian_readings")} (
            id BIGSERIAL PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            ts_utc TIMESTAMPTZ NOT NULL,
            gateway_id TEXT NULL,
            gateway_name TEXT NULL,
            device_name TEXT NULL,
            plc_ip TEXT NULL,
            database_name TEXT NULL,
            tag_name TEXT NOT NULL,
            value DOUBLE PRECISION NULL,
            value_text TEXT NULL,
            quality INTEGER NULL,
            quality_label TEXT NULL,
            source TEXT NULL,
            created_utc TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
    ))
    steps.append((
        "historian_readings_idx_tenant_tag_ts",
        f"""CREATE INDEX IF NOT EXISTS hr_tenant_tag_ts
            ON {_table(s, "historian_readings")} (tenant_id, tag_name, ts_utc DESC)""",
    ))
    steps.append((
        "historian_readings_idx_tenant_ts",
        f"""CREATE INDEX IF NOT EXISTS hr_tenant_ts
            ON {_table(s, "historian_readings")} (tenant_id, ts_utc DESC)""",
    ))
    steps.append((
        "historian_readings_idx_tenant_gw_ts",
        f"""CREATE INDEX IF NOT EXISTS hr_tenant_gw_ts
            ON {_table(s, "historian_readings")} (tenant_id, gateway_id, ts_utc DESC)""",
    ))

    # ------------------------------------------------------------------
    # live_latest. (tenant_id, gateway_id, tag_name) PK so the
    # ON CONFLICT … DO UPDATE upsert from sinks_sql.write_live_latest_*
    # is unambiguous.
    # ------------------------------------------------------------------
    steps.append((
        "live_latest",
        f"""
        CREATE TABLE IF NOT EXISTS {_table(s, "live_latest")} (
            tenant_id TEXT NOT NULL DEFAULT 'default',
            gateway_id TEXT NOT NULL,
            gateway_name TEXT NULL,
            device_name TEXT NULL,
            plc_ip TEXT NULL,
            database_name TEXT NULL,
            tag_name TEXT NOT NULL,
            ts_utc TIMESTAMPTZ NOT NULL,
            value DOUBLE PRECISION NULL,
            value_text TEXT NULL,
            quality INTEGER NULL,
            quality_label TEXT NULL,
            source TEXT NULL,
            updated_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (tenant_id, gateway_id, tag_name)
        )
        """,
    ))
    steps.append((
        "live_latest_idx_ts",
        f"CREATE INDEX IF NOT EXISTS ll_ts ON {_table(s, 'live_latest')} (ts_utc DESC)",
    ))
    steps.append((
        "live_latest_idx_tenant_ts",
        f"CREATE INDEX IF NOT EXISTS ll_tenant_ts ON {_table(s, 'live_latest')} (tenant_id, ts_utc DESC)",
    ))

    # ------------------------------------------------------------------
    # Aggregates (minute / hour / day). M3 ships the schema so a
    # future rollup worker writing here doesn't need a second migration.
    # ------------------------------------------------------------------
    for grain in ("minute", "hour", "day"):
        steps.append((
            f"historian_agg_{grain}",
            f"""
            CREATE TABLE IF NOT EXISTS {_table(s, f"historian_agg_{grain}")} (
                bucket_utc TIMESTAMPTZ NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                gateway_id TEXT NULL,
                gateway_name TEXT NULL,
                device_name TEXT NULL,
                plc_ip TEXT NULL,
                database_name TEXT NULL,
                tag_name TEXT NOT NULL,
                avg_value DOUBLE PRECISION NULL,
                min_value DOUBLE PRECISION NULL,
                max_value DOUBLE PRECISION NULL,
                sample_count INTEGER NOT NULL DEFAULT 0,
                quality_min INTEGER NULL,
                quality_max INTEGER NULL,
                created_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (bucket_utc, tenant_id, gateway_id, tag_name, database_name)
            )
            """,
        ))
        steps.append((
            f"historian_agg_{grain}_idx",
            f"CREATE INDEX IF NOT EXISTS hag_{grain}_bucket ON "
            f"{_table(s, f'historian_agg_{grain}')} (bucket_utc DESC)",
        ))

    # ------------------------------------------------------------------
    # config_documents — mirror of the edge's config_documents table.
    # M5 keeps this in sync with SQLite for `dashboard_configurations`,
    # `power_management_config`, `gateway_configurations`, `users_access`
    # etc.
    # ------------------------------------------------------------------
    steps.append((
        "config_documents",
        f"""
        CREATE TABLE IF NOT EXISTS {_table(s, "config_documents")} (
            domain TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            version INTEGER NOT NULL DEFAULT 1,
            payload_json JSONB NOT NULL,
            updated_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by TEXT NULL,
            PRIMARY KEY (tenant_id, domain)
        )
        """,
    ))
    steps.append((
        "config_documents_scoped",
        f"""
        CREATE TABLE IF NOT EXISTS {_table(s, "config_documents_scoped")} (
            domain TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            version INTEGER NOT NULL DEFAULT 1,
            payload_json JSONB NOT NULL,
            updated_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by TEXT NULL,
            PRIMARY KEY (tenant_id, scope_key, domain)
        )
        """,
    ))

    # ------------------------------------------------------------------
    # Users + permissions. Edge is the writer; this is the mirror Lite
    # reads from when the operator wants LAN viewers to authenticate
    # against the same credentials they use on the desktop.
    # ------------------------------------------------------------------
    steps.append((
        "users",
        f"""
        CREATE TABLE IF NOT EXISTS {_table(s, "users")} (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            username TEXT NOT NULL,
            display_name TEXT NULL,
            email TEXT NULL,
            role TEXT NOT NULL DEFAULT 'viewer',
            password_hash TEXT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_utc TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
    ))
    steps.append((
        "users_idx_tenant_username",
        f"CREATE UNIQUE INDEX IF NOT EXISTS users_tenant_username "
        f"ON {_table(s, 'users')} (tenant_id, username)",
    ))
    steps.append((
        "user_permissions",
        f"""
        CREATE TABLE IF NOT EXISTS {_table(s, "user_permissions")} (
            user_id TEXT NOT NULL,
            permission_key TEXT NOT NULL,
            granted BOOLEAN NOT NULL DEFAULT TRUE,
            updated_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, permission_key)
        )
        """,
    ))

    # ------------------------------------------------------------------
    # View-link tokens. M7 writes here so Lite can resolve a
    # `/lite/view/<token>` URL against the customer DB even when the
    # edge isn't reachable.
    # ------------------------------------------------------------------
    steps.append((
        "lite_view_links",
        f"""
        CREATE TABLE IF NOT EXISTS {_table(s, "lite_view_links")} (
            token TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            edge_id TEXT NULL,
            customer_id TEXT NULL,
            scope_key TEXT NULL,
            label TEXT NULL,
            permissions JSONB NULL,
            status TEXT NOT NULL DEFAULT 'active',
            expires_utc TIMESTAMPTZ NULL,
            created_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            revoked_utc TIMESTAMPTZ NULL,
            created_by TEXT NULL
        )
        """,
    ))
    steps.append((
        "lite_view_links_idx_status",
        f"CREATE INDEX IF NOT EXISTS lvl_status ON {_table(s, 'lite_view_links')} (status, tenant_id)",
    ))

    return steps


def bootstrap_customer_db(engine, schema: str = "public", note: str = "") -> Dict[str, Any]:
    """Create / migrate every table the edge owns in the customer DB.

    Safe to call on every boot — the work is wrapped in a single
    transaction and each statement uses ``IF NOT EXISTS``. The result
    payload reports how many steps actually executed and surfaces any
    error so the Settings UI can show "schema ready" or "schema
    failed: <reason>".
    """
    global _bootstrap_done_for_cache_key, _bootstrap_last_error
    if engine is None:
        return {"ok": False, "steps_run": 0, "error": "engine is None"}

    from sqlalchemy import text

    s = (schema or "public").strip() or "public"
    cache_key = f"{id(engine)}::{s}::{SCHEMA_VERSION}"
    with _bootstrap_lock:
        if _bootstrap_done_for_cache_key == cache_key:
            return {"ok": True, "steps_run": 0, "cached": True}

        steps = _ddl_steps(s)
        steps_run = 0
        try:
            with engine.begin() as conn:
                conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{s}"'))
                for label, sql in steps:
                    conn.execute(text(sql))
                    steps_run += 1
                conn.execute(
                    text(
                        f"""
                        INSERT INTO {_table(s, 'schema_meta')} (id, version, last_bootstrap_utc, note)
                        VALUES (1, :v, NOW(), :note)
                        ON CONFLICT (id) DO UPDATE SET
                            version = EXCLUDED.version,
                            last_bootstrap_utc = EXCLUDED.last_bootstrap_utc,
                            note = EXCLUDED.note
                        """
                    ),
                    {"v": SCHEMA_VERSION, "note": (note or "edge bootstrap")[:200]},
                )
        except Exception as exc:
            _bootstrap_last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("customer DB schema bootstrap failed: %s", _bootstrap_last_error)
            return {"ok": False, "steps_run": steps_run, "error": _bootstrap_last_error}

        _bootstrap_done_for_cache_key = cache_key
        _bootstrap_last_error = ""
        logger.info("customer DB schema bootstrap OK (%d steps, version=%d)", steps_run, SCHEMA_VERSION)
        return {"ok": True, "steps_run": steps_run, "version": SCHEMA_VERSION}


def force_rebootstrap() -> None:
    """Reset the per-engine cache. Called when the operator flips the
    mode or edits the connection target — the next bootstrap call
    re-runs the full DDL pass against the fresh engine.
    """
    global _bootstrap_done_for_cache_key
    with _bootstrap_lock:
        _bootstrap_done_for_cache_key = ""


def last_bootstrap_error() -> str:
    return _bootstrap_last_error


# ----------------------------------------------------------------------
# Shared writers
# ----------------------------------------------------------------------

def write_historian_batch(engine, rows: List[Dict[str, Any]], schema: str = "public") -> int:
    """Bulk-insert historian rows into the customer DB. Used by M4's
    parallel-Postgres sink and M5's power-meter fan-out.

    `rows` is a list of dicts with keys matching the column names in
    `historian_readings`. Missing keys land as NULL. Returns the
    number of rows written.
    """
    if not rows:
        return 0
    from sqlalchemy import text
    s = (schema or "public").strip() or "public"
    cols = (
        "tenant_id", "ts_utc", "gateway_id", "gateway_name", "device_name",
        "plc_ip", "database_name", "tag_name", "value", "value_text",
        "quality", "quality_label", "source",
    )
    placeholders = ", ".join(f":{c}" for c in cols)
    sql = text(
        f'INSERT INTO {_table(s, "historian_readings")} '
        f'({", ".join(cols)}) VALUES ({placeholders})'
    )
    payload = []
    for r in rows:
        payload.append({c: r.get(c) for c in cols})
    with engine.begin() as conn:
        conn.execute(sql, payload)
    return len(payload)


def upsert_live_latest(engine, rows: List[Dict[str, Any]], schema: str = "public") -> int:
    """Collapse a row stream to latest-per-tag and upsert into
    live_latest. The collapsing happens client-side (same as
    plc_manager.py:1956-1989) so the SQL is one batched statement.
    """
    if not rows:
        return 0
    from sqlalchemy import text

    latest: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for r in rows:
        tenant = str(r.get("tenant_id") or "default")
        gw = str(r.get("gateway_id") or "")
        tag = str(r.get("tag_name") or "")
        if not gw or not tag:
            continue
        key = (tenant, gw, tag)
        prev = latest.get(key)
        if prev is None or str(r.get("ts_utc") or "") >= str(prev.get("ts_utc") or ""):
            latest[key] = r

    if not latest:
        return 0

    s = (schema or "public").strip() or "public"
    cols = (
        "tenant_id", "gateway_id", "tag_name", "gateway_name", "device_name",
        "plc_ip", "database_name", "ts_utc", "value", "value_text",
        "quality", "quality_label", "source",
    )
    placeholders = ", ".join(f":{c}" for c in cols)
    update_assignments = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in cols if c not in ("tenant_id", "gateway_id", "tag_name")
    )
    sql = text(
        f'INSERT INTO {_table(s, "live_latest")} '
        f'({", ".join(cols)}, updated_utc) '
        f'VALUES ({placeholders}, NOW()) '
        f'ON CONFLICT (tenant_id, gateway_id, tag_name) '
        f'DO UPDATE SET {update_assignments}, updated_utc = NOW()'
    )
    payload = [
        {c: r.get(c) for c in cols}
        for r in latest.values()
    ]
    with engine.begin() as conn:
        conn.execute(sql, payload)
    return len(payload)
