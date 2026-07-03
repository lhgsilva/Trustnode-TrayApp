"""Server-side accessors for the customer's license modules + limits.

Mirrors the frontend `hasLicenseModule()` so any endpoint can check
"is this feature enabled for this customer" with one import.

The license payload shape lives in docs/LICENSE_PACKAGES.md.

Public API:
    has_module(key: str) -> bool
    get_limits() -> dict[str, int]
    get_package_key() -> str
    get_license_summary() -> dict   # for /api/health

Behavior:
    * Reads app_settings.edge_license_snapshot (set by the license
      check on every successful probe) — same cache the frontend reads.
    * Caches for 30 seconds. Cheap to call.
    * Failure modes fail-OPEN for legacy grandfathering: missing
      `modules` list => the historical grandfathered_modules set is
      used; missing `limits` block => unlimited.
    * NEVER throws — the caller can treat the return as authoritative.
"""
from __future__ import annotations

import time
from typing import Any

_CACHE_TTL_SECONDS = 30.0
_cache: dict = {
    "checked_at": 0.0,
    "modules": set(),
    "limits": {},
    "package_key": "",
    "grandfathered": set(),
}


def _normalize_module_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _read_snapshot() -> dict[str, Any]:
    """Return the app_settings dict the license check evaluates against.

    Read order:
      1. Unscoped `config_documents.app_settings` — historical truth.
      2. If that dict has no usable `license` block, scan
         `config_documents_scoped` for the most-recent `app_settings`
         row whose `license` is populated AND has a `modules` list,
         then overlay the `license` field. This rescues edges where
         the UI saved the license bundle into a scoped row (per-user
         or per-edge scope) but never mirrored it back to the
         unscoped doc. Other fields are left untouched.

    Safety:
      * Unscoped data ALWAYS wins when it carries a non-empty license.
        Only the empty/missing case falls back.
      * No new tables, no schema migration.
      * Pure read; cached upstream by `_refresh_if_stale()` so the
        scoped scan happens at most every 30 s.
    """
    try:
        from app.state import app_store
        bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
    except Exception:
        return {}
    s = bootstrap.get("app_settings")
    if not isinstance(s, dict):
        s = {}

    # Fast path: unscoped doc already carries a usable license.
    lic_unscoped = s.get("license") if isinstance(s.get("license"), dict) else None
    if isinstance(lic_unscoped, dict) and lic_unscoped.get("modules"):
        return s

    # Fallback: walk scoped app_settings rows and overlay the first
    # `license` block we find that has a non-empty modules list. We
    # also overlay `grandfathered_modules` if the scoped doc has one
    # and the unscoped doesn't, so legacy installs continue to work.
    try:
        import sqlite3 as _sqlite
        import json as _json
        with _sqlite.connect(getattr(app_store, "_db_path", ""), timeout=5.0) as _con:
            _con.row_factory = _sqlite.Row
            rows = _con.execute(
                "SELECT payload_json FROM config_documents_scoped "
                "WHERE domain='app_settings' ORDER BY updated_utc DESC"
            ).fetchall()
        for r in rows:
            try:
                payload = _json.loads(str(r["payload_json"] or "null"))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            scoped_lic = payload.get("license") if isinstance(payload.get("license"), dict) else None
            if isinstance(scoped_lic, dict) and scoped_lic.get("modules"):
                # Overlay only the license-related fields. Do NOT replace
                # the whole dict — other fields (endpoint_mode, cloud_url,
                # etc.) on the unscoped row are authoritative.
                merged = dict(s)
                merged["license"] = scoped_lic
                if not s.get("grandfathered_modules") and payload.get("grandfathered_modules"):
                    merged["grandfathered_modules"] = payload["grandfathered_modules"]
                return merged
    except Exception:
        # Best-effort fallback — never raise from a license probe.
        pass
    return s


def _evaluate() -> dict[str, Any]:
    s = _read_snapshot()
    if not s:
        return {
            "modules": set(),
            "limits": {},
            "package_key": "",
            "grandfathered": set(),
        }
    license_obj = s.get("license") if isinstance(s.get("license"), dict) else {}

    # modules list — entries can be {"key":"...","enabled":bool} or bare strings.
    modules_raw = license_obj.get("modules")
    enabled_modules: set[str] = set()
    if isinstance(modules_raw, list):
        for entry in modules_raw:
            if isinstance(entry, dict):
                key = _normalize_module_key(entry.get("key") or entry.get("module_key"))
                # If `enabled` is explicitly False, skip. Default True when absent.
                if key and entry.get("enabled", True):
                    enabled_modules.add(key)
            else:
                key = _normalize_module_key(entry)
                if key:
                    enabled_modules.add(key)

    # limits — numeric per-key. Missing/null/0 means "unlimited".
    limits_raw = license_obj.get("limits")
    limits: dict[str, int] = {}
    if isinstance(limits_raw, dict):
        for k, v in limits_raw.items():
            try:
                limits[str(k)] = int(v) if v is not None else 0
            except Exception:
                limits[str(k)] = 0

    # grandfathered (legacy installs)
    grand_raw = s.get("grandfathered_modules")
    grandfathered: set[str] = set()
    if isinstance(grand_raw, list):
        for entry in grand_raw:
            key = _normalize_module_key(entry)
            if key:
                grandfathered.add(key)

    pkg = _normalize_module_key(license_obj.get("package_key"))

    # Operator 2026-06-30: surface per-module config blobs to bolt-on
    # modules (e.g. trustnode_intelligence reading its AI endpoint URL /
    # model / token). The portal pushes these inside the signed license
    # bundle under `license.module_configs[<module_key>]`; we copy them
    # straight through. Empty dict when absent.
    raw_mc = license_obj.get("module_configs")
    module_configs: dict[str, dict[str, Any]] = {}
    if isinstance(raw_mc, dict):
        for k, v in raw_mc.items():
            if isinstance(v, dict):
                module_configs[_normalize_module_key(k)] = dict(v)

    return {
        "modules": enabled_modules,
        "limits": limits,
        "package_key": pkg or "edge",  # legacy installs report as "edge" for the banner
        "grandfathered": grandfathered,
        "module_configs": module_configs,
    }


