"""Customer SQL connectivity (operator 2026-06-17).

This module is the single source of truth for the edge's connection to
the operator-supplied "customer database" — the LAN-shared Postgres
(or, behind a feature flag later, MS SQL) where every customer-scoped
artefact (configs, users, permissions, historian, live_latest,
view-links) is persisted in `customer_sql` mode.

The default mode is `local_sqlite` — meaning the existing SQLite at
`~/.trustnode_edge/data/trustnode_app_store.db` is canonical and the
customer-sql plumbing is dormant. When the operator switches to
`customer_sql` mode via Settings → Database, this module:

  * Reads connection params from `app_settings.customer_sql_target`.
  * Lazily creates a SQLAlchemy engine with a small connection pool.
  * Surfaces simple `test_connection()` and `get_engine()` helpers
    that the schema bootstrap (M3) and the parallel-Postgres sink
    fan-out (M4) will use.

Intentionally NO schema work in this module — that lives in M3 under
`sinks_sql.py`. M2 is just the persistence + connectivity surface so
the operator can configure the target and we can verify reachability
before flipping the mode.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Module-level cache. Recreated when the operator edits the target.
_engine_lock = threading.Lock()
_engine: Optional[Any] = None
_engine_cache_key: str = ""
_engine_last_error: str = ""
_engine_last_error_utc: float = 0.0


SUPPORTED_ENGINES = ("postgresql",)
# MSSQL goes here in a later round behind a feature flag.


def _build_url(target: Dict[str, Any]) -> str:
    """Construct a SQLAlchemy URL from the operator-provided fields.

    Only emits a URL for engines this module supports right now.
    Other engines raise — the route layer reports the error verbatim.
    """
    engine_kind = str(target.get("engine") or "").strip().lower()
    if engine_kind not in SUPPORTED_ENGINES:
        raise ValueError(
            f"Unsupported customer DB engine '{engine_kind}'. "
            f"Supported: {', '.join(SUPPORTED_ENGINES)}."
        )
    host = str(target.get("host") or "").strip()
    if not host:
        raise ValueError("customer DB host is required")
    port = int(target.get("port") or 5432)
    database = str(target.get("database") or "").strip()
    if not database:
        raise ValueError("customer DB database name is required")
    username = str(target.get("username") or "").strip()
    if not username:
        raise ValueError("customer DB username is required")
    password = str(target.get("password") or "")
    # psycopg3 driver — same one the existing PLC sink uses for postgres
    # in plc_manager.py, so we don't grow a new wheel dependency.
    from urllib.parse import quote_plus
    pw_enc = quote_plus(password)
    user_enc = quote_plus(username)
    return f"postgresql+psycopg://{user_enc}:{pw_enc}@{host}:{port}/{database}"


def _cache_key(target: Dict[str, Any]) -> str:
    """Stable identity for the (engine, host, port, db, user) tuple.

    Password isn't part of the key because rotating it should swap the
    cached engine without flushing for an unrelated rename. We do
    include the database name so a typo doesn't silently re-use an
    engine pointed at the wrong DB.
    """
    return "|".join([
        str(target.get("engine") or ""),
        str(target.get("host") or ""),
        str(target.get("port") or ""),
        str(target.get("database") or ""),
        str(target.get("username") or ""),
        str(target.get("schema") or ""),
        "tls=1" if target.get("tls") else "tls=0",
    ]).lower()


def get_engine(target: Dict[str, Any]) -> Tuple[Any, str]:
    """Return (engine, error). On success error is "". On failure the
    engine is None and error carries a short human-readable reason.

    Engines are cached across calls so the same poll loop doesn't
    bounce the pool. Cache invalidates automatically when any of
    (engine, host, port, database, username, schema, tls) changes.
    """
    global _engine, _engine_cache_key, _engine_last_error, _engine_last_error_utc

    try:
        url = _build_url(target)
    except ValueError as exc:
        return None, str(exc)

    key = _cache_key(target)
    with _engine_lock:
        if _engine is not None and _engine_cache_key == key:
            return _engine, ""
        # New / changed target — dispose the old engine and build fresh.
        if _engine is not None:
            try:
                _engine.dispose()
            except Exception:
                pass
            _engine = None
        try:
            from sqlalchemy import create_engine
            new_engine = create_engine(
                url,
                # Modest pool: edge writers are bursty (one batch per
                # poll), Lite readers are 1-2 Hz. 5+10 is plenty.
                pool_size=5,
                max_overflow=10,
                pool_pre_ping=True,
                pool_recycle=300,
                connect_args={"connect_timeout": 5},
            )
        except Exception as exc:
            _engine_last_error = f"engine create failed: {type(exc).__name__}: {exc}"
            _engine_last_error_utc = time.time()
            return None, _engine_last_error
        _engine = new_engine
        _engine_cache_key = key
        _engine_last_error = ""
        return _engine, ""


def test_connection(target: Dict[str, Any], timeout_s: float = 5.0) -> Dict[str, Any]:
    """Run a single SELECT 1 and surface (ok, latency_ms, error).

    The Settings UI calls this before letting the operator commit a
    mode switch — we want a clear go/no-go signal, not a partial
    success. Logs are squelched here because the route returns the
    raw payload to the UI.
    """
    t0 = time.monotonic()
    engine, err = get_engine(target)
    if engine is None:
        return {"ok": False, "latency_ms": int((time.monotonic() - t0) * 1000), "error": err}
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            res = conn.execute(text("SELECT 1")).scalar()
            ok = (res == 1)
    except Exception as exc:
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "error": f"connect failed: {type(exc).__name__}: {exc}",
        }
    return {"ok": bool(ok), "latency_ms": int((time.monotonic() - t0) * 1000), "error": ""}


def reset_engine() -> None:
    """Drop the cached engine. Called by the Settings route when the
    operator edits the target — even when the key would match — so a
    password-only change still re-tests cleanly.
    """
    global _engine, _engine_cache_key
    with _engine_lock:
        if _engine is not None:
            try:
                _engine.dispose()
            except Exception:
                pass
        _engine = None
        _engine_cache_key = ""
