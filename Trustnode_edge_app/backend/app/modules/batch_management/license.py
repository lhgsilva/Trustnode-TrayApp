"""License gate for the Batch Management module.

Returns True only if the customer's edge license carries the
`batch_management` module key (or grandfathered it). Mirrors the
frontend `hasLicenseModule("batch_management")` semantics so the
backend and the UI agree on what is gated.

Single source of truth:
  app_store.get_bootstrap()["app_settings"] keeps a snapshot of the
  license payload (license.modules: [{key,...}], grandfathered_modules,
  signature status). We read that snapshot rather than re-querying the
  control-plane store on every request — same pattern as license_gate.
"""
from __future__ import annotations

import time
from typing import Any, Tuple

from fastapi import HTTPException, status

MODULE_KEY = "batch_management"

_CACHE_TTL_SECONDS = 30.0
_cache: dict = {"checked_at": 0.0, "enabled": False, "reason": "init"}


def _read_license_snapshot() -> dict[str, Any]:
    try:
        from app.state import app_store  # local import — circular safety
        bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
    except Exception:
        return {}
    settings = bootstrap.get("app_settings")
    if not isinstance(settings, dict):
        return {}
    return settings


def _normalize_module_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _evaluate() -> Tuple[bool, str]:
    s = _read_license_snapshot()
    if not s:
        # No bootstrap row yet (fresh install). Mirror frontend behavior:
        # show nothing for new modules until a license arrives.
        return False, "no_license_snapshot"

    # Tampered license: blocked regardless of module list. Matches frontend
    # licenseTampered logic.
    sig = s.get("license_signature_status")
    if isinstance(sig, dict) and sig.get("signature_present") and not sig.get("verified"):
        return False, "license_tampered"

    # Grandfathered modules (legacy installs).
    grand = s.get("grandfathered_modules")
    if isinstance(grand, list):
        for entry in grand:
            if _normalize_module_key(entry) == MODULE_KEY:
                return True, "grandfathered"

    # Modern license.modules list. Each entry is either {"key": "..."} or
    # a bare string depending on portal version.
    license_obj = s.get("license") if isinstance(s.get("license"), dict) else {}
    modules = license_obj.get("modules") if isinstance(license_obj.get("modules"), list) else []
    for entry in modules:
        if isinstance(entry, dict):
            key = _normalize_module_key(entry.get("key") or entry.get("module_key"))
        else:
            key = _normalize_module_key(entry)
        if key == MODULE_KEY:
            return True, "licensed"

    return False, "not_in_license"


def is_batch_management_enabled(force_refresh: bool = False) -> Tuple[bool, str]:
    """Returns (enabled, reason). Cached for 30s. Reason is short-form
    so endpoints can include it in 404/403 responses for debugging."""
    global _cache
    now = time.monotonic()
    if not force_refresh and (now - _cache["checked_at"]) < _CACHE_TTL_SECONDS:
        return _cache["enabled"], _cache["reason"]
    try:
        enabled, reason = _evaluate()
    except Exception as exc:
        enabled, reason = False, f"check_failed:{type(exc).__name__}"
    _cache = {"checked_at": now, "enabled": enabled, "reason": reason}
    return enabled, reason


def require_batch_management_license() -> None:
    """FastAPI dependency. Raises 404 (not 403) when the module is not
    licensed so the API surface itself is indistinguishable from "feature
    does not exist" — same shape as how the frontend hides the menu."""
    enabled, reason = is_batch_management_enabled()
    if not enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"module": MODULE_KEY, "reason": reason},
        )