def _refresh_if_stale() -> dict[str, Any]:
    global _cache
    now = time.monotonic()
    if now - _cache["checked_at"] >= _CACHE_TTL_SECONDS:
        try:
            v = _evaluate()
        except Exception:
            v = {"modules": set(), "limits": {}, "package_key": "", "grandfathered": set()}
        v["checked_at"] = now
        _cache = v
    return _cache


def has_module(key: str) -> bool:
    """True iff the customer's license lists this module key OR has it
    grandfathered. Empty/unknown licenses return False — same semantics
    as the frontend hasLicenseModule()."""
    k = _normalize_module_key(key)
    if not k:
        return False
    c = _refresh_if_stale()
    if k in c.get("grandfathered", set()):
        return True
    return k in c.get("modules", set())


def get_limit(key: str) -> int:
    """Returns the numeric limit for a key, or 0 = unlimited."""
    c = _refresh_if_stale()
    limits = c.get("limits") or {}
    try:
        return int(limits.get(key) or 0)
    except Exception:
        return 0


def get_limits() -> dict[str, int]:
    return dict(_refresh_if_stale().get("limits") or {})


def get_package_key() -> str:
    return str(_refresh_if_stale().get("package_key") or "")


def get_enabled_modules() -> list[str]:
    return sorted(_refresh_if_stale().get("modules", set()))


def invalidate_cache() -> None:
    """Force a re-read on the next call. Use after license-check
    completes successfully."""
    global _cache
    _cache = {"checked_at": 0.0, "modules": set(), "limits": {}, "package_key": "", "grandfathered": set()}


# ---- usage counters ---------------------------------------------------
def count_configured_tags() -> int:
    """Total tags across every gateway in the active scope. Used to
    enforce max_tags."""
    try:
        from app.state import app_store
        bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
    except Exception:
        return 0
    gateways = bootstrap.get("gateway_configurations") or []
    if not isinstance(gateways, list):
        return 0
    total = 0
    for g in gateways:
        if not isinstance(g, dict):
            continue
        if g.get("enabled") is False:
            continue
        tags = g.get("tags")
        if isinstance(tags, list):
            total += len(tags)
    return total


def count_configured_gateways() -> int:
    try:
        from app.state import app_store
        bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
    except Exception:
        return 0
    gateways = bootstrap.get("gateway_configurations") or []
    if not isinstance(gateways, list):
        return 0
    return sum(1 for g in gateways if isinstance(g, dict) and g.get("enabled") is not False)


def count_configured_admin_users() -> int:
    try:
        from app.state import app_store
        bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
    except Exception:
        return 0
    users_block = bootstrap.get("users_access") or {}
    if not isinstance(users_block, dict):
        return 0
    users = users_block.get("users")
    if not isinstance(users, list):
        return 0
    n = 0
    for u in users:
        if not isinstance(u, dict):
            continue
        role = str(u.get("role") or "").lower()
        if role in ("admin", "engineer", "super"):
            n += 1
    return n


def get_license_summary() -> dict[str, Any]:
    """Returns the dict /api/health embeds so the frontend can render
    the License Details / package banner without a second roundtrip."""
    c = _refresh_if_stale()
    # Active View-session count is sourced from the in-memory tracker
    # so the banner can show "View sessions: 2/3" live. Cheap to read.
    active_view_users = 0
    try:
        from app.services import view_sessions
        active_view_users = view_sessions.active_view_session_count()
    except Exception:
        pass
    return {
        "package_key": c.get("package_key") or "",
        "modules": sorted(c.get("modules", set())),
        "grandfathered": sorted(c.get("grandfathered", set())),
        "limits": dict(c.get("limits") or {}),
        "module_configs": dict(c.get("module_configs") or {}),
        "usage": {
            "tags": count_configured_tags(),
            "gateways": count_configured_gateways(),
            "admin_users": count_configured_admin_users(),
            "active_view_sessions": active_view_users,
        },
    }
