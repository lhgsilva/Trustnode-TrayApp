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

from app.state import app_store, auth_store, control_plane_store


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



# ---------------------------------------------------------------------------
# What a workspace may NOT carry between machines
# ---------------------------------------------------------------------------
# 2026-10-30. An operator wiped a machine, reinstalled, activated it as a new
# customer, and then imported the OLD machine's workspace. The import wrote
# back app_settings verbatim - including edge_profile - plus the previous
# licence receipt and every user's old tenant_id, and re-mirrored that
# activation into the Windows registry. The fresh edge ended up:
#
#   * writing config under a scope built from the OLD customer and edge id
#     (tenant-cust-NEW | cust-OLD | edge-OLD), split from its own,
#   * showing the OLD tenant's licensed modules,
#   * reporting "pushed to the cloud" with no cloud configured,
#   * and running an OPC UA outbound connection it does not license - which
#     is what then deadlocked and dropped collection from 1 s to 8 s.
#
# None of that is the customer's content. It is the old machine's IDENTITY,
# and identity must never travel. A workspace carries what the operator
# BUILT: gateways, dashboards, reports, alarms, rules.
#
# Stripped on BOTH export and import. Import matters most - files exported by
# older builds are already out there, and this is the only place that can
# protect the machine receiving one.
_IDENTITY_KEYS = (
    "edge_profile",          # edge_id, linked_customer_id/tenant/licence
    "edge_id", "customer_id", "tenant_id", "linked_customer_id",
    "linked_tenant", "linked_license_id", "license", "license_key",
    "activation", "activation_code",
)
#: Infrastructure that belongs to the machine it was configured on. A cloud
#: URL or an outbound service from somewhere else is at best wrong and at
#: worst - as here - actively harmful.
_INFRASTRUCTURE_KEYS = (
    "cloud_url", "cloud_api_url", "cloud_auto_sync_enabled",
    "connections",           # OPC UA / MQTT outbound services
    "lan_sharing_enabled", "lan_bind_host",
)


def _strip_machine_identity(domains: dict) -> tuple[dict, list[str]]:
    """Remove identity + infrastructure from a workspace payload.

    Returns (clean_domains, what_was_removed). Never raises: a workspace that
    cannot be sanitised is one we refuse to apply, not one we apply blindly.
    """
    removed: list[str] = []
    out = dict(domains or {})
    settings = out.get("app_settings")
    if isinstance(settings, dict):
        clean = dict(settings)
        for k in _IDENTITY_KEYS + _INFRASTRUCTURE_KEYS:
            if k in clean:
                clean.pop(k, None)
                removed.append("app_settings.%s" % k)
        out["app_settings"] = clean
    return out, removed


def _current_edge_tenant() -> str:
    """This machine's own tenant, which an import may never change."""
    try:
        from app.tenant import get_current_tenant
        return str(get_current_tenant() or "default")
    except Exception:
        return "default"


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
    # 2026-10-30: the activation receipt is NOT exported any more. It is this
    # machine's licence, bound to its tenant, customer and edge id; carrying
    # it to another machine is how a fresh install ends up showing another
    # tenant's modules. A new edge activates with its own code - that is a
    # 30-second step, and it is correct.
    license_snapshot: Dict[str, Any] = {}

    domains, stripped = _strip_machine_identity(domains)
    # Operator 2026-06-18: AuthStore snapshot. Includes every user row
    # plus the JWT signing secret — restoring an export onto a fresh
    # machine gives the customer the same login experience instantly.
    auth_snapshot: Dict[str, Any] = {"users": [], "secret": ""}
    try:
        # Users travel; their TENANT does not. On the importing machine they
        # are mapped to that edge's tenant, so a restored operator can log in
        # and immediately sees that edge's data instead of an empty scope.
        auth_snapshot["users"] = [
            {k: v for k, v in (u or {}).items() if k != "tenant_id"}
            for u in (auth_store.list_users() or [])
        ]
        auth_snapshot["secret"] = auth_store.get_or_create_secret()
    except Exception:
        pass
    return {
        "format": "trustnode.workspace",
        "format_version": WORKSPACE_FORMAT_VERSION,
        "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        "exported_by": actor,
        "domains": domains,
        "license": license_snapshot,
        "auth": auth_snapshot,
        # Informational only - never applied on import. It lets an operator
        # see where a file came from before trusting it.
        "source": {"tenant_id": _current_edge_tenant()},
        "stripped": stripped,
    }


