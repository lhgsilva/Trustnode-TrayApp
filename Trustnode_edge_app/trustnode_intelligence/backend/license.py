"""License + scope checks for TrustNode Intelligence.

Mirrors the pattern used by batch_management: a single
`require_intelligence_license()` dependency that 404s when the customer
doesn't have the module, plus helpers that read fine-grained portal
config (rate limits, feature flags, tool categories).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException, Request


MODULE_KEY = "trustnode_intelligence"


import json as _json
import sqlite3 as _sqlite3
import threading as _threading
import time as _time

# Operator 2026-07-02 (WEDGE ROOT-CAUSE FIX):
# `require_intelligence_license` is a FastAPI dependency on EVERY route in
# this module. It calls has_intelligence_module() -> _get_license_summary().
# That previously went to app.services.license_inspect.get_license_summary()
# -> app_store.get_bootstrap() -> acquires the GLOBAL app_store lock and
# scans config_documents. The historian write path holds that same lock
# while batch-inserting into a 640k+ row table. So under normal PLC load,
# EVERY intelligence request (create/list/delete/send/status) blocked on
# the lock — a burst of them exhausted the anyio thread limiter and the
# whole module wedged (and never recovered).
#
# Fix: read the license snapshot from `config_documents.app_settings` with
# a LOCK-FREE read-only SQLite connection (mode=ro + WAL => readers never
# block writers, never touch app_store._lock), cached for a few seconds.
# This is the SAME shape license_inspect builds, but without the global
# lock. Falls back to the (locking) service reader only if the direct read
# fails, so behavior is unchanged when the fast path is unavailable.
_LIC_CACHE: Dict[str, Any] = {"at": 0.0, "value": None}
_LIC_CACHE_TTL = 10.0
_LIC_LOCK = _threading.Lock()
_LIC_REFRESHING = {"busy": False}


def _read_license_snapshot_lockfree() -> Optional[Dict[str, Any]]:
    """Return {modules, grandfathered, module_configs, limits, package_key}
    read directly from app_settings.license — no app_store lock. None on
    any failure (caller falls back to the locking service reader)."""
    try:
        from app.state import app_store  # type: ignore
        db_path = getattr(app_store, "_db_path", "") or ""
        if not db_path:
            return None
        con = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
        try:
            row = con.execute(
                "SELECT payload_json FROM config_documents WHERE domain='app_settings'"
            ).fetchone()
        finally:
            con.close()
        if not row or not row[0]:
            return None
        s = _json.loads(row[0])
        if not isinstance(s, dict):
            return None
        lic = s.get("license") if isinstance(s.get("license"), dict) else {}
        modules_raw = lic.get("modules") or []
        modules = set()
        for m in modules_raw:
            if isinstance(m, dict):
                key = str(m.get("module_key") or m.get("key") or "").strip().lower()
                if key and m.get("enabled") is not False:
                    modules.add(key)
            elif isinstance(m, str):
                modules.add(m.strip().lower())
        grand = lic.get("grandfathered_modules") or s.get("grandfathered_modules") or []
        grandfathered = {str(g).strip().lower() for g in grand if g}
        mc_raw = lic.get("module_configs") or {}
        module_configs = {}
        if isinstance(mc_raw, dict):
            for k, v in mc_raw.items():
                if isinstance(v, dict):
                    module_configs[str(k).strip().lower()] = dict(v)
        return {
            "modules": sorted(modules),
            "grandfathered": sorted(grandfathered),
            "module_configs": module_configs,
            "limits": lic.get("limits") or {},
            "package_key": str(lic.get("package_key") or "edge"),
        }
    except Exception:
        return None


def _read_license_snapshot() -> Dict[str, Any]:
    """One synchronous read (lock-free direct, with a service fallback)."""
    snap = _read_license_snapshot_lockfree()
    if snap is None:
        try:
            from app.services import license_inspect  # type: ignore
            snap = license_inspect.get_license_summary() or {}
        except Exception:
            snap = {}
    return snap


def _kick_license_refresh() -> None:
    """Refresh the license cache in a daemon thread — never blocks the
    request path. At most one refresh at a time."""
    with _LIC_LOCK:
        if _LIC_REFRESHING["busy"]:
            return
        _LIC_REFRESHING["busy"] = True

    def _worker():
        try:
            snap = _read_license_snapshot()
            with _LIC_LOCK:
                _LIC_CACHE["at"] = _time.monotonic()
                _LIC_CACHE["value"] = snap
        except Exception:
            pass
        finally:
            with _LIC_LOCK:
                _LIC_REFRESHING["busy"] = False

    try:
        _threading.Thread(target=_worker, name="tn-intel-lic-refresh", daemon=True).start()
    except Exception:
        with _LIC_LOCK:
            _LIC_REFRESHING["busy"] = False


def _get_license_summary() -> Dict[str, Any]:
    """Best-effort read of the current license, STALE-WHILE-REVALIDATE.
    The request path never blocks on disk: a cached value (even stale) is
    returned instantly and a background thread refreshes it. Only the very
    first call reads synchronously. Fails CLOSED ({}) if nothing is cached
    yet and the first read fails.
    """
    now = _time.monotonic()
    with _LIC_LOCK:
        cached = _LIC_CACHE["value"]
        fresh = cached is not None and (now - _LIC_CACHE["at"]) < _LIC_CACHE_TTL
    if cached is not None:
        if not fresh:
            _kick_license_refresh()
        return cached
    # Cold start: read once synchronously.
    snap = _read_license_snapshot()
    with _LIC_LOCK:
        _LIC_CACHE["at"] = now
        _LIC_CACHE["value"] = snap
    return snap


def has_intelligence_module() -> bool:
    """True iff the active license includes the trustnode_intelligence module."""
    # DEV bypass: local dev runs against a license snapshot that may not list
    # the AI module, so /status 404s and the menu hides. Setting
    # TRUSTNODE_DEV_LICENSE_BYPASS=1 (dev only) treats it as licensed. Never set
    # in production builds — the real license gate applies there.
    import os as _os
    if str(_os.environ.get("TRUSTNODE_DEV_LICENSE_BYPASS", "")).strip().lower() in {"1", "true", "yes", "on"}:
        return True
    summary = _get_license_summary()
    modules = summary.get("modules") or []
    grandfathered = summary.get("grandfathered") or []
    return MODULE_KEY in modules or MODULE_KEY in grandfathered


async def require_intelligence_license(request: Request) -> None:
    """FastAPI dependency. 404 when the license doesn't cover the module
    so it looks like the route doesn't exist (same shape as batch_mgmt).

    Operator 2026-07-02: made `async` so it runs directly on the event loop
    instead of the SHARED anyio threadpool. It only reads the in-memory
    stale-while-revalidate license cache (no blocking I/O after cold start),
    so this is safe — and it stops /status (and every intelligence route)
    from queueing behind other app work on the anyio pool during an AI turn.
    """
    if not has_intelligence_module():
        raise HTTPException(status_code=404, detail="Not found")


# --- Fine-grained portal-driven controls ---------------------------------

def get_module_config() -> Dict[str, Any]:
    """Return the portal-configured module config blob, or {} if missing.

    Expected shape from the portal license bundle:
        {
          "endpoint_url": "https://ai.trustnode.lsapps.app",
          "model": "qwen2.5:7b-instruct",
          "auth_token": "...",
          "rate_limits": {"queries_per_day": 500, "max_tokens_per_query": 2048},
          "features": {"insights": true, "email_schedule": true},
          "allowed_tools": ["read_only"]   // or ["read_only","can_run_batches"]
        }
    """
    summary = _get_license_summary()
    module_configs = summary.get("module_configs") or summary.get("modules_config") or {}
    return dict(module_configs.get(MODULE_KEY) or {})


def get_feature_flag(flag: str, default: bool = False) -> bool:
    cfg = get_module_config()
    features = cfg.get("features") or {}
    val = features.get(flag, default)
    return bool(val)


def get_rate_limit(key: str, default: int) -> int:
    cfg = get_module_config()
    limits = cfg.get("rate_limits") or {}
    try:
        return int(limits.get(key, default))
    except Exception:
        return default


def is_tool_allowed(tool_category: str) -> bool:
    cfg = get_module_config()
    allowed = cfg.get("allowed_tools")
    if not allowed:
        # No restrictions set → allow read_only by default, deny anything
        # destructive. The portal must explicitly opt-in to write tools.
        return tool_category == "read_only"
    return tool_category in allowed
