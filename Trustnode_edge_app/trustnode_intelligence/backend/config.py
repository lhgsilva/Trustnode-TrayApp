"""Resolve the AI endpoint config for the running edge.

Precedence (first hit wins):
  1. Environment variables (operator override — for local dev / testing)
       TRUSTNODE_AI_ENDPOINT_URL
       TRUSTNODE_AI_MODEL
       TRUSTNODE_AI_AUTH_TOKEN
  2. Local `app_settings.ai_endpoint_config` — mirrored from the VPS
     portal by the ONE-shot pull on Edge boot (see refresh.py). This is
     the authoritative source for the running Edge.
  3. Portal-pushed license bundle (legacy path, kept as a fallback for
     older Edges that still receive the config inside the signed license).
  4. Empty defaults — the module loads but every chat call returns
     "AI endpoint not configured" until the portal pushes a real URL.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict

from . import license as _license


@dataclass
class AIConfig:
    endpoint_url: str = ""
    model: str = ""
    auth_token: str = ""

    @property
    def is_configured(self) -> bool:
        return bool(self.endpoint_url.strip())


import threading
import time as _time

# Operator 2026-07-02: cache the AI config read. get_ai_config() is called
# on EVERY /status request (which the UI polls), and each call did a full
# app_store.get_bootstrap() — which grabs the global app_store lock and
# reads the entire config_documents table. Under normal main-backend write
# pressure that lock is contended, so status calls queued and the whole
# module wedged. A 15s in-memory cache eliminates the per-request lock hit.
# The config only changes on the boot refresh (or an admin portal save),
# so 15s staleness is invisible. invalidate_config_cache() clears it after
# the boot refresh persists a new config.
_CONFIG_CACHE: Dict[str, Any] = {"at": 0.0, "value": None}
_CONFIG_CACHE_TTL = 15.0
_CONFIG_LOCK = threading.Lock()
_CONFIG_REFRESHING = {"busy": False}


def invalidate_config_cache() -> None:
    with _CONFIG_LOCK:
        _CONFIG_CACHE["at"] = 0.0
        _CONFIG_CACHE["value"] = None


def _read_local_ai_endpoint_config() -> Dict[str, Any]:
    """Pull `app_settings.ai_endpoint_config` from the Edge app_store, cached
    for a few seconds. Returns {} on any failure so the caller falls through.

    Operator 2026-07-02 (ROOT-CAUSE FIX): this used to call
    app_store.get_bootstrap(), which acquires the GLOBAL app_store lock
    (self._lock) and scans the entire config_documents table. Because
    get_ai_config() runs on EVERY /status request (polled by the UI), and
    the main backend holds that same lock during historian flushes /
    cloud-sync HTTP pushes, the status handler's threadpool thread would
    block waiting on the lock. Enough of those and the shared anyio
    threadpool drained — freezing /api/health and every other sync route.
    The whole backend wedged.

    Fix: read the single `app_settings` row directly with a LOCK-FREE
    read-only SQLite connection (mode=ro + WAL means readers never block
    writers and never touch app_store._lock). One indexed SELECT, no
    global lock, no full-table scan.
    """
    now = _time.monotonic()
    with _CONFIG_LOCK:
        cached = _CONFIG_CACHE["value"]
        fresh = cached is not None and (now - _CONFIG_CACHE["at"]) < _CONFIG_CACHE_TTL
    # Operator 2026-07-02 (STALE-WHILE-REVALIDATE): the request path must
    # NEVER do blocking sqlite I/O. The app_store.db can have a large WAL
    # under active historian writes; a read during a checkpoint can stall
    # for seconds. If many status polls hit an expired cache at once, they
    # ALL block on that read and drain the DB pool → wedge. So: if we have a
    # cached value, return it IMMEDIATELY (even if stale) and kick a ONE-shot
    # background refresh. Only the very first call (no cache yet) reads
    # synchronously. After that, reads are always instant memory hits.
    if cached is not None:
        if not fresh:
            _kick_background_refresh()
        return dict(cached)
    # Cold start: no cache yet — do a bounded synchronous read once.
    value = _read_config_from_disk()
    with _CONFIG_LOCK:
        _CONFIG_CACHE["at"] = now
        _CONFIG_CACHE["value"] = value
    return dict(value)


def _read_config_from_disk() -> Dict[str, Any]:
    import json as _json
    import sqlite3 as _sqlite3
    value: Dict[str, Any] = {}
    try:
        from app.state import app_store  # type: ignore
        db_path = getattr(app_store, "_db_path", "") or ""
        if db_path:
            con = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
            try:
                row = con.execute(
                    "SELECT payload_json FROM config_documents WHERE domain='app_settings'"
                ).fetchone()
            finally:
                con.close()
            if row and row[0]:
                s = _json.loads(row[0])
                if isinstance(s, dict):
                    cfg = s.get("ai_endpoint_config")
                    if isinstance(cfg, dict):
                        value = dict(cfg)
    except Exception:
        value = {}
    return value


def _kick_background_refresh() -> None:
    """Refresh the config cache in a daemon thread so the request path never
    blocks on disk. At most one refresh runs at a time."""
    with _CONFIG_LOCK:
        if _CONFIG_REFRESHING["busy"]:
            return
        _CONFIG_REFRESHING["busy"] = True

    def _worker():
        try:
            v = _read_config_from_disk()
            with _CONFIG_LOCK:
                _CONFIG_CACHE["at"] = _time.monotonic()
                _CONFIG_CACHE["value"] = v
        except Exception:
            pass
        finally:
            with _CONFIG_LOCK:
                _CONFIG_REFRESHING["busy"] = False

    try:
        threading.Thread(target=_worker, name="tn-intel-config-refresh", daemon=True).start()
    except Exception:
        with _CONFIG_LOCK:
            _CONFIG_REFRESHING["busy"] = False


def get_ai_config() -> AIConfig:
    cfg = AIConfig()

    # 1. Env overrides (operator / dev)
    env_url = os.environ.get("TRUSTNODE_AI_ENDPOINT_URL", "").strip()
    env_model = os.environ.get("TRUSTNODE_AI_MODEL", "").strip()
    env_token = os.environ.get("TRUSTNODE_AI_AUTH_TOKEN", "").strip()
    if env_url:
        cfg.endpoint_url = env_url
        cfg.model = env_model or "qwen2.5:7b-instruct"
        cfg.auth_token = env_token
        return cfg

    # 2. Local mirror of the portal AI Endpoint config (populated on Edge
    # boot by the one-shot pull — see trustnode_intelligence/backend/refresh.py).
    local = _read_local_ai_endpoint_config()
    local_url = str(local.get("endpoint_url") or "").strip()
    if local_url:
        cfg.endpoint_url = local_url
        cfg.model = str(local.get("model") or "").strip()
        cfg.auth_token = str(local.get("auth_token") or "").strip()
        return cfg

    # 3. Legacy: portal-pushed license bundle. Older Edges receive the
    # config inside the signed license.module_configs. Still supported.
    portal = _license.get_module_config()
    cfg.endpoint_url = str(portal.get("endpoint_url") or "").strip()
    cfg.model = str(portal.get("model") or "").strip()
    cfg.auth_token = str(portal.get("auth_token") or "").strip()
    return cfg