class WorkspaceImportRequest(BaseModel):
    format: str = "trustnode.workspace"
    format_version: int = WORKSPACE_FORMAT_VERSION
    domains: Dict[str, Any] = {}
    license: Dict[str, Any] = {}
    auth: Dict[str, Any] = {}
    # Allow callers to opt out of overwriting specific high-risk domains
    # (license, users_access, auth). Defaults to importing everything.
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
    # Sanitise BEFORE anything is written. Files exported by older builds
    # carry identity and infrastructure, and this is the only place that can
    # protect the machine receiving one. Self-healing, not refusing: the
    # operator gets their gateways, dashboards and reports, and this edge
    # keeps its own identity, licence and services.
    safe_domains, stripped = _strip_machine_identity(payload.domains or {})
    here = _current_edge_tenant()
    there = ""
    try:
        there = str((payload.domains or {}).get("app_settings", {}).get("tenant_id") or "")
    except Exception:
        there = ""
    foreign_tenant = bool(there) and there.strip().lower() != here.strip().lower()

    applied: list[str] = []
    skipped: list[str] = []
    failed: Dict[str, str] = {}
    for domain, value in safe_domains.items():
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
    # 2026-10-30: an activation receipt is NEVER applied from a file. It
    # belongs to the machine that earned it - importing one gave a freshly
    # activated edge the PREVIOUS tenant's modules, and re-mirrored that old
    # activation into the registry so a later wipe would restore it again.
    # This edge keeps the licence it activated with.
    license_applied = False
    if False and payload.license and "license" not in skip:
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
    # Operator 2026-06-18: AuthStore restore. Brings users + the JWT
    # secret back so logins on the freshly-imported machine work
    # immediately. Skip when caller explicitly opted out.
    auth_users_applied = 0
    if isinstance(payload.auth, dict) and "auth" not in skip:
        try:
            for u in (payload.auth.get("users") or []):
                if not isinstance(u, dict): continue
                if not str(u.get("username") or "").strip(): continue
                try:
                    auth_store.upsert_user(
                        username=str(u.get("username")),
                        password_hash=str(u.get("password_hash") or ""),
                        role=str(u.get("role") or "viewer"),
                        permissions=u.get("permissions") if isinstance(u.get("permissions"), dict) else None,
                        modules=u.get("modules") if isinstance(u.get("modules"), list) else None,
                        # Always THIS edge's tenant. A user carrying the old
                        # machine's tenant reads and writes a scope this edge
                        # does not use - which is how dashboards "disappear"
                        # after switching user.
                        tenant_id=here,
                        status=str(u.get("status") or "active"),
                        must_change_password=bool(u.get("must_change_password")),
                    )
                    auth_users_applied += 1
                except Exception:
                    pass
            # JWT secret restore is intentionally guarded: we only adopt
            # the imported secret when AuthStore had none, otherwise we
            # would invalidate every active session on the current
            # machine.
            imported_secret = str(payload.auth.get("secret") or "").strip()
            if imported_secret:
                try:
                    current = auth_store.get_or_create_secret()
                    if not current:
                        # Direct write only allowed via internal helper.
                        with auth_store._write_lock:  # type: ignore[attr-defined]
                            with auth_store._connect() as _con:  # type: ignore[attr-defined]
                                _con.execute(
                                    "INSERT OR REPLACE INTO auth_settings(id, secret, updated_utc) VALUES(1, ?, ?)",
                                    (imported_secret, datetime.now(timezone.utc).isoformat()),
                                )
                                _con.commit()
                except Exception:
                    pass
        except Exception as exc:
            failed["__auth__"] = f"{type(exc).__name__}: {exc}"
    return {
        "ok": True,
        "stripped": stripped,
        "tenant_here": here,
        "tenant_in_file": there,
        "foreign_tenant": foreign_tenant,
        "note": ("The file came from a different tenant. Its configuration was "
                 "imported; the identity, licence and services of THIS edge were "
                 "kept." if foreign_tenant else ""),
        "applied": applied,
        "skipped": skipped,
        "auth_users_applied": auth_users_applied,
        "failed": failed,
        "license_applied": license_applied,
    }
