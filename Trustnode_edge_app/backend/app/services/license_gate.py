"""License state cache + write gate (operator 2026-06-18, Phase 3b).

The frontend already locks the UI when a license is expired AND no
active trial covers it. This module gives the BACKEND the same view
so it refuses historian writes from a tampered or expired install.

The check is cached for 60 seconds because every historian batch
would otherwise re-query the SQLite license + trial tables.

Public API:
    is_data_writes_allowed() -> tuple[bool, str]
        Returns (True, "ok") when writes should proceed.
        Returns (False, reason) when blocked. Reason is short-form
        so the caller can stuff it into the response body.

Behavior:
    * No license rows yet (fresh install) → ALLOW. The activation
      flow handles initial licensing; we don't lock empty installs.
    * License signature missing (legacy build) → ALLOW.
    * License signature INVALID → BLOCK (tampered).
    * License expired AND no active trial → BLOCK.
    * License grandfathered_modules set → still subject to expiry
      check; grandfathering only covers MISSING module keys, not
      hard expiry.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional, Tuple

from app.state import app_store, control_plane_store

_CACHE_TTL_SECONDS = 60
_cache: dict = {"checked_at": 0.0, "allowed": True, "reason": "ok"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_expired(end_utc: str) -> bool:
    if not end_utc:
        return False
    try:
        end = datetime.fromisoformat(str(end_utc).replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return end < datetime.now(timezone.utc)
    except Exception:
        return False


def _evaluate() -> Tuple[bool, str]:
    try:
        bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
    except Exception:
        return True, "ok"  # store unreachable — don't deadlock writes
    s = bootstrap.get("app_settings") or {}
    if not isinstance(s, dict):
        return True, "ok"
    tenant_id = str(s.get("tenant_id") or "default")
    edge_id = str(s.get("edge_id") or "").strip()
    license_id = str(s.get("license_id") or "").strip()

    # Fresh install — no edge linked, nothing to gate.
    if not edge_id or not license_id:
        return True, "ok"

    # Resolve the license row from the control-plane store.
    try:
        rows = control_plane_store.list_licenses(tenant_id=tenant_id) or []
    except Exception:
        return True, "ok"
    license_row = next((r for r in rows if str(r.get("license_id") or "") == license_id), None)
    if not license_row:
        return True, "ok"  # the activation flow will surface a clearer error

    # Expiry check.
    end_utc = str(license_row.get("end_utc") or license_row.get("expires_utc") or "")
    if end_utc and _is_expired(end_utc):
        # An active trial grant overrides expiry until trial's own expiry.
        try:
            grants = control_plane_store.list_trial_grants(
                tenant_id=tenant_id, license_id=license_id, edge_id=edge_id,
            ) or []
        except Exception:
            grants = []
        active_trial = False
        for g in grants:
            tg_end = str(g.get("expires_utc") or "")
            if tg_end and not _is_expired(tg_end):
                active_trial = True
                break
        if not active_trial:
            return False, "license_expired"

    # Signature tamper check — only enforced when a signature is present.
    sig_status = s.get("license_signature_status")
    if isinstance(sig_status, dict):
        if sig_status.get("signature_present") and not sig_status.get("verified"):
            return False, "license_tampered"

    return True, "ok"


def is_data_writes_allowed(force_refresh: bool = False) -> Tuple[bool, str]:
    """Returns (allowed, reason). Cached for 60 seconds unless
    force_refresh=True. Callers should NOT rate-limit themselves —
    this function is cheap enough to call on every batch.
    """
    global _cache
    now = time.monotonic()
    if not force_refresh and (now - _cache["checked_at"]) < _CACHE_TTL_SECONDS:
        return _cache["allowed"], _cache["reason"]
    try:
        allowed, reason = _evaluate()
    except Exception as exc:
        # On any internal failure, ALLOW. We never want a license-check
        # bug to silently corrupt the customer's data acquisition.
        allowed, reason = True, f"check_failed:{type(exc).__name__}"
    _cache = {"checked_at": now, "allowed": allowed, "reason": reason}
    return allowed, reason
