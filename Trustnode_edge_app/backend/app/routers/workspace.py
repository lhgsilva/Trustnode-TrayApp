"""Workspace export / import (operator 2026-06-18).

Why this exists
---------------
Customers were afraid to update the EXE because every update appeared to
risk losing their dashboards, users, gateway configs and license. The
data is in fact preserved at the file level (SQLite at
%ProgramData%\\TrustNode\\edge\\) — but operators understandably want a
belt-and-braces backup they can hand to anyone.

Endpoints
---------
  * GET  /api/workspace/export
      Returns a single JSON blob with every configuration domain plus
      the active license activation receipt. Save this file before any
      update.

  * POST /api/workspace/import
      Accepts the JSON blob and writes each domain back. Atomic at the
      domain level — a partial import leaves untouched domains alone.

Both endpoints require an authenticated admin user (enforced upstream
by the PUBLIC_PATHS guard in main.py — workspace/* is NOT public).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.state import app_store, control_plane_store


router = APIRouter(prefix="/api/workspace", tags=["workspace"])


WORKSPACE_FORMAT_VERSION = 1


def _require_admin(request: Request) -> str:
    """Lightweight admin gate. The auth middleware in main.py sets
    request.state.user_payload from the JWT. We re-read role and refuse
    non-admins. PUBLIC_PATHS does NOT include /api/workspace/*, so the
    middleware has already rejected unauthenticated calls before we
    reach this point.
    """
    payload = getattr(request.state, "user_payload", None) or {}
    role = str(payload.get("role") or "").strip().lower()
    username = str(payload.get("sub") or payload.get("username") or "admin")
    if role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    return username


@router.get("/export")
def export_workspace(request: Request) -> Dict[str, Any]:
    actor = _require_admin(request)
    domains: Dict[str, Any] = {}
    for domain in app_store.REQUIRED_CONFIG_DOMAINS.keys():
        try:
            domains[domain] = app_store.get_config_domain(domain, None)
        except Exception:
            domains[domain] = None
    # License activation snapshot — the receipt the customer's edge
    # received from the control plane. Re-applying this on a fresh
    # install skips the activation-code round trip.
    license_snapshot: Dict[str, Any] = {}
    try:
        license_snapshot = control_plane_store.export_activation_state() or {}
    except Exception:
        license_snapshot = {}
    return {
        "format": "trustnode.workspace",
        "format_version": WORKSPACE_FORMAT_VERSION,
        "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        "exported_by": actor,
        "domains": domains,
        "license": license_snapshot,
    }


class WorkspaceImportRequest(BaseModel):
    format: str = "trustnode.workspace"
    format_version: int = WORKSPACE_FORMAT_VERSION
    domains: Dict[str, Any] = {}
    license: Dict[str, Any] = {}
    # Allow callers to opt out of overwriting specific high-risk domains
    # (license, users_access). Defaults to importing everything.
    skip_domains: list[str] = []


@router.post("/import")
def import_workspace(payload: WorkspaceImportRequest, request: Request) -> Dict[str, Any]:
    actor = _require_admin(request)
    if str(payload.format or "").strip() != "trustnode.workspace":
        raise HTTPException(status_code=400, detail="not a trustnode workspace file")
    if int(payload.format_version or 0) > WORKSPACE_FORMAT_VERSION:
        raise HTTPException(
            status_code=400,
            detail=f"workspace was exported from a newer version (v{payload.format_version}); update this edge first",
        )
    skip = {str(s).strip().lower() for s in (payload.skip_domains or [])}
    applied: list[str] = []
    skipped: list[str] = []
    failed: Dict[str, str] = {}
    for domain, value in (payload.domains or {}).items():
        if not isinstance(domain, str) or not domain.strip():
            continue
        if domain.strip().lower() in skip:
            skipped.append(domain)
            continue
        if value is None:
            skipped.append(domain)
            continue
        try:
            app_store.upsert_domain(domain, value, actor=f"workspace_import:{actor}")
            applied.append(domain)
        except Exception as exc:
            failed[domain] = f"{type(exc).__name__}: {exc}"
    license_applied = False
    if payload.license and "license" not in skip:
        try:
            control_plane_store.import_activation_state(payload.license)
            license_applied = True
            # Re-mirror the freshly-imported activation into the Windows
            # registry so a future "wipe + reinstall" auto-restores from
            # the registry without needing the JSON file again.
            try:
                control_plane_store.mirror_activation_to_registry()
            except Exception:
                pass
        except Exception as exc:
            failed["__license__"] = f"{type(exc).__name__}: {exc}"
    return {
        "ok": True,
        "applied": applied,
        "skipped": skipped,
        "failed": failed,
        "license_applied": license_applied,
    }
