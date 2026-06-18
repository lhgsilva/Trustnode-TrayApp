from datetime import datetime, timezone
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
import requests

from app.state import app_store, control_plane_store, telemetry_service
from app.tenant import get_current_tenant, normalize_tenant_id

router = APIRouter(prefix="/api/control-plane", tags=["control-plane"])


def _is_same_origin_as_request(base_url: str, request: Request) -> bool:
    try:
        b = str(base_url or "").strip().rstrip("/").lower()
        if not b:
            return False
        req_origin = f"{request.url.scheme}://{str(request.headers.get('host', '')).strip()}".rstrip("/").lower()
        return b == req_origin
    except Exception:
        return False


def _resolve_cloud_control_plane_base(request: Request) -> str:
    explicit = str(os.getenv("TRUSTNODE_CONTROL_PLANE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    try:
        boot = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
        settings = boot.get("app_settings") if isinstance(boot, dict) else {}
        if isinstance(settings, dict):
            from_settings = str(settings.get("cloud_url") or settings.get("cloud_api_url") or "").strip().rstrip("/")
            if from_settings:
                return from_settings
    except Exception:
        pass
    host = str(request.headers.get("host", "")).split(":")[0].strip().lower()
    if host in {"127.0.0.1", "localhost", "::1"}:
        return "https://trustnode.lsapps.app"
    return f"{request.url.scheme}://{host}".rstrip("/")


class TenantUpsertRequest(BaseModel):
    tenant_id: str
    name: str
    status: str = "active"
    primary_domain: str = ""
    timezone: str = "UTC"
    metadata: dict[str, Any] = Field(default_factory=dict)


class CustomerUpsertRequest(BaseModel):
    customer_id: str = ""
    company_name: str
    contact_email: str = ""
    status: str = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class EdgeUpsertRequest(BaseModel):
    edge_id: str = ""
    edge_name: str
    customer_id: str = ""
    site: str = ""
    area: str = ""
    equipment: str = ""
    status: str = "inactive"
    metadata: dict[str, Any] = Field(default_factory=dict)


class LicenseUpsertRequest(BaseModel):
    license_id: str = ""
    customer_id: str = ""
    plan_code: str = "standard"
    status: str = "active"
    start_utc: str = ""
    end_utc: str = ""
    max_edges: int = 3
    max_users: int = 10
    metadata: dict[str, Any] = Field(default_factory=dict)


class LicenseModulesRequest(BaseModel):
    modules: list[dict[str, Any]] = Field(default_factory=list)


class UserUpsertRequest(BaseModel):
    customer_id: str = ""
    username: str
    password: str | None = None
    role: str = "viewer"
    status: str = "active"
    email: str = ""
    mfa_enabled: bool = False
    modules: list[str] = Field(default_factory=list)
    permissions: dict[str, Any] = Field(default_factory=dict)


class UserSetPasswordRequest(BaseModel):
    password: str
    must_change: bool = False


class UserTempPasswordRequest(BaseModel):
    """Empty body — admin clicks the button; backend rolls the password
    and returns it. Pydantic still wants a model so we keep one here."""
    length: int = 14


class ActivationCodeIssueRequest(BaseModel):
    customer_id: str
    edge_id: str
    license_id: str
    edge_name: str = ""
    ttl_minutes: int = 30
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActivationCodeApplyRequest(BaseModel):
    activation_code: str
    edge_id: str
    edge_name: str = ""
    site: str = ""
    area: str = ""
    equipment: str = ""
    admin_username: str = "admin"
    admin_password: str = ""


class ActivationCodeUpdateRequest(BaseModel):
    status: str = ""
    expires_utc: str = ""


class EdgeRegisterRequest(BaseModel):
    activation_code: str
    edge_id: str = ""
    edge_name: str = ""
    site: str = ""
    area: str = ""
    equipment: str = ""
    admin_username: str = "admin"
    admin_password: str = ""


class EdgeLocalFinalizeRequest(BaseModel):
    tenant_id: str
    edge_id: str
    edge_name: str = ""
    customer_id: str = ""
    license_id: str = ""
    license_status: str = "active"
    license_plan_code: str = "standard"
    license_start_utc: str = ""
    license_end_utc: str = ""
    license_max_edges: int = 0
    license_max_users: int = 0
    license_modules: list[dict[str, Any]] = Field(default_factory=list)
    cloud_api_url: str = ""
    primary_domain: str = ""
    admin_username: str = "admin"
    admin_password: str = ""


class PasswordResetIssueRequest(BaseModel):
    username: str
    ttl_minutes: int = 15


class PasswordResetApplyRequest(BaseModel):
    username: str
    reset_token: str
    new_password: str


class PasswordResetPublicIssueRequest(BaseModel):
    username: str
    tenant_id: str = ""
    ttl_minutes: int = 15


class PasswordResetPublicApplyRequest(BaseModel):
    username: str
    tenant_id: str = ""
    reset_token: str
    new_password: str


class CustomerBundleProvisionRequest(BaseModel):
    tenant_id: str
    tenant_name: str
    primary_domain: str
    timezone: str = "Europe/Dublin"
    customer_id: str
    company_name: str
    contact_email: str = ""
    admin_username: str = "admin"
    admin_password: str
    license_id: str = ""
    plan_code: str = "standard"
    max_edges: int = 5
    max_users: int = 25
    modules: list[dict[str, Any]] = Field(default_factory=list)


def _require_auth_payload(request: Request) -> dict[str, Any]:
    payload = getattr(request.state, "user_payload", {}) or {}
    if not payload:
        raise HTTPException(status_code=401, detail="Authentication required")
    return payload


def _is_admin(payload: dict[str, Any]) -> bool:
    return str(payload.get("role") or "").strip().lower() == "admin"


def _is_global_admin(payload: dict[str, Any]) -> bool:
    return _is_admin(payload) and normalize_tenant_id(str(payload.get("tenant_id") or "default")) == "default"


def _scoped_tenant(request: Request, tenant_id: str | None, *, require_admin_write: bool = False) -> str:
    payload = _require_auth_payload(request)
    token_tenant = normalize_tenant_id(str(payload.get("tenant_id") or get_current_tenant()))
    requested = normalize_tenant_id(tenant_id or token_tenant)
    if requested != token_tenant and not _is_global_admin(payload):
        raise HTTPException(status_code=403, detail="Cross-tenant access denied")
    if require_admin_write and not _is_admin(payload):
        raise HTTPException(status_code=403, detail="Admin role required")
    return requested


def _audit(
    request: Request,
    *,
    tenant_id: str,
    action: str,
    outcome: str,
    details: dict[str, Any] | None = None,
) -> None:
    payload = getattr(request.state, "user_payload", {}) or {}
    try:
        control_plane_store.audit(
            actor_type="user",
            actor_id=str(payload.get("sub") or "unknown"),
            tenant_id=tenant_id,
            action=action,
            outcome=outcome,
            correlation_id=request.headers.get("X-Correlation-Id", "") or request.headers.get("X-Request-Id", "") or "-",
            details=details or {},
        )
    except Exception:
        # Never let audit logging failures break primary portal/activation flows.
        return


@router.get("/modules")
def list_module_catalog(request: Request) -> dict[str, Any]:
    _require_auth_payload(request)
    return {"ok": True, "modules": control_plane_store.module_catalog()}


@router.get("/tenants")
def list_tenants(request: Request, include_suspended: bool = True) -> dict[str, Any]:
    payload = _require_auth_payload(request)
    if not _is_global_admin(payload):
        raise HTTPException(status_code=403, detail="Global admin required")
    return {"ok": True, "rows": control_plane_store.list_tenants(include_suspended=include_suspended)}


@router.post("/tenants")
def upsert_tenant(payload: TenantUpsertRequest, request: Request) -> dict[str, Any]:
    user_payload = _require_auth_payload(request)
    if not _is_global_admin(user_payload):
        raise HTTPException(status_code=403, detail="Global admin required")
    row = control_plane_store.upsert_tenant(
        tenant_id=payload.tenant_id,
        name=payload.name,
        status=payload.status,
        primary_domain=payload.primary_domain,
        timezone_name=payload.timezone,
        metadata=payload.metadata,
    )
    _audit(
        request,
        tenant_id=normalize_tenant_id(payload.tenant_id),
        action="tenant.upsert",
        outcome="ok",
        details={"tenant_id": payload.tenant_id},
    )
    return {"ok": True, "row": row}


def _master_wants_all_tenants(request: Request, tenant_id: str | None) -> bool:
    """True when the caller is the master admin (role=admin AND
    tenant_id=default in their JWT) AND they're asking for either the
    explicit `__all__` sentinel or filtering by `default`. The portal's
    Customers/Edges/Licenses pages have always sent
    `?tenant_id=default`, which used to return only master's own rows;
    after per-customer tenancy that filter hides every customer-scoped
    row, so the page came up empty. Treat it as cross-tenant for the
    master."""
    requested = str(tenant_id or "").strip().lower()
    if requested in ("__all__", "*"):
        try:
            payload = _require_auth_payload(request)
            return _is_global_admin(payload)
        except Exception:
            return False
    if requested in ("", "default"):
        try:
            payload = _require_auth_payload(request)
            return _is_global_admin(payload)
        except Exception:
            return False
    return False


@router.get("/customers")
def list_customers(request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    if _master_wants_all_tenants(request, tenant_id):
        rows = control_plane_store.list_customers(all_tenants=True)
        return {"ok": True, "tenant_id": "__all__", "rows": rows}
    tid = _scoped_tenant(request, tenant_id)
    return {"ok": True, "tenant_id": tid, "rows": control_plane_store.list_customers(tenant_id=tid)}


def _customer_tenant_id(customer_id: str) -> str:
    """Resolve the per-customer tenant slug.

    Rules (refined 2026-05-18 after seeing live data):

      1. If the customer has an EXISTING tenant_id that's NOT 'default'
         (e.g. 'customer_a', 'tenant-cust-x'), honour it. This preserves
         compatibility with the customers you created before per-customer
         tenancy was enforced ('customer_a' etc.).

      2. If the customer is on tenant_id='default' or doesn't exist yet,
         GENERATE 'tenant-<customer_id>'. 'default' is the master admin's
         own tenant; no customer-scoped resource may live on it, otherwise
         that customer's Lite users would see every other 'default' row.

      3. If customer_id itself is empty, raise — the caller (POST /customers)
         must auto-generate it BEFORE calling here, since the tenant slug
         needs the id baked in.
    """
    cid = str(customer_id or "").strip()
    if not cid:
        raise HTTPException(status_code=400, detail="customer_id is required for tenant assignment")
    # 1) Existing customer with a real per-customer tenant: honour it.
    try:
        existing_tenant = (control_plane_store.get_customer_tenant_id(customer_id=cid) or "").strip()
    except Exception:
        existing_tenant = ""
    if existing_tenant and existing_tenant.lower() != "default":
        return existing_tenant
    # 2) New customer OR customer stuck on 'default': always generate.
    return f"tenant-{cid}"


@router.post("/customers")
def upsert_customer(payload: CustomerUpsertRequest, request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    # Authorize against the *caller's* tenant (master admin's `default` for
    # cross-tenant creates), then assign the *customer's* per-customer
    # tenant. Each customer gets their own tenant — that's what makes the
    # existing Lite RLS isolate them from each other.
    _scoped_tenant(request, tenant_id, require_admin_write=True)
    # Auto-generate a customer_id when the portal sends an empty one.
    # control_plane_store.upsert_customer falls back to 'cust-<8hex>' if
    # we don't supply one, but _customer_tenant_id below needs the id
    # before the store assigns it, so we mint it here ourselves.
    if not str(payload.customer_id or "").strip():
        import secrets as _secrets
        payload.customer_id = f"cust-{_secrets.token_hex(4)}"
    assigned_tenant = _customer_tenant_id(payload.customer_id)
    # Ensure the per-customer tenant exists before inserting the customer
    # row (cp_customers.tenant_id has a foreign key to cp_tenants).
    try:
        control_plane_store.upsert_tenant(
            tenant_id=assigned_tenant,
            name=payload.company_name or payload.customer_id,
            status="active",
            primary_domain="",
            timezone_name="UTC",
            metadata={"source": "per_customer_auto", "customer_id": payload.customer_id},
        )
    except Exception as exc:
        # If tenant creation fails for any reason, surface it rather than
        # silently dropping the customer onto 'default' (which would
        # reopen the cross-tenant leak).
        raise HTTPException(status_code=500, detail=f"failed to provision per-customer tenant: {exc}") from exc
    row = control_plane_store.upsert_customer(
        tenant_id=assigned_tenant,
        customer_id=payload.customer_id,
        company_name=payload.company_name,
        contact_email=payload.contact_email,
        status=payload.status,
        metadata=payload.metadata,
    )
    _audit(
        request,
        tenant_id=assigned_tenant,
        action="customer.upsert",
        outcome="ok",
        details={"customer_id": row.get("customer_id", ""), "assigned_tenant": assigned_tenant},
    )
    return {"ok": True, "tenant_id": assigned_tenant, "row": row}


@router.delete("/customers/{customer_id}")
def delete_customer(request: Request, customer_id: str, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    try:
        deleted = control_plane_store.delete_customer(tenant_id=tid, customer_id=customer_id)
    except Exception as exc:
        _audit(request, tenant_id=tid, action="customer.delete", outcome="error", details={"customer_id": customer_id, "error": str(exc)})
        raise HTTPException(status_code=409, detail=f"customer_delete_blocked:{exc}") from exc
    if not deleted:
        _audit(request, tenant_id=tid, action="customer.delete", outcome="not_found", details={"customer_id": customer_id})
        raise HTTPException(status_code=404, detail="customer_not_found")
    _audit(request, tenant_id=tid, action="customer.delete", outcome="ok", details={"customer_id": customer_id})
    return {"ok": True, "tenant_id": tid, "customer_id": customer_id}

@router.post("/customers/{customer_id}/delete")
def delete_customer_post(request: Request, customer_id: str, tenant_id: str | None = None) -> dict[str, Any]:
    return delete_customer(request=request, customer_id=customer_id, tenant_id=tenant_id)


@router.get("/edges")
def list_edges(request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    if _master_wants_all_tenants(request, tenant_id):
        rows = control_plane_store.list_edges(all_tenants=True)
        return {"ok": True, "tenant_id": "__all__", "rows": rows}
    tid = _scoped_tenant(request, tenant_id)
    return {"ok": True, "tenant_id": tid, "rows": control_plane_store.list_edges(tenant_id=tid)}


@router.post("/edges")
def upsert_edge(payload: EdgeUpsertRequest, request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    # Authorize against the caller's tenant, then put the edge on its
    # owning customer's tenant. If no customer_id is supplied, fall back
    # to the caller's tenant (master may create unowned edges; tenant
    # users without a customer is undefined and rejected upstream).
    caller_tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    assigned_tenant = (
        _customer_tenant_id(payload.customer_id) if str(payload.customer_id or "").strip() else caller_tid
    )
    row = control_plane_store.upsert_edge(
        tenant_id=assigned_tenant,
        edge_id=payload.edge_id,
        edge_name=payload.edge_name,
        customer_id=payload.customer_id,
        site=payload.site,
        area=payload.area,
        equipment=payload.equipment,
        status=payload.status,
        metadata=payload.metadata,
    )
    _audit(
        request,
        tenant_id=assigned_tenant,
        action="edge.upsert",
        outcome="ok",
        details={"edge_id": row.get("edge_id", ""), "assigned_tenant": assigned_tenant},
    )
    return {"ok": True, "tenant_id": assigned_tenant, "row": row}


@router.delete("/edges/{edge_id}")
def delete_edge(request: Request, edge_id: str, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    deleted = control_plane_store.delete_edge(tenant_id=tid, edge_id=edge_id)
    if not deleted:
        _audit(request, tenant_id=tid, action="edge.delete", outcome="not_found", details={"edge_id": edge_id})
        raise HTTPException(status_code=404, detail="edge_not_found")
    _audit(request, tenant_id=tid, action="edge.delete", outcome="ok", details={"edge_id": edge_id})
    return {"ok": True, "tenant_id": tid, "edge_id": edge_id}

@router.post("/edges/{edge_id}/delete")
def delete_edge_post(request: Request, edge_id: str, tenant_id: str | None = None) -> dict[str, Any]:
    return delete_edge(request=request, edge_id=edge_id, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# Per-edge read-only Client View share links
# ---------------------------------------------------------------------------
#
# A "client view link" is a long random URL token that lets anyone (without
# logging in) open a read-only Lite view of a single edge. Created by master
# / portal admins so they can share live monitoring with customers, partners
# and field engineers who shouldn't get a full account.
#
# Lifecycle:
#   * `POST /edges/{edge_id}/view-link`   — create-or-return active token.
#   * `POST /edges/{edge_id}/view-link/rotate` — revoke current + mint new.
#   * `DELETE /edges/{edge_id}/view-link` — revoke (no new token).
#   * `GET /edges/{edge_id}/view-link`    — fetch active token (or null).
#   * `GET /lite-view/resolve/{token}`    — PUBLIC. Resolves a token to
#     {tenant_id, customer_id, edge_id}; the Lite app uses this to scope
#     the read-only render. No JWT/auth required.

def _new_view_link_token() -> str:
    import secrets
    return secrets.token_urlsafe(24)


def _view_link_for_edge(tenant_id: str, edge_id: str) -> dict[str, Any] | None:
    """Return the active view-link row for an edge, or None."""
    try:
        rows = getattr(control_plane_store, "list_edge_view_links", None)
        if callable(rows):
            for row in rows(tenant_id=tenant_id, edge_id=edge_id) or []:
                if str(row.get("status") or "active") == "active":
                    return row
    except Exception:
        pass
    return None


@router.get("/edges/{edge_id}/view-link")
def get_edge_view_link(request: Request, edge_id: str, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id, require_admin_write=False)
    eid = str(edge_id or "").strip()
    if not eid:
        raise HTTPException(status_code=400, detail="edge_id_required")
    row = _view_link_for_edge(tid, eid)
    return {"ok": True, "tenant_id": tid, "edge_id": eid, "link": row}


@router.post("/edges/{edge_id}/view-link")
def create_edge_view_link(request: Request, edge_id: str, tenant_id: str | None = None) -> dict[str, Any]:
    """Create a view-link token for an edge. Idempotent — if an active
    token already exists we return it instead of minting a duplicate."""
    tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    eid = str(edge_id or "").strip()
    if not eid:
        raise HTTPException(status_code=400, detail="edge_id_required")
    existing = _view_link_for_edge(tid, eid)
    if existing:
        return {"ok": True, "tenant_id": tid, "edge_id": eid, "link": existing}
    # Resolve customer_id from the edge row so the link carries the right
    # scope. The Lite read-only view scopes its queries to this tenant.
    customer_id = ""
    try:
        for e in control_plane_store.list_edges(tenant_id=tid) or []:
            if str(e.get("edge_id") or "") == eid:
                customer_id = str(e.get("customer_id") or "")
                break
    except Exception:
        customer_id = ""
    payload = getattr(request.state, "user_payload", {}) or {}
    actor = str(payload.get("sub") or "admin")
    token = _new_view_link_token()
    row: dict[str, Any] = {
        "token": token,
        "tenant_id": tid,
        "customer_id": customer_id,
        "edge_id": eid,
        "status": "active",
        "created_by": actor,
    }
    try:
        if hasattr(control_plane_store, "upsert_edge_view_link"):
            control_plane_store.upsert_edge_view_link(**row)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"view_link_create_failed: {exc}") from exc
    _audit(request, tenant_id=tid, action="edge.view_link.create", outcome="ok",
           details={"edge_id": eid, "token_prefix": token[:8]})
    return {"ok": True, "tenant_id": tid, "edge_id": eid, "link": row}


@router.post("/edges/{edge_id}/view-link/rotate")
def rotate_edge_view_link(request: Request, edge_id: str, tenant_id: str | None = None) -> dict[str, Any]:
    """Revoke the current view-link (if any) and mint a fresh token."""
    tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    eid = str(edge_id or "").strip()
    if not eid:
        raise HTTPException(status_code=400, detail="edge_id_required")
    try:
        if hasattr(control_plane_store, "revoke_edge_view_links"):
            control_plane_store.revoke_edge_view_links(tenant_id=tid, edge_id=eid)
    except Exception:
        pass
    return create_edge_view_link(request=request, edge_id=eid, tenant_id=tid)


@router.delete("/edges/{edge_id}/view-link")
def revoke_edge_view_link(request: Request, edge_id: str, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    eid = str(edge_id or "").strip()
    if not eid:
        raise HTTPException(status_code=400, detail="edge_id_required")
    revoked = 0
    try:
        if hasattr(control_plane_store, "revoke_edge_view_links"):
            revoked = int(control_plane_store.revoke_edge_view_links(tenant_id=tid, edge_id=eid) or 0)
    except Exception:
        revoked = 0
    _audit(request, tenant_id=tid, action="edge.view_link.revoke", outcome="ok",
           details={"edge_id": eid, "revoked_count": revoked})
    return {"ok": True, "tenant_id": tid, "edge_id": eid, "revoked": revoked}


# ────────────────────────────────────────────────────────────────────
# Per-USER view-links (operator 2026-06-17). Same `cp_edge_view_links`
# table, but with `user_id` set. Lets the local admin generate one
# Lite token per user from the Users page, rotate/revoke individually
# without affecting other users or the edge-wide shareable link.
# All four endpoints are admin-only (require_admin_write=True).
#   POST   /edges/{edge_id}/users/{user_id}/view-link        — mint/return
#   POST   /edges/{edge_id}/users/{user_id}/view-link/rotate — revoke + mint
#   GET    /edges/{edge_id}/users/{user_id}/view-link        — fetch active
#   DELETE /edges/{edge_id}/users/{user_id}/view-link        — revoke
# ────────────────────────────────────────────────────────────────────

def _user_view_link_for(tenant_id: str, edge_id: str, user_id: str) -> dict[str, Any] | None:
    try:
        fn = getattr(control_plane_store, "list_edge_view_links_for_user", None)
        if callable(fn):
            rows = fn(tenant_id=tenant_id, edge_id=str(edge_id), user_id=str(user_id)) or []
            for row in rows:
                if str(row.get("status") or "active") == "active":
                    return row
    except Exception:
        pass
    return None


@router.get("/edges/{edge_id}/users/{user_id}/view-link")
def get_edge_user_view_link(request: Request, edge_id: str, user_id: str,
                            tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id, require_admin_write=False)
    eid = str(edge_id or "").strip()
    uid = str(user_id or "").strip()
    if not eid or not uid:
        raise HTTPException(status_code=400, detail="edge_id_and_user_id_required")
    row = _user_view_link_for(tid, eid, uid)
    return {"ok": True, "tenant_id": tid, "edge_id": eid, "user_id": uid, "link": row}


@router.post("/edges/{edge_id}/users/{user_id}/view-link")
def create_edge_user_view_link(request: Request, edge_id: str, user_id: str,
                                tenant_id: str | None = None) -> dict[str, Any]:
    """Mint a per-user token. Idempotent — returns the active one if any."""
    tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    eid = str(edge_id or "").strip()
    uid = str(user_id or "").strip()
    if not eid or not uid:
        raise HTTPException(status_code=400, detail="edge_id_and_user_id_required")
    existing = _user_view_link_for(tid, eid, uid)
    if existing:
        return {"ok": True, "tenant_id": tid, "edge_id": eid, "user_id": uid, "link": existing}
    customer_id = ""
    try:
        for e in control_plane_store.list_edges(tenant_id=tid) or []:
            if str(e.get("edge_id") or "") == eid:
                customer_id = str(e.get("customer_id") or "")
                break
    except Exception:
        customer_id = ""
    payload = getattr(request.state, "user_payload", {}) or {}
    actor = str(payload.get("sub") or "admin")
    token = _new_view_link_token()
    try:
        if hasattr(control_plane_store, "upsert_edge_view_link"):
            control_plane_store.upsert_edge_view_link(
                token=token, tenant_id=tid, edge_id=eid,
                customer_id=customer_id, status="active",
                created_by=actor, user_id=uid,
            )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"user_view_link_create_failed: {exc}") from exc
    row = {
        "token": token, "tenant_id": tid, "customer_id": customer_id,
        "edge_id": eid, "user_id": uid, "status": "active", "created_by": actor,
    }
    _audit(request, tenant_id=tid, action="edge.user_view_link.create", outcome="ok",
           details={"edge_id": eid, "user_id": uid, "token_prefix": token[:8]})
    return {"ok": True, "tenant_id": tid, "edge_id": eid, "user_id": uid, "link": row}


@router.post("/edges/{edge_id}/users/{user_id}/view-link/rotate")
def rotate_edge_user_view_link(request: Request, edge_id: str, user_id: str,
                                tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    eid = str(edge_id or "").strip()
    uid = str(user_id or "").strip()
    if not eid or not uid:
        raise HTTPException(status_code=400, detail="edge_id_and_user_id_required")
    try:
        fn = getattr(control_plane_store, "revoke_edge_view_links_for_user", None)
        if callable(fn):
            fn(tenant_id=tid, edge_id=eid, user_id=uid)
    except Exception:
        pass
    return create_edge_user_view_link(request=request, edge_id=eid, user_id=uid, tenant_id=tid)


@router.delete("/edges/{edge_id}/users/{user_id}/view-link")
def revoke_edge_user_view_link(request: Request, edge_id: str, user_id: str,
                                tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    eid = str(edge_id or "").strip()
    uid = str(user_id or "").strip()
    if not eid or not uid:
        raise HTTPException(status_code=400, detail="edge_id_and_user_id_required")
    revoked = 0
    try:
        fn = getattr(control_plane_store, "revoke_edge_view_links_for_user", None)
        if callable(fn):
            revoked = int(fn(tenant_id=tid, edge_id=eid, user_id=uid) or 0)
    except Exception:
        revoked = 0
    _audit(request, tenant_id=tid, action="edge.user_view_link.revoke", outcome="ok",
           details={"edge_id": eid, "user_id": uid, "revoked_count": revoked})
    return {"ok": True, "tenant_id": tid, "edge_id": eid, "user_id": uid, "revoked": revoked}


# Public resolver — NO auth. Returns the scope a Lite share-link viewer is
# allowed to see. Defined outside the auth-protected router via a fresh
# APIRouter-less callable mounted at the FastAPI app root in main.py.
def resolve_edge_view_link_public(token: str) -> dict[str, Any]:
    t = str(token or "").strip()
    if not t:
        raise HTTPException(status_code=400, detail="token_required")
    row = None
    try:
        if hasattr(control_plane_store, "get_edge_view_link_by_token"):
            row = control_plane_store.get_edge_view_link_by_token(token=t)
    except Exception:
        row = None
    if not row or str(row.get("status") or "") != "active":
        raise HTTPException(status_code=404, detail="view_link_not_found")
    try:
        if hasattr(control_plane_store, "touch_edge_view_link"):
            control_plane_store.touch_edge_view_link(token=t)
    except Exception:
        pass
    return {
        "ok": True,
        "tenant_id": str(row.get("tenant_id") or ""),
        "customer_id": str(row.get("customer_id") or ""),
        "edge_id": str(row.get("edge_id") or ""),
    }


@router.post("/edges/heartbeat")
def heartbeat_edge(request: Request, edge_id: str, payload: dict[str, Any] | None = None, tenant_id: str | None = None) -> dict[str, Any]:
    # Heartbeat tolerates a cross-tenant lookup for master admin: edges
    # created on per-customer tenants (e.g. tenant-smoke-customer-XXXX)
    # still need to receive heartbeats sent with tenant_id=default by the
    # post-deploy smoke test. For non-master callers we keep the strict
    # tenant scoping.
    auth_payload = getattr(request.state, "user_payload", {}) or {}
    is_master = _is_global_admin(auth_payload)
    requested = _scoped_tenant(request, tenant_id)
    # Try the heartbeat with the requested tenant first
    row = control_plane_store.heartbeat_edge(tenant_id=requested, edge_id=edge_id, payload=payload or {})
    if not row and is_master:
        # Master: find the edge across tenants and retry
        try:
            all_edges = control_plane_store.list_edges(all_tenants=True)
            owner = next((r for r in all_edges if str(r.get("edge_id") or "") == str(edge_id or "")), None)
            if owner:
                actual_tid = normalize_tenant_id(str(owner.get("tenant_id") or "default"))
                row = control_plane_store.heartbeat_edge(tenant_id=actual_tid, edge_id=edge_id, payload=payload or {})
                if row:
                    _audit(request, tenant_id=actual_tid, action="edge.heartbeat", outcome="ok",
                           details={"edge_id": edge_id, "resolved_tenant": actual_tid})
                    return {"ok": True, "tenant_id": actual_tid, "row": row}
        except Exception:
            pass
    if not row:
        _audit(request, tenant_id=requested, action="edge.heartbeat", outcome="not_found", details={"edge_id": edge_id})
        raise HTTPException(status_code=404, detail="edge_not_found")
    _audit(request, tenant_id=requested, action="edge.heartbeat", outcome="ok", details={"edge_id": edge_id})
    return {"ok": True, "tenant_id": requested, "row": row}


@router.get("/licenses")
def list_licenses(request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    if _master_wants_all_tenants(request, tenant_id):
        rows = control_plane_store.list_licenses(all_tenants=True)
        return {"ok": True, "tenant_id": "__all__", "rows": rows}
    tid = _scoped_tenant(request, tenant_id)
    return {"ok": True, "tenant_id": tid, "rows": control_plane_store.list_licenses(tenant_id=tid)}


@router.post("/licenses")
def upsert_license(payload: LicenseUpsertRequest, request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    # Same pattern as customers/edges: authorize the caller, then put the
    # license on the customer's per-customer tenant.
    caller_tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    assigned_tenant = (
        _customer_tenant_id(payload.customer_id) if str(payload.customer_id or "").strip() else caller_tid
    )
    row = control_plane_store.upsert_license(
        tenant_id=assigned_tenant,
        license_id=payload.license_id,
        customer_id=payload.customer_id,
        plan_code=payload.plan_code,
        status=payload.status,
        start_utc=payload.start_utc,
        end_utc=payload.end_utc,
        max_edges=payload.max_edges,
        max_users=payload.max_users,
        metadata=payload.metadata,
    )
    _audit(
        request,
        tenant_id=assigned_tenant,
        action="license.upsert",
        outcome="ok",
        details={"license_id": row.get("license_id", ""), "assigned_tenant": assigned_tenant},
    )
    return {"ok": True, "tenant_id": assigned_tenant, "row": row}


@router.delete("/licenses/{license_id}")
def delete_license(request: Request, license_id: str, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    deleted = control_plane_store.delete_license(tenant_id=tid, license_id=license_id)
    if not deleted:
        _audit(request, tenant_id=tid, action="license.delete", outcome="not_found", details={"license_id": license_id})
        raise HTTPException(status_code=404, detail="license_not_found")
    _audit(request, tenant_id=tid, action="license.delete", outcome="ok", details={"license_id": license_id})
    return {"ok": True, "tenant_id": tid, "license_id": license_id}

@router.post("/licenses/{license_id}/delete")
def delete_license_post(request: Request, license_id: str, tenant_id: str | None = None) -> dict[str, Any]:
    return delete_license(request=request, license_id=license_id, tenant_id=tenant_id)


def _require_license_tenant_match(request: Request, license_id: str, *, require_admin_write: bool = False) -> str:
    """Bind a license_id to the caller's tenant.

    Resolves the license's owning tenant and forces _scoped_tenant() to use
    it, so a customer admin cannot read or modify licenses that belong to
    another tenant by guessing the license_id. Global admin (tenant=default,
    role=admin) is still allowed cross-tenant via _scoped_tenant() below.
    A missing license is treated as 404 so we don't reveal id existence to
    other tenants via the 403 timing channel.
    """
    license_tenant = control_plane_store.get_license_tenant(license_id=license_id)
    if not license_tenant:
        raise HTTPException(status_code=404, detail="License not found")
    return _scoped_tenant(request, license_tenant, require_admin_write=require_admin_write)


@router.get("/licenses/{license_id}/modules")
def list_license_modules(request: Request, license_id: str) -> dict[str, Any]:
    _require_license_tenant_match(request, license_id)
    return {"ok": True, "license_id": license_id, "rows": control_plane_store.list_license_modules(license_id=license_id)}


@router.put("/licenses/{license_id}/modules")
def set_license_modules(request: Request, license_id: str, payload: LicenseModulesRequest) -> dict[str, Any]:
    tid = _require_license_tenant_match(request, license_id, require_admin_write=True)
    out = {"ok": True, **control_plane_store.set_license_modules(license_id=license_id, modules=payload.modules)}
    _audit(request, tenant_id=tid, action="license.modules.set", outcome="ok", details={"license_id": license_id})
    return out


@router.get("/users")
def list_users(request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    """List users. Two modes:
      - tenant_id="__all__" (or "*"): return EVERY user across every
        tenant. Only the master/global admin (role=admin, tenant=default)
        can call this; everyone else gets 403. Lets the master admin
        manage all tenants/customers from one view.
      - tenant_id=<specific tenant>: existing tenant-scoped behaviour.
        Falls back to the caller's own tenant from the JWT if omitted.
    """
    # Bring /users in line with /customers, /edges, /licenses, /activation-codes:
    # the master admin gets all-tenant rows whenever they ask for `__all__`
    # OR send the legacy `default` (which the portal still sends from cards
    # that pre-date per-customer tenancy). Without this branch, a master
    # opening the workspace right after activating a customer edge sees no
    # admin user for that customer — the customer's admin lives on
    # tenant-cust-..., but the call was scoped to `default`.
    if _master_wants_all_tenants(request, tenant_id):
        rows = control_plane_store.list_users(all_tenants=True)
        return {"ok": True, "tenant_id": "__all__", "rows": rows}
    tid = _scoped_tenant(request, tenant_id)
    return {"ok": True, "tenant_id": tid, "rows": control_plane_store.list_users(tenant_id=tid)}


@router.post("/users")
def upsert_user(payload: UserUpsertRequest, request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    # Per-customer tenancy: the user lives on the customer's tenant, not
    # the caller's. master admin creating a Lite user for Customer A
    # writes the user under 'tenant-<A>'. Without a customer_id we fall
    # back to the caller's tenant (master's own staff accounts).
    caller_tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    assigned_tenant = (
        _customer_tenant_id(payload.customer_id) if str(payload.customer_id or "").strip() else caller_tid
    )
    row = control_plane_store.upsert_user(
        tenant_id=assigned_tenant,
        customer_id=payload.customer_id,
        username=payload.username,
        password=payload.password,
        role=payload.role,
        status=payload.status,
        email=payload.email,
        mfa_enabled=payload.mfa_enabled,
        modules=payload.modules,
        permissions=payload.permissions,
    )
    row.pop("password_hash", None)
    _audit(request, tenant_id=assigned_tenant, action="user.upsert", outcome="ok",
           details={"username": payload.username, "assigned_tenant": assigned_tenant})
    # Mirror the user to Supabase Auth + lite_profiles so the same login
    # works in the cloud Lite app under the same tenant. Best-effort —
    # the local save above is the source of truth; cloud mirror is a
    # convenience and never blocks this endpoint.
    try:
        from app.services.lite_user_mirror import mirror_user_upsert
        mirror_user_upsert(
            tenant_id=assigned_tenant,
            username=payload.username,
            password=payload.password,
            role=payload.role,
            email=payload.email,
        )
    except Exception:
        pass
    # Cloud cp_users mirror — only when this edge runs in `local`
    # control-plane mode (cloud-mode already writes directly to Supabase
    # via control_plane_store_cloud). Without this hop, edge-created
    # users never propagate to OTHER edges in the same tenant via their
    # cp_users_puller. Best-effort, never blocks the local save.
    try:
        import os as _os, requests as _rq
        backend_mode = str(_os.environ.get("TRUSTNODE_CONTROL_PLANE_BACKEND", "")).strip().lower()
        cloud_url = _resolve_cloud_control_plane_base(request)
        if backend_mode != "cloud" and cloud_url and not _is_same_origin_as_request(cloud_url, request):
            _rq.post(
                f"{cloud_url}/api/control-plane/users",
                params={"tenant_id": assigned_tenant},
                json={
                    "customer_id": payload.customer_id,
                    "username": payload.username,
                    "password": payload.password,
                    "role": payload.role,
                    "status": payload.status,
                    "email": payload.email,
                    "mfa_enabled": payload.mfa_enabled,
                    "modules": payload.modules,
                    "permissions": payload.permissions,
                },
                headers={
                    "Content-Type": "application/json",
                    "Authorization": request.headers.get("Authorization", ""),
                },
                timeout=8,
            )
    except Exception:
        pass
    return {"ok": True, "tenant_id": assigned_tenant, "row": row}


@router.delete("/users/{username}")
def delete_user(request: Request, username: str, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    if str(username or "").strip().lower() == "admin":
        raise HTTPException(status_code=400, detail="builtin_admin_cannot_be_deleted")
    deleted = control_plane_store.delete_user(tenant_id=tid, username=username)
    if not deleted:
        _audit(request, tenant_id=tid, action="user.delete", outcome="not_found", details={"username": username})
        raise HTTPException(status_code=404, detail="user_not_found")
    _audit(request, tenant_id=tid, action="user.delete", outcome="ok", details={"username": username})
    # Drop the mirrored Supabase Auth account so the deleted user can't
    # log into Lite anymore. Best-effort.
    try:
        from app.services.lite_user_mirror import mirror_user_delete
        mirror_user_delete(tenant_id=tid, username=username)
    except Exception:
        pass
    # Cloud cp_users delete in local-mode control plane (same rationale
    # as upsert): keeps other edges' cp_users_puller views consistent.
    try:
        import os as _os, requests as _rq
        backend_mode = str(_os.environ.get("TRUSTNODE_CONTROL_PLANE_BACKEND", "")).strip().lower()
        cloud_url = _resolve_cloud_control_plane_base(request)
        if backend_mode != "cloud" and cloud_url and not _is_same_origin_as_request(cloud_url, request):
            _rq.delete(
                f"{cloud_url}/api/control-plane/users/{username}",
                params={"tenant_id": tid},
                headers={"Authorization": request.headers.get("Authorization", "")},
                timeout=8,
            )
    except Exception:
        pass
    return {"ok": True, "tenant_id": tid, "username": username}

@router.post("/users/{username}/password")
def set_user_password(payload: UserSetPasswordRequest, request: Request, username: str,
                      tenant_id: str | None = None) -> dict[str, Any]:
    """Admin sets a specific password for a user (and optionally flags
    the account so the user must change it on next login)."""
    tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    if not str(payload.password or "").strip():
        raise HTTPException(status_code=400, detail="password_required")
    row = control_plane_store.set_user_password(
        tenant_id=tid, username=username,
        password=payload.password, must_change=bool(payload.must_change),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="user_not_found")
    _audit(request, tenant_id=tid, action="user.password.set", outcome="ok",
           details={"username": username, "must_change": bool(payload.must_change)})
    # Mirror the new password to Supabase Auth so the same credential
    # works in Lite without re-saving the user from the edge UI.
    try:
        from app.services.lite_user_mirror import mirror_user_upsert
        mirror_user_upsert(
            tenant_id=tid, username=username,
            password=payload.password,
            role=str(row.get("role") or "viewer"),
            email=str(row.get("email") or ""),
        )
    except Exception:
        pass
    return {"ok": True, "tenant_id": tid, "row": row}


@router.post("/users/{username}/password/temp")
def generate_temp_user_password(payload: UserTempPasswordRequest, request: Request,
                                username: str, tenant_id: str | None = None) -> dict[str, Any]:
    """Admin presses the Reset button: backend rolls a strong random
    password, installs it on the user with must_change_password=1, and
    returns the plaintext ONCE so the admin can copy it from the portal
    screen and hand it to the user.
    """
    tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    result = control_plane_store.generate_temp_password(
        tenant_id=tid, username=username, length=int(payload.length or 14),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="user_not_found")
    plaintext, row = result
    _audit(request, tenant_id=tid, action="user.password.temp", outcome="ok",
           details={"username": username})
    # Mirror to Supabase Auth so the user can use the temp password to
    # log into Lite right away (must_change_password is an edge concept
    # — the Lite app shows the change-password modal on first login by
    # reading the flag from /api/auth/login when used through the edge,
    # or operators can simply re-issue from the edge after the user logs
    # in to Lite once. Mirror keeps the credentials in sync regardless).
    try:
        from app.services.lite_user_mirror import mirror_user_upsert
        mirror_user_upsert(
            tenant_id=tid, username=username,
            password=plaintext,
            role=str(row.get("role") or "viewer"),
            email=str(row.get("email") or ""),
        )
    except Exception:
        pass
    return {"ok": True, "tenant_id": tid, "row": row,
            "temp_password": plaintext, "must_change_password": True}


@router.post("/users/{username}/delete")
def delete_user_post(request: Request, username: str, tenant_id: str | None = None) -> dict[str, Any]:
    return delete_user(request=request, username=username, tenant_id=tenant_id)


@router.post("/activation-code/issue")
def issue_activation_code(payload: ActivationCodeIssueRequest, request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    # The activation code is what the edge reads to know which tenant it
    # belongs to (edge_link/local-finalize uses payload.tenant_id). For
    # per-customer isolation that MUST be the customer's tenant, not the
    # master admin's `default`. Force it here regardless of what the
    # portal sent in the request.
    caller_tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    assigned_tenant = (
        _customer_tenant_id(payload.customer_id) if str(payload.customer_id or "").strip() else caller_tid
    )
    try:
        row = control_plane_store.issue_activation_code(
            tenant_id=assigned_tenant,
            customer_id=payload.customer_id,
            edge_id=payload.edge_id,
            license_id=payload.license_id,
            edge_name=payload.edge_name,
            ttl_minutes=payload.ttl_minutes,
            metadata=payload.metadata,
        )
    except Exception as exc:
        _audit(
            request,
            tenant_id=assigned_tenant,
            action="activation_code.issue",
            outcome="error",
            details={
                "customer_id": payload.customer_id,
                "edge_id": payload.edge_id,
                "license_id": payload.license_id,
                "error": str(exc),
            },
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(
        request,
        tenant_id=assigned_tenant,
        action="activation_code.issue",
        outcome="ok",
        details={
            "customer_id": payload.customer_id,
            "edge_id": payload.edge_id,
            "license_id": payload.license_id,
            "edge_name": payload.edge_name,
            "assigned_tenant": assigned_tenant,
        },
    )
    return {"ok": True, "tenant_id": assigned_tenant, "row": row}


@router.post("/activation-code/apply")
def apply_activation_code(payload: ActivationCodeApplyRequest, request: Request) -> dict[str, Any]:
    auth_payload = getattr(request.state, "user_payload", {}) or {}
    actor_type = "user" if auth_payload else "device"
    actor_id = str(auth_payload.get("sub") or payload.edge_id or "edge")
    try:
        row = control_plane_store.activate_edge_with_code(
            activation_code=payload.activation_code,
            edge_id=payload.edge_id,
            edge_name=payload.edge_name,
            site=payload.site,
            area=payload.area,
            equipment=payload.equipment,
            consume_code=False,
        )
        tid = normalize_tenant_id(str(row.get("tenant_id") or get_current_tenant()))
        control_plane_store.audit(
            actor_type=actor_type,
            actor_id=actor_id,
            tenant_id=tid,
            action="activation_code.apply",
            outcome="ok",
            correlation_id=request.headers.get("X-Correlation-Id", "") or request.headers.get("X-Request-Id", "") or "-",
            details={"edge_id": payload.edge_id},
        )
        # Operator 2026-06-18: mirror the freshly-activated edge into the
        # Windows registry so a future data-folder wipe + reinstall
        # auto-restores the license. Best-effort: failure here doesn't
        # affect the activation itself.
        try:
            control_plane_store.mirror_activation_to_registry()
        except Exception:
            pass
        return {"ok": True, "row": row}
    except Exception as exc:
        control_plane_store.audit(
            actor_type=actor_type,
            actor_id=actor_id,
            tenant_id=get_current_tenant(),
            action="activation_code.apply",
            outcome="error",
            correlation_id=request.headers.get("X-Correlation-Id", "") or request.headers.get("X-Request-Id", "") or "-",
            details={"edge_id": payload.edge_id, "error": str(exc)},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/activation-codes")
def list_activation_codes(request: Request, tenant_id: str | None = None, customer_id: str = "") -> dict[str, Any]:
    if _master_wants_all_tenants(request, tenant_id):
        rows = control_plane_store.list_activation_codes(all_tenants=True, customer_id=customer_id)
        return {"ok": True, "tenant_id": "__all__", "rows": rows}
    tid = _scoped_tenant(request, tenant_id)
    rows = control_plane_store.list_activation_codes(tenant_id=tid, customer_id=customer_id)
    return {"ok": True, "tenant_id": tid, "rows": rows}


@router.put("/activation-codes/{row_id}")
def update_activation_code(row_id: int, payload: ActivationCodeUpdateRequest, request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    try:
        row = control_plane_store.update_activation_code(
            tenant_id=tid,
            row_id=row_id,
            status=payload.status,
            expires_utc=payload.expires_utc,
        )
    except Exception as exc:
        _audit(request, tenant_id=tid, action="activation_code.update", outcome="error", details={"id": row_id, "error": str(exc)})
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(request, tenant_id=tid, action="activation_code.update", outcome="ok", details={"id": row_id, "status": row.get("status")})
    return {"ok": True, "tenant_id": tid, "row": row}


@router.delete("/activation-codes/{row_id}")
def delete_activation_code(row_id: int, request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    deleted = control_plane_store.delete_activation_code(tenant_id=tid, row_id=row_id)
    if not deleted:
        _audit(request, tenant_id=tid, action="activation_code.delete", outcome="not_found", details={"id": row_id})
        raise HTTPException(status_code=404, detail="activation_code_not_found")
    _audit(request, tenant_id=tid, action="activation_code.delete", outcome="ok", details={"id": row_id})
    return {"ok": True, "tenant_id": tid, "id": row_id}


@router.post("/activation-codes/{row_id}/delete")
def delete_activation_code_post(row_id: int, request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    return delete_activation_code(row_id=row_id, request=request, tenant_id=tenant_id)


@router.post("/password-reset/issue")
def issue_password_reset(payload: PasswordResetIssueRequest, request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    row = control_plane_store.issue_password_reset(
        tenant_id=tid,
        username=payload.username,
        ttl_minutes=payload.ttl_minutes,
    )
    _audit(request, tenant_id=tid, action="password_reset.issue", outcome="ok", details={"username": payload.username})
    return {"ok": True, "tenant_id": tid, "row": row}


@router.post("/password-reset/apply")
def apply_password_reset(payload: PasswordResetApplyRequest, request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    try:
        row = control_plane_store.reset_password_with_token(
            tenant_id=tid,
            username=payload.username,
            reset_token=payload.reset_token,
            new_password=payload.new_password,
        )
        row.pop("password_hash", None)
        _audit(request, tenant_id=tid, action="password_reset.apply", outcome="ok", details={"username": payload.username})
        return {"ok": True, "tenant_id": tid, "row": row}
    except Exception as exc:
        _audit(
            request,
            tenant_id=tid,
            action="password_reset.apply",
            outcome="error",
            details={"username": payload.username, "error": str(exc)},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/password-reset/public/issue")
def issue_password_reset_public(payload: PasswordResetPublicIssueRequest, request: Request) -> dict[str, Any]:
    username = str(payload.username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username_required")
    tid = normalize_tenant_id(str(payload.tenant_id or get_current_tenant()))
    row = control_plane_store.issue_password_reset(
        tenant_id=tid,
        username=username,
        ttl_minutes=max(1, int(payload.ttl_minutes or 15)),
    )
    control_plane_store.audit(
        actor_type="system",
        actor_id="public",
        tenant_id=tid,
        action="password_reset.public_issue",
        outcome="ok",
        correlation_id=request.headers.get("X-Correlation-Id", "") or request.headers.get("X-Request-Id", "") or "-",
        details={"username": username},
    )
    # Local edge flow uses this verification code in-app.
    return {"ok": True, "tenant_id": tid, "row": row}


@router.post("/password-reset/public/apply")
def apply_password_reset_public(payload: PasswordResetPublicApplyRequest, request: Request) -> dict[str, Any]:
    username = str(payload.username or "").strip()
    reset_token = str(payload.reset_token or "").strip()
    new_password = str(payload.new_password or "")
    if not username or not reset_token or not new_password:
        raise HTTPException(status_code=400, detail="username_reset_token_new_password_required")
    tid = normalize_tenant_id(str(payload.tenant_id or get_current_tenant()))
    try:
        row = control_plane_store.reset_password_with_token(
            tenant_id=tid,
            username=username,
            reset_token=reset_token,
            new_password=new_password,
        )
        row.pop("password_hash", None)
        control_plane_store.audit(
            actor_type="system",
            actor_id="public",
            tenant_id=tid,
            action="password_reset.public_apply",
            outcome="ok",
            correlation_id=request.headers.get("X-Correlation-Id", "") or request.headers.get("X-Request-Id", "") or "-",
            details={"username": username},
        )
        return {"ok": True, "tenant_id": tid, "row": row}
    except Exception as exc:
        control_plane_store.audit(
            actor_type="system",
            actor_id="public",
            tenant_id=tid,
            action="password_reset.public_apply",
            outcome="error",
            correlation_id=request.headers.get("X-Correlation-Id", "") or request.headers.get("X-Request-Id", "") or "-",
            details={"username": username, "error": str(exc)},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/summary")
def tenant_summary(request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id)
    return {"ok": True, "tenant_id": tid, **control_plane_store.tenant_summary(tenant_id=tid)}


@router.get("/runtime-context")
def runtime_context(request: Request) -> dict[str, Any]:
    payload = _require_auth_payload(request)
    tenant_id = normalize_tenant_id(str(payload.get("tenant_id") or get_current_tenant()))
    return {
        "ok": True,
        "tenant_id": tenant_id,
        "username": str(payload.get("sub") or ""),
        "role": str(payload.get("role") or "viewer"),
        "modules": payload.get("modules") or [],
        "permissions": payload.get("permissions") or {},
        "edges": control_plane_store.list_edges(tenant_id=tenant_id),
        "customers": control_plane_store.list_customers(tenant_id=tenant_id),
    }


@router.get("/edge-bootstrap-status")
def edge_bootstrap_status(request: Request) -> dict[str, Any]:
    tid = _scoped_tenant(request, None)
    diag = telemetry_service.diagnostics()
    ingest_url = str(diag.get("vps_ingest_url") or "").strip()
    device_token_mode = str(diag.get("device_token_mode") or "gateway_auto_issue")
    outbox_depth = int(diag.get("outbox_depth") or 0)
    return {
        "ok": True,
        "tenant_id": tid,
        "ingest_ready": bool(ingest_url),
        "ingest_url": ingest_url,
        "device_token_mode": device_token_mode,
        "outbox_depth": outbox_depth,
        "oldest_unsynced_sample_ts_utc": diag.get("oldest_unsynced_sample_ts_utc"),
        "last_outbox_error": diag.get("last_outbox_error"),
        "by_gateway": diag.get("outbox_by_gateway") or [],
    }


@router.post("/provision/customer-bundle")
def provision_customer_bundle(payload: CustomerBundleProvisionRequest, request: Request) -> dict[str, Any]:
    user_payload = _require_auth_payload(request)
    if not _is_global_admin(user_payload):
        raise HTTPException(status_code=403, detail="Global admin required")
    license_id = str(payload.license_id or "").strip() or f"lic-{normalize_tenant_id(payload.tenant_id)}"
    row = control_plane_store.provision_customer_bundle(
        tenant_id=payload.tenant_id,
        tenant_name=payload.tenant_name,
        primary_domain=payload.primary_domain,
        timezone_name=payload.timezone,
        customer_id=payload.customer_id,
        company_name=payload.company_name,
        contact_email=payload.contact_email,
        admin_username=payload.admin_username,
        admin_password=payload.admin_password,
        license_id=license_id,
        plan_code=payload.plan_code,
        max_edges=payload.max_edges,
        max_users=payload.max_users,
        modules=payload.modules,
    )
    _audit(
        request,
        tenant_id=normalize_tenant_id(payload.tenant_id),
        action="customer_bundle.provision",
        outcome="ok",
        details={
            "tenant_id": payload.tenant_id,
            "customer_id": payload.customer_id,
            "primary_domain": payload.primary_domain,
            "license_id": license_id,
        },
    )
    return {"ok": True, "row": row}


@router.get("/portal-context")
def portal_context(request: Request) -> dict[str, Any]:
    host = str(request.headers.get("host") or "").strip().lower().split(":")[0]
    tenant = control_plane_store.get_tenant_by_domain(host=host) if host else None
    if not tenant:
        return {"ok": True, "host": host, "resolved": False}
    tid = normalize_tenant_id(str(tenant.get("tenant_id") or "default"))
    return {
        "ok": True,
        "resolved": True,
        "host": host,
        "tenant_id": tid,
        "tenant_name": str(tenant.get("name") or tid),
        "primary_domain": str(tenant.get("primary_domain") or ""),
        "timezone": str(tenant.get("timezone") or "UTC"),
        "summary": control_plane_store.tenant_summary(tenant_id=tid).get("counts") or {},
    }


@router.post("/edge-link/bootstrap")
def edge_link_bootstrap(payload: ActivationCodeApplyRequest, request: Request) -> dict[str, Any]:
    cloud_url = _resolve_cloud_control_plane_base(request)
    try:
        row = control_plane_store.build_edge_bootstrap_payload(
            activation_code=payload.activation_code,
            edge_id=payload.edge_id,
            edge_name=payload.edge_name,
            site=payload.site,
            area=payload.area,
            equipment=payload.equipment,
            cloud_url=cloud_url,
        )
        control_plane_store.audit(
            actor_type="device",
            actor_id=str(payload.edge_id or "edge"),
            tenant_id=normalize_tenant_id(str(row.get("tenant_id") or "default")),
            action="edge_link.bootstrap",
            outcome="ok",
            correlation_id=request.headers.get("X-Correlation-Id", "") or request.headers.get("X-Request-Id", "") or "-",
            details={"edge_id": payload.edge_id},
        )
        return {"ok": True, "row": row}
    except Exception as exc:
        if "activation_code_not_found" in str(exc or "") and cloud_url and (not _is_same_origin_as_request(cloud_url, request)):
            try:
                safe_admin_username = str(getattr(payload, "admin_username", "admin") or "admin").strip() or "admin"
                safe_admin_password = str(getattr(payload, "admin_password", "") or "")
                upstream = requests.post(
                    f"{cloud_url}/api/control-plane/edge-link/bootstrap",
                    json={
                        "activation_code": payload.activation_code,
                        "edge_id": payload.edge_id,
                        "edge_name": payload.edge_name,
                        "site": payload.site,
                        "area": payload.area,
                        "equipment": payload.equipment,
                        "admin_username": safe_admin_username,
                        "admin_password": safe_admin_password,
                    },
                    headers={"Content-Type": "application/json"},
                    timeout=20,
                )
                if 200 <= upstream.status_code < 300:
                    data = upstream.json()
                    row = data.get("row") if isinstance(data, dict) and isinstance(data.get("row"), dict) else (data if isinstance(data, dict) else {})
                    if isinstance(row, dict):
                        tid = normalize_tenant_id(str(row.get("tenant_id") or "default"))
                        eid = str(row.get("edge_id") or payload.edge_id or "").strip()
                        if eid:
                            control_plane_store.upsert_edge(
                                tenant_id=tid,
                                edge_id=eid,
                                edge_name=str(row.get("edge_name") or payload.edge_name or eid),
                                customer_id=str(row.get("customer_id") or "").strip(),
                                site=str(row.get("site") or payload.site or ""),
                                area=str(row.get("area") or payload.area or ""),
                                equipment=str(row.get("equipment") or payload.equipment or ""),
                                status="active",
                                metadata={"activated_via": "code", "source": "cloud_bootstrap_proxy"},
                            )
                    if isinstance(data, dict) and data.get("ok"):
                        return {"ok": True, "row": row}
                    return {"ok": True, "row": row}
                try:
                    detail = (upstream.json() or {}).get("detail")
                except Exception:
                    detail = upstream.text
                raise ValueError(str(detail or f"upstream_error_{upstream.status_code}"))
            except Exception as up_exc:
                exc = up_exc
        control_plane_store.audit(
            actor_type="device",
            actor_id=str(payload.edge_id or "edge"),
            tenant_id=get_current_tenant(),
            action="edge_link.bootstrap",
            outcome="error",
            correlation_id=request.headers.get("X-Correlation-Id", "") or request.headers.get("X-Request-Id", "") or "-",
            details={"edge_id": payload.edge_id, "error": str(exc)},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/edge-link/register")
def edge_link_register(payload: EdgeRegisterRequest, request: Request) -> dict[str, Any]:
    cloud_url = _resolve_cloud_control_plane_base(request)
    admin_username = str(payload.admin_username or "").strip() or "admin"
    admin_password = str(payload.admin_password or "").strip() or "admin"
    try:
        proxied_row: dict[str, Any] | None = None
        def _extract_scope_from_upstream(data: dict[str, Any] | None) -> tuple[str, str, str]:
            d = data or {}
            nested = d.get("row") if isinstance(d.get("row"), dict) else {}
            tenant_val = str(d.get("tenant_id") or nested.get("tenant_id") or "").strip()
            customer_val = str(d.get("customer_id") or nested.get("customer_id") or "").strip()
            license_val = str(d.get("license_id") or nested.get("license_id") or "").strip()
            if not license_val:
                lic_obj = nested.get("license") if isinstance(nested.get("license"), dict) else {}
                license_val = str(lic_obj.get("license_id") or "").strip()
            return tenant_val, customer_val, license_val
        try:
            row = control_plane_store.build_edge_bootstrap_payload(
                activation_code=payload.activation_code,
                edge_id=payload.edge_id,
                edge_name=payload.edge_name,
                site=payload.site,
                area=payload.area,
                equipment=payload.equipment,
                cloud_url=cloud_url,
            )
        except Exception as bootstrap_exc:
            # Desktop/local edge can run against a local app-store where control-plane
            # rows are absent or stale. Resolve activation against cloud control-plane
            # for any activation-related local resolution error.
            bootstrap_err = str(bootstrap_exc or "")
            should_try_cloud = cloud_url and (not _is_same_origin_as_request(cloud_url, request)) and (
                "activation_code_" in bootstrap_err
                or "activation_" in bootstrap_err
            )
            if should_try_cloud:
                upstream = requests.post(
                    f"{cloud_url}/api/control-plane/edge-link/register",
                    json={
                        "activation_code": payload.activation_code,
                        "edge_id": payload.edge_id,
                        "edge_name": payload.edge_name,
                        "site": payload.site,
                        "area": payload.area,
                        "equipment": payload.equipment,
                        "admin_username": payload.admin_username,
                        "admin_password": admin_password,
                    },
                    headers={"Content-Type": "application/json"},
                    timeout=20,
                )
                if 200 <= upstream.status_code < 300:
                    data = upstream.json() if upstream.text else {}
                    if isinstance(data, dict) and data.get("ok"):
                        proxied_row = dict(data)
                        upstream_tenant_id, upstream_customer_id, upstream_license_id = _extract_scope_from_upstream(proxied_row)
                        upstream_license_obj: dict[str, Any] = {"license_id": str(upstream_license_id or data.get("license_id") or "")}
                        # Enrich proxied license payload with full cloud license details/modules so local finalize
                        # can persist start/end and module entitlements immediately.
                        try:
                            if cloud_url and upstream_license_obj.get("license_id"):
                                tenant_q = str(upstream_tenant_id or "default")
                                auth = str(request.headers.get("Authorization") or "").strip()
                                fwd_headers = {"Content-Type": "application/json"}
                                if auth:
                                    fwd_headers["Authorization"] = auth
                                lic_res = requests.get(
                                    f"{cloud_url}/api/control-plane/licenses?tenant_id={requests.utils.quote(tenant_q, safe='')}",
                                    headers=fwd_headers,
                                    timeout=15,
                                )
                                if 200 <= lic_res.status_code < 300:
                                    rows = (lic_res.json() or {}).get("rows") if lic_res.text else []
                                    if isinstance(rows, list):
                                        hit = next(
                                            (
                                                r
                                                for r in rows
                                                if str((r or {}).get("license_id") or "").strip()
                                                == str(upstream_license_obj.get("license_id") or "").strip()
                                            ),
                                            None,
                                        )
                                        if isinstance(hit, dict):
                                            upstream_license_obj.update(hit)
                                mod_res = requests.get(
                                    f"{cloud_url}/api/control-plane/licenses/{requests.utils.quote(str(upstream_license_obj.get('license_id') or ''), safe='')}/modules",
                                    headers=fwd_headers,
                                    timeout=15,
                                )
                                if 200 <= mod_res.status_code < 300:
                                    mod_rows = (mod_res.json() or {}).get("rows") if mod_res.text else []
                                    if isinstance(mod_rows, list):
                                        upstream_license_obj["modules"] = mod_rows
                        except Exception:
                            pass
                        row = {
                            "tenant_id": str(upstream_tenant_id or "default"),
                            "customer_id": str(upstream_customer_id or ""),
                            "edge_id": str(data.get("edge_id") or payload.edge_id),
                            "edge_name": str(data.get("edge_name") or payload.edge_name or payload.edge_id),
                            "site": str(data.get("site") or payload.site or ""),
                            "area": str(data.get("area") or payload.area or ""),
                            "equipment": str(data.get("equipment") or payload.equipment or ""),
                            "primary_domain": str(data.get("primary_domain") or ""),
                            "cloud_api_url": str(data.get("cloud_api_url") or cloud_url or ""),
                            "license": upstream_license_obj,
                            "app_settings_patch": {},
                        }
                    else:
                        return {"ok": True, "row": data}
                else:
                    try:
                        detail = (upstream.json() or {}).get("detail")
                    except Exception:
                        detail = upstream.text
                    raise ValueError(str(detail or f"upstream_error_{upstream.status_code}")) from bootstrap_exc
            else:
                raise
        # Finalize one-time activation only at registration commit (local store path).
        if proxied_row is None:
            control_plane_store.activate_edge_with_code(
                activation_code=payload.activation_code,
                edge_id=payload.edge_id,
                edge_name=payload.edge_name,
                site=payload.site,
                area=payload.area,
                equipment=payload.equipment,
                consume_code=True,
            )
        # Safety fallback: always resolve customer/license scope from activation record.
        # Some legacy bootstrap paths can return incomplete customer linkage.
        resolved_customer_id = str(row.get("customer_id") or "").strip()
        resolved_license_id = str((row.get("license") or {}).get("license_id") or "").strip()
        activation_row = control_plane_store.get_activation_code_row(activation_code=payload.activation_code) or {}
        if not resolved_customer_id or not resolved_license_id:
            try:
                probe = control_plane_store.activate_edge_with_code(
                    activation_code=payload.activation_code,
                    edge_id=str(row.get("edge_id") or payload.edge_id or ""),
                    edge_name=str(row.get("edge_name") or payload.edge_name or ""),
                    site=str(row.get("site") or payload.site or ""),
                    area=str(row.get("area") or payload.area or ""),
                    equipment=str(row.get("equipment") or payload.equipment or ""),
                    consume_code=False,
                )
                resolved_customer_id = resolved_customer_id or str(probe.get("customer_id") or "").strip()
                resolved_license_id = resolved_license_id or str((probe.get("license") or {}).get("license_id") or "").strip()
            except Exception:
                pass
        if proxied_row is not None and cloud_url and (not resolved_customer_id or not resolved_license_id):
            try:
                upstream_bootstrap = requests.post(
                    f"{cloud_url}/api/control-plane/edge-link/bootstrap",
                    json={
                        "activation_code": payload.activation_code,
                        "edge_id": str(row.get("edge_id") or payload.edge_id or ""),
                        "edge_name": str(row.get("edge_name") or payload.edge_name or ""),
                        "site": str(row.get("site") or payload.site or ""),
                        "area": str(row.get("area") or payload.area or ""),
                        "equipment": str(row.get("equipment") or payload.equipment or ""),
                    },
                    headers={"Content-Type": "application/json"},
                    timeout=20,
                )
                if 200 <= upstream_bootstrap.status_code < 300:
                    b = upstream_bootstrap.json() if upstream_bootstrap.text else {}
                    if isinstance(b, dict):
                        b_row = b.get("row") if isinstance(b.get("row"), dict) else b
                        resolved_customer_id = resolved_customer_id or str(b_row.get("customer_id") or "").strip()
                        if not resolved_license_id:
                            lic_obj = b_row.get("license") if isinstance(b_row.get("license"), dict) else {}
                            resolved_license_id = str(
                                b_row.get("license_id") or lic_obj.get("license_id") or ""
                            ).strip()
            except Exception:
                pass
        resolved_customer_id = resolved_customer_id or str(activation_row.get("customer_id") or "").strip()
        resolved_license_id = resolved_license_id or str(activation_row.get("license_id") or "").strip()
        tenant_id = normalize_tenant_id(str(row.get("tenant_id") or activation_row.get("tenant_id") or "default"))
        # Last-mile scope recovery for legacy/incomplete activation records:
        # derive customer from license mapping, or derive license from customer mapping.
        if not resolved_customer_id and resolved_license_id:
            try:
                lic_rows = control_plane_store.list_licenses(tenant_id=tenant_id) or []
                lic_match = next(
                    (r for r in lic_rows if str(r.get("license_id") or "").strip() == resolved_license_id),
                    None,
                )
                resolved_customer_id = str((lic_match or {}).get("customer_id") or "").strip()
            except Exception:
                pass
        if resolved_customer_id and not resolved_license_id:
            try:
                lic = control_plane_store.get_license_for_customer(
                    tenant_id=tenant_id,
                    customer_id=resolved_customer_id,
                ) or {}
                resolved_license_id = str(lic.get("license_id") or "").strip()
            except Exception:
                pass
        scope_incomplete = False
        scope_warnings: list[str] = []
        if not resolved_customer_id:
            scope_incomplete = True
            scope_warnings.append("customer_id_missing")
        if not resolved_license_id:
            scope_incomplete = True
            scope_warnings.append("license_id_missing")
        if scope_incomplete:
            raise ValueError(
                "activation_scope_resolution_failed:" + ",".join(scope_warnings)
            )
        # Create/refresh tenant admin in control-plane auth store.
        # This is mandatory for first login after activation.
        local_user_row = control_plane_store.upsert_user(
            tenant_id=tenant_id,
            customer_id=resolved_customer_id,
            username=admin_username,
            password=admin_password,
            role="admin",
            status="active",
            email="",
            mfa_enabled=False,
            modules=[],
            permissions={},
        )
        if not local_user_row:
            raise ValueError("activation_admin_user_create_failed_local")
        if str(local_user_row.get("customer_id") or "").strip() != resolved_customer_id:
            raise ValueError("activation_admin_user_scope_mismatch")

        # When activation was proxied to cloud control-plane, force cloud-side user upsert too.
        # This guarantees portal users list + hosted login can immediately authenticate.
        cloud_user_sync_ok = True
        cloud_user_sync_error = ""
        if proxied_row is not None and cloud_url:
            try:
                upstream_user = requests.post(
                    f"{cloud_url}/api/control-plane/users?tenant_id={tenant_id}",
                    json={
                        "customer_id": resolved_customer_id,
                        "username": admin_username,
                        "password": admin_password,
                        "role": "admin",
                        "status": "active",
                        "email": "",
                        "mfa_enabled": False,
                        "modules": [],
                        "permissions": {},
                    },
                    headers={
                        "Content-Type": "application/json",
                        # Reuse caller auth if available so cloud endpoint can authorize admin write.
                        "Authorization": request.headers.get("Authorization", ""),
                    },
                    timeout=20,
                )
                if not (200 <= upstream_user.status_code < 300):
                    try:
                        detail = (upstream_user.json() or {}).get("detail")
                    except Exception:
                        detail = upstream_user.text
                    cloud_user_sync_ok = False
                    cloud_user_sync_error = str(detail or f"cloud_user_upsert_failed_{upstream_user.status_code}")
            except Exception as exc:
                cloud_user_sync_ok = False
                cloud_user_sync_error = str(exc)

        # Materialize local bootstrap so first login works immediately.
        try:
            existing = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
            users_access = existing.get("users_access") if isinstance(existing.get("users_access"), dict) else {}
            users = users_access.get("users") if isinstance(users_access.get("users"), list) else []
            next_users = []
            replaced = False
            for u in users:
                if not isinstance(u, dict):
                    continue
                if str(u.get("username") or "").strip() == admin_username:
                    next_users.append(
                        {
                            "username": admin_username,
                            "password": admin_password,
                            "role": "admin",
                            "permissions": u.get("permissions") or {},
                            "modules": u.get("modules") or [],
                            "tenant_id": tenant_id,
                        }
                    )
                    replaced = True
                else:
                    next_users.append(dict(u))
            if not replaced:
                next_users.append(
                    {
                        "username": admin_username,
                        "password": admin_password,
                        "role": "admin",
                        "permissions": {},
                        "modules": [],
                        "tenant_id": tenant_id,
                    }
                )
            app_settings_patch = dict(row.get("app_settings_patch") or {})
            app_settings_patch["edge_id"] = str(row.get("edge_id") or payload.edge_id)
            app_settings_patch["edge_name"] = str(row.get("edge_name") or payload.edge_name or payload.edge_id)
            app_settings_patch["customer_id"] = resolved_customer_id
            app_settings_patch["edge_linked"] = True
            app_settings_patch["license_id"] = resolved_license_id
            app_settings_patch["edge_profile"] = {
                "edge_id": str(row.get("edge_id") or payload.edge_id or ""),
                "edge_name": str(row.get("edge_name") or payload.edge_name or payload.edge_id or ""),
                # See edge-link/local-finalize: linked_customer_id /
                # linked_license_id are what _build_scope_key reads to
                # route scoped writes (dashboards, alarms, etc.) to a row
                # Lite can filter by customer.
                "linked_customer_id": resolved_customer_id,
                "linked_license_id": resolved_license_id,
                "description": "",
                "location": " / ".join(
                    [p for p in [str(payload.site or "").strip(), str(payload.area or "").strip()] if p]
                ),
                "machine_group": str(payload.equipment or "").strip(),
            }
            app_store.save_bootstrap(
                {
                    "app_settings": app_settings_patch,
                    "users_access": {
                        "users": next_users,
                        "current_user": admin_username,
                    },
                },
                actor=f"edge_register:{admin_username}",
            )
            telemetry_service.configure_from_bootstrap({"data": app_store.get_bootstrap(prefer_cloud_reads=False)})
        except Exception:
            # Cloud-side registration should not fail due to optional local bootstrap materialization.
            pass
        control_plane_store.audit(
            actor_type="device",
            actor_id=str(payload.edge_id or "edge"),
            tenant_id=tenant_id,
            action="edge_link.register",
            outcome="ok",
            correlation_id=request.headers.get("X-Correlation-Id", "") or request.headers.get("X-Request-Id", "") or "-",
            details={"edge_id": payload.edge_id, "admin_username": admin_username},
        )
        response_license = dict(row.get("license") or {})
        response_license_id = str(response_license.get("license_id") or resolved_license_id or "").strip()
        local_modules = (
            control_plane_store.list_license_modules(license_id=response_license_id)
            if response_license_id
            else []
        )
        existing_modules = response_license.get("modules") if isinstance(response_license.get("modules"), list) else []
        response_license["modules"] = local_modules or existing_modules or []
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "edge_id": str(row.get("edge_id") or payload.edge_id),
            "edge_name": str(row.get("edge_name") or payload.edge_name or payload.edge_id),
            "customer_id": resolved_customer_id,
            "license_id": resolved_license_id,
            "license": response_license,
            "cloud_api_url": str(row.get("cloud_api_url") or ""),
            "primary_domain": str(row.get("primary_domain") or ""),
            "site": str(row.get("site") or payload.site or ""),
            "area": str(row.get("area") or payload.area or ""),
            "equipment": str(row.get("equipment") or payload.equipment or ""),
            "scope_incomplete": scope_incomplete,
            "scope_warnings": scope_warnings,
            "cloud_user_sync_ok": cloud_user_sync_ok,
            "cloud_user_sync_error": cloud_user_sync_error,
        }
    except Exception as exc:
        # Idempotent recovery: allow re-link using an already consumed activation code.
        if "activation_code_used" in str(exc or ""):
            try:
                activation_row = control_plane_store.get_activation_code_row(activation_code=payload.activation_code) or {}
                tenant_id = normalize_tenant_id(str(activation_row.get("tenant_id") or get_current_tenant()))
                resolved_edge_id = str(activation_row.get("edge_id") or payload.edge_id or "").strip()
                resolved_customer_id = str(activation_row.get("customer_id") or "").strip()
                resolved_license_id = str(activation_row.get("license_id") or "").strip()
                if not resolved_edge_id:
                    raise ValueError("edge_id_missing_for_used_activation_code")

                control_plane_store.upsert_edge(
                    tenant_id=tenant_id,
                    edge_id=resolved_edge_id,
                    edge_name=str(activation_row.get("edge_name") or payload.edge_name or resolved_edge_id),
                    customer_id=resolved_customer_id,
                    site=str(payload.site or ""),
                    area=str(payload.area or ""),
                    equipment=str(payload.equipment or ""),
                    status="active",
                    metadata={"source": "activation_code_used_relink", "license_id": resolved_license_id},
                )

                # Cloud bootstrap is public (activation-code based) and can return authoritative
                # license start/end/modules even when activation code has already been consumed.
                try:
                    cloud_url = _resolve_cloud_control_plane_base(request)
                    if cloud_url and (not _is_same_origin_as_request(cloud_url, request)):
                        b = requests.post(
                            f"{cloud_url}/api/control-plane/edge-link/bootstrap",
                            json={
                                "activation_code": payload.activation_code,
                                "edge_id": resolved_edge_id,
                                "edge_name": str(activation_row.get("edge_name") or payload.edge_name or resolved_edge_id),
                                "site": str(payload.site or ""),
                                "area": str(payload.area or ""),
                                "equipment": str(payload.equipment or ""),
                            },
                            headers={"Content-Type": "application/json"},
                            timeout=20,
                        )
                        if 200 <= b.status_code < 300:
                            data = b.json() if b.text else {}
                            brow = data.get("row") if isinstance(data, dict) and isinstance(data.get("row"), dict) else (data if isinstance(data, dict) else {})
                            lic_obj = brow.get("license") if isinstance(brow.get("license"), dict) else {}
                            lic_id = str(lic_obj.get("license_id") or resolved_license_id or "").strip()
                            if lic_id:
                                control_plane_store.upsert_license(
                                    tenant_id=tenant_id,
                                    license_id=lic_id,
                                    customer_id=resolved_customer_id,
                                    plan_code=str(lic_obj.get("plan_code") or "standard"),
                                    status=str(lic_obj.get("status") or "active"),
                                    start_utc=str(lic_obj.get("start_utc") or ""),
                                    end_utc=str(lic_obj.get("end_utc") or ""),
                                    max_edges=max(0, int(lic_obj.get("max_edges") or 0)),
                                    max_users=max(0, int(lic_obj.get("max_users") or 0)),
                                    metadata={"source": "activation_code_used_relink_bootstrap"},
                                )
                                if isinstance(lic_obj.get("modules"), list):
                                    control_plane_store.set_license_modules(
                                        license_id=lic_id,
                                        modules=list(lic_obj.get("modules") or []),
                                    )
                                resolved_license_id = lic_id
                except Exception:
                    pass

                control_plane_store.upsert_user(
                    tenant_id=tenant_id,
                    customer_id=resolved_customer_id,
                    username=admin_username,
                    password=admin_password,
                    role="admin",
                    status="active",
                    email="",
                    mfa_enabled=False,
                    modules=[],
                    permissions={},
                )

                app_store.save_bootstrap(
                    {
                        "app_settings": {
                            "tenant_login_realm": tenant_id,
                            "tenant_id": tenant_id,
                            "edge_id": resolved_edge_id,
                            "edge_name": str(activation_row.get("edge_name") or payload.edge_name or resolved_edge_id),
                            "customer_id": resolved_customer_id,
                            "license_id": resolved_license_id,
                            "edge_linked": True,
                        },
                        "users_access": {
                            "current_user": admin_username,
                        },
                    },
                    actor=f"edge_register_relink:{admin_username}",
                )
                telemetry_service.configure_from_bootstrap({"data": app_store.get_bootstrap(prefer_cloud_reads=False)})

                lic_rows = control_plane_store.list_licenses(tenant_id=tenant_id) or []
                lic = next((r for r in lic_rows if str(r.get("license_id") or "").strip() == resolved_license_id), {}) if resolved_license_id else {}
                lic_modules = control_plane_store.list_license_modules(license_id=resolved_license_id) if resolved_license_id else []
                license_payload = dict(lic or {})
                license_payload.setdefault("license_id", resolved_license_id)
                license_payload["modules"] = lic_modules

                return {
                    "ok": True,
                    "tenant_id": tenant_id,
                    "edge_id": resolved_edge_id,
                    "edge_name": str(activation_row.get("edge_name") or payload.edge_name or resolved_edge_id),
                    "customer_id": resolved_customer_id,
                    "license_id": resolved_license_id,
                    "license": license_payload,
                    "scope_incomplete": False,
                    "scope_warnings": [],
                    "cloud_user_sync_ok": True,
                    "cloud_user_sync_error": "",
                }
            except Exception:
                pass
        control_plane_store.audit(
            actor_type="device",
            actor_id=str(payload.edge_id or "edge"),
            tenant_id=get_current_tenant(),
            action="edge_link.register",
            outcome="error",
            correlation_id=request.headers.get("X-Correlation-Id", "") or request.headers.get("X-Request-Id", "") or "-",
            details={"edge_id": payload.edge_id, "error": str(exc)},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/edge-link/local-finalize")
def edge_link_local_finalize(payload: EdgeLocalFinalizeRequest, request: Request) -> dict[str, Any]:
    # Track which step we're in so a downstream failure produces a useful
    # error message instead of the generic 'No item with that key' that
    # bubbles out of sqlite3.Row when a stale-schema column is missing.
    step = "init"
    try:
        tenant_id = normalize_tenant_id(str(payload.tenant_id or "default"))
        admin_username = str(payload.admin_username or "").strip() or "admin"
        admin_password = str(payload.admin_password or "").strip() or "admin"
        edge_id = str(payload.edge_id or "").strip()
        edge_name = str(payload.edge_name or payload.edge_id or "").strip() or edge_id
        customer_id = str(payload.customer_id or "").strip()
        license_id = str(payload.license_id or "").strip()
        if not edge_id:
            raise ValueError("edge_id_missing_for_local_finalize")
        if not customer_id:
            raise ValueError("customer_id_missing_for_local_finalize")
        if not license_id:
            raise ValueError("license_id_missing_for_local_finalize")

        # Per-customer tenant guard (decided 2026-05-18): every customer
        # gets their own tenant slug 'tenant-<customer_id>'. If the cloud
        # bootstrap returned 'default' for a customer-scoped activation,
        # the portal hasn't been upgraded to per-customer tenancy yet and
        # finalizing the edge here would tag every write with 'default'
        # — which breaks Lite isolation between customers.
        expected_tenant = f"tenant-{customer_id}"
        if tenant_id == "default" and customer_id:
            raise ValueError(
                f"tenant_id_default_with_customer_id (got 'default', expected '{expected_tenant}'). "
                "Cloud portal is on an older build; apply the per-customer tenancy migration before activating."
            )

        # Materialize local control-plane scope so license-check can validate immediately.
        step = "upsert_customer"
        if customer_id:
            control_plane_store.upsert_customer(
                tenant_id=tenant_id,
                customer_id=customer_id,
                company_name=customer_id,
                contact_email="",
                status="active",
                metadata={"source": "edge_local_finalize"},
            )
        step = "upsert_license"
        if license_id:
            control_plane_store.upsert_license(
                tenant_id=tenant_id,
                license_id=license_id,
                customer_id=customer_id,
                plan_code=str(payload.license_plan_code or "standard"),
                status=str(payload.license_status or "active"),
                start_utc=str(payload.license_start_utc or ""),
                end_utc=str(payload.license_end_utc or ""),
                max_edges=max(0, int(payload.license_max_edges or 0)),
                max_users=max(0, int(payload.license_max_users or 0)),
                metadata={"source": "edge_local_finalize"},
            )
            if payload.license_modules:
                step = "set_license_modules"
                # Defensive normalisation: accept either dict rows
                # ({"module_key":..., "enabled":...}) or bare module-key
                # strings, since the cloud proxy path can return either
                # depending on which endpoint it hit. Without this,
                # set_license_modules can raise on a sqlite3.Row that
                # doesn't have a string key — producing the cryptic
                # "No item with that key" the user saw.
                normalized_modules: list[dict[str, Any]] = []
                for m in (payload.license_modules or []):
                    if isinstance(m, dict):
                        key = str(m.get("module_key") or m.get("key") or "").strip()
                        if not key: continue
                        normalized_modules.append({
                            "module_key": key,
                            "enabled": bool(m.get("enabled", True)),
                        })
                    elif isinstance(m, str) and m.strip():
                        normalized_modules.append({"module_key": m.strip(), "enabled": True})
                control_plane_store.set_license_modules(
                    license_id=license_id,
                    modules=normalized_modules,
                )
        step = "upsert_edge"
        if edge_id:
            control_plane_store.upsert_edge(
                tenant_id=tenant_id,
                edge_id=edge_id,
                edge_name=edge_name,
                customer_id=customer_id,
                site="",
                area="",
                equipment="",
                status="active",
                metadata={"source": "edge_local_finalize", "license_id": license_id},
            )

        # Ensure local auth store contains the edge admin for immediate login.
        step = "upsert_user"
        control_plane_store.upsert_user(
            tenant_id=tenant_id,
            customer_id=customer_id,
            username=admin_username,
            password=admin_password,
            role="admin",
            status="active",
            email="",
            mfa_enabled=False,
            modules=[],
            permissions={},
        )

        cloud_user_sync_ok = True
        cloud_user_sync_error = ""
        cloud_base = str(payload.cloud_api_url or "").strip().rstrip("/")
        if cloud_base and customer_id:
            try:
                cloud_user = str(
                    os.getenv("TRUSTNODE_CLOUD_AUTH_USER")
                    or "admin"
                ).strip()
                cloud_pass = str(
                    os.getenv("TRUSTNODE_CLOUD_AUTH_PASSWORD")
                    or "admin"
                ).strip()
                token = ""
                login_res = requests.post(
                    f"{cloud_base}/api/auth/login",
                    json={"username": cloud_user, "password": cloud_pass},
                    timeout=15,
                )
                if 200 <= login_res.status_code < 300:
                    body = login_res.json() if login_res.text else {}
                    token = str(body.get("access_token") or body.get("token") or "").strip()
                headers = {"Content-Type": "application/json"}
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                user_res = requests.post(
                    f"{cloud_base}/api/control-plane/users?tenant_id={requests.utils.quote(tenant_id, safe='')}",
                    json={
                        "customer_id": customer_id,
                        "username": admin_username,
                        "password": admin_password,
                        "role": "admin",
                        "status": "active",
                        "email": "",
                        "mfa_enabled": False,
                        "modules": [],
                        "permissions": {},
                    },
                    headers=headers,
                    timeout=20,
                )
                if user_res.status_code >= 400:
                    cloud_user_sync_ok = False
                    try:
                        cloud_user_sync_error = str((user_res.json() or {}).get("detail") or user_res.text or "").strip()
                    except Exception:
                        cloud_user_sync_error = str(user_res.text or "").strip()
            except Exception as exc:
                cloud_user_sync_ok = False
                cloud_user_sync_error = str(exc)

        existing = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
        users_access = existing.get("users_access") if isinstance(existing.get("users_access"), dict) else {}
        users = users_access.get("users") if isinstance(users_access.get("users"), list) else []
        next_users = []
        replaced = False
        for u in users:
            if not isinstance(u, dict):
                continue
            if str(u.get("username") or "").strip() == admin_username:
                next_users.append(
                    {
                        "username": admin_username,
                        "password": admin_password,
                        "role": "admin",
                        "permissions": u.get("permissions") or {},
                        "modules": u.get("modules") or [],
                        "tenant_id": tenant_id,
                    }
                )
                replaced = True
            else:
                next_users.append(dict(u))
        if not replaced:
            next_users.append(
                {
                    "username": admin_username,
                    "password": admin_password,
                    "role": "admin",
                    "permissions": {},
                    "modules": [],
                    "tenant_id": tenant_id,
                }
            )

        app_settings = dict((existing.get("app_settings") if isinstance(existing, dict) else {}) or {})
        app_settings["tenant_login_realm"] = tenant_id
        app_settings["tenant_id"] = tenant_id
        app_settings["edge_id"] = edge_id
        app_settings["edge_name"] = edge_name
        app_settings["customer_id"] = customer_id
        app_settings["license_id"] = license_id
        app_settings["edge_linked"] = True
        # edge_profile MUST carry linked_customer_id / linked_license_id so
        # the scoped writes (dashboard_configurations, alarms_setup, etc.)
        # land under the right scope key. Without these the scope key
        # collapses to 'tenant|-|edge|user' and the cloud mirror writes
        # to a row Lite can't filter back to a customer.
        existing_edge_profile = (
            (app_settings.get("edge_profile") or {})
            if isinstance(app_settings.get("edge_profile"), dict)
            else {}
        )
        app_settings["edge_profile"] = {
            "edge_id": edge_id,
            "edge_name": edge_name,
            "linked_customer_id": customer_id,
            "linked_license_id": license_id,
            "description": str(existing_edge_profile.get("description") or ""),
            "location": str(existing_edge_profile.get("location") or ""),
            "machine_group": str(existing_edge_profile.get("machine_group") or ""),
        }
        if str(payload.cloud_api_url or "").strip():
            app_settings["cloud_url"] = str(payload.cloud_api_url or "").strip()
        if str(payload.primary_domain or "").strip():
            app_settings["tenant_web_client_url"] = f"https://{str(payload.primary_domain or '').strip()}"

        app_store.save_bootstrap(
            {
                "app_settings": app_settings,
                "users_access": {
                    "users": next_users,
                    "current_user": admin_username,
                },
            },
            actor=f"edge_local_finalize:{admin_username}",
        )
        telemetry_service.configure_from_bootstrap({"data": app_store.get_bootstrap(prefer_cloud_reads=False)})
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "edge_id": edge_id,
            "cloud_user_sync_ok": cloud_user_sync_ok,
            "cloud_user_sync_error": cloud_user_sync_error,
        }
    except Exception as exc:
        # Include the step name so the next failure is diagnosable
        # without spelunking through this whole function. 'No item with
        # that key' from a deeper sqlite3.Row access used to surface
        # bare with no context.
        raise HTTPException(status_code=400, detail=f"{step}: {exc}") from exc


@router.post("/edge-link/unlink")
def edge_link_unlink(request: Request) -> dict[str, Any]:
    payload = _require_auth_payload(request)
    if not _is_admin(payload):
        raise HTTPException(status_code=403, detail="Admin role required")
    tenant_id = normalize_tenant_id(str(payload.get("tenant_id") or get_current_tenant()))
    bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
    app_settings = dict(bootstrap.get("app_settings") or {})
    edge_id = str(app_settings.get("edge_id") or "").strip()
    if edge_id:
        # Mark linked edge inactive in control-plane metadata.
        try:
            edge_rows = control_plane_store.list_edges(tenant_id=tenant_id)
            current = next((r for r in edge_rows if str(r.get("edge_id") or "") == edge_id), None)
            if current:
                control_plane_store.upsert_edge(
                    tenant_id=tenant_id,
                    edge_id=edge_id,
                    edge_name=str(current.get("edge_name") or edge_id),
                    customer_id=str(current.get("customer_id") or ""),
                    site=str(current.get("site") or ""),
                    area=str(current.get("area") or ""),
                    equipment=str(current.get("equipment") or ""),
                    status="inactive",
                    metadata={"unlinked_utc": datetime.now(timezone.utc).isoformat()},
                )
        except Exception:
            pass
    reset_settings = {
        "tenant_login_realm": "",
        "tenant_id": "",
        "edge_id": "",
        "edge_name": "",
        "customer_id": "",
        "license_id": "",
        "edge_linked": False,
        "endpoint_mode": "local",
        "cloud_auto_sync_enabled": False,
    }
    # Save the reset settings — this is the only step that MUST succeed
    # for unlink to be considered complete. If save_bootstrap raises,
    # the local edge is still "linked" and we surface 500.
    try:
        app_store.save_bootstrap({"app_settings": reset_settings}, actor=f"edge_unlink:{payload.get('sub') or 'admin'}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"unlink_save_failed: {exc}") from exc
    # Reconfigure telemetry + audit are best-effort. A failure here
    # (e.g. control_plane_store backed by an unreachable Supabase)
    # shouldn't keep the edge in a half-unlinked state — the local
    # bootstrap is already cleared.
    try:
        telemetry_service.configure_from_bootstrap({"data": app_store.get_bootstrap(prefer_cloud_reads=False)})
    except Exception:
        pass
    try:
        _audit(request, tenant_id=tenant_id, action="edge_link.unlink", outcome="ok", details={"edge_id": edge_id})
    except Exception:
        pass
    return {"ok": True, "tenant_id": tenant_id, "edge_id": edge_id}


@router.get("/edge-link/license-check")
def edge_link_license_check(request: Request, edge_id: str = "", tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id)
    check_edge_id = str(edge_id or "").strip()
    if not check_edge_id:
        bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
        app_settings = dict(bootstrap.get("app_settings") or {})
        check_edge_id = str(app_settings.get("edge_id") or "").strip()
    out = control_plane_store.check_edge_license(tenant_id=tid, edge_id=check_edge_id)
    resolved_tenant = str(out.get("resolved_tenant_id") or tid)

    # If local scope is stale/incomplete, hydrate from cloud control-plane authoritative source.
    try:
        bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
        app_settings = dict(bootstrap.get("app_settings") or {})
        cloud_url = str(app_settings.get("cloud_url") or app_settings.get("cloud_api_url") or "").strip().rstrip("/")
        should_try_cloud = bool(cloud_url and not _is_same_origin_as_request(cloud_url, request))
        linked_license_id = str(app_settings.get("license_id") or "").strip()
        linked_edge_id = str(app_settings.get("edge_id") or check_edge_id or "").strip()

        # Local first-aid for legacy links: if edge is linked but customer scope is missing,
        # recover customer from locally mirrored license row by linked license_id.
        if str(out.get("reason") or "") == "edge_customer_missing" and linked_license_id and linked_edge_id:
            tenant_candidates = []
            for cand in [
                str(app_settings.get("tenant_id") or "").strip(),
                resolved_tenant,
                tid,
                "default",
            ]:
                c = normalize_tenant_id(str(cand or "default"))
                if c and c not in tenant_candidates:
                    tenant_candidates.append(c)
            for cand_tenant in tenant_candidates:
                try:
                    lrows = control_plane_store.list_licenses(tenant_id=cand_tenant) or []
                    lrow = next(
                        (
                            r for r in lrows
                            if str((r or {}).get("license_id") or "").strip() == linked_license_id
                        ),
                        None,
                    )
                    c_customer = str((lrow or {}).get("customer_id") or "").strip()
                    if not c_customer:
                        continue
                    edge_rows = control_plane_store.list_edges(tenant_id=cand_tenant) or []
                    edge_match = next(
                        (
                            e for e in edge_rows
                            if str((e or {}).get("edge_id") or "").strip() == linked_edge_id
                        ),
                        None,
                    )
                    control_plane_store.upsert_edge(
                        tenant_id=cand_tenant,
                        edge_id=linked_edge_id,
                        edge_name=str((edge_match or {}).get("edge_name") or linked_edge_id),
                        customer_id=c_customer,
                        site=str((edge_match or {}).get("site") or ""),
                        area=str((edge_match or {}).get("area") or ""),
                        equipment=str((edge_match or {}).get("equipment") or ""),
                        status=str((edge_match or {}).get("status") or "active"),
                        metadata={
                            "source": "edge_license_check_local_customer_recovery",
                            "license_id": linked_license_id,
                        },
                    )
                    out = control_plane_store.check_edge_license(tenant_id=cand_tenant, edge_id=linked_edge_id)
                    resolved_tenant = str(out.get("resolved_tenant_id") or cand_tenant)
                    if bool(out.get("ok")):
                        break
                except Exception:
                    continue
        local_license = dict(out.get("license") or {}) if isinstance(out.get("license"), dict) else {}
        local_modules = local_license.get("modules") if isinstance(local_license, dict) else []
        local_license_id = str(local_license.get("license_id") or linked_license_id or "").strip()
        local_missing_details = (
            (not bool(out.get("ok")))
            or (not str(local_license.get("start_utc") or "").strip())
            or (not str(local_license.get("end_utc") or "").strip())
            or (not isinstance(local_modules, list))
            or (isinstance(local_modules, list) and len(local_modules) == 0)
        )
        if should_try_cloud and local_missing_details:
            fwd_headers: dict[str, str] = {}
            auth = str(request.headers.get("authorization") or "").strip()
            if auth:
                fwd_headers["Authorization"] = auth
            if resolved_tenant:
                fwd_headers["X-Tenant-Id"] = resolved_tenant

            def _cloud_get(url: str, *, timeout: int = 8) -> requests.Response:
                # Try forwarded auth first.
                resp = requests.get(url, timeout=timeout, headers=fwd_headers or None)
                if resp.status_code != 401:
                    return resp
                # Local JWT may not match cloud JWT secret; obtain cloud token and retry.
                try:
                    cloud_user = str(
                        os.getenv("TRUSTNODE_CLOUD_AUTH_USER")
                        or app_settings.get("cloud_auth_user")
                        or "admin"
                    ).strip()
                    cloud_pass = str(
                        os.getenv("TRUSTNODE_CLOUD_AUTH_PASSWORD")
                        or app_settings.get("cloud_auth_password")
                        or "admin"
                    ).strip()
                    if not cloud_user or not cloud_pass:
                        return resp
                    login = requests.post(
                        f"{cloud_url}/api/auth/login",
                        json={"username": cloud_user, "password": cloud_pass},
                        timeout=timeout,
                    )
                    if login.status_code >= 400:
                        return resp
                    body = login.json() if login.content else {}
                    token = str(body.get("access_token") or body.get("token") or "").strip()
                    if not token:
                        return resp
                    retry_headers = dict(fwd_headers or {})
                    retry_headers["Authorization"] = f"Bearer {token}"
                    return requests.get(url, timeout=timeout, headers=retry_headers)
                except Exception:
                    return resp

            # First try the dedicated cloud license-check by edge_id.
            params = []
            if check_edge_id:
                params.append(f"edge_id={requests.utils.quote(str(check_edge_id), safe='')}")
            if resolved_tenant:
                params.append(f"tenant_id={requests.utils.quote(str(resolved_tenant), safe='')}")
            qs = f"?{'&'.join(params)}" if params else ""
            url = f"{cloud_url}/api/control-plane/edge-link/license-check{qs}"
            r = _cloud_get(url, timeout=8)
            if r.status_code < 400:
                cloud = r.json() if r.content else {}
                if isinstance(cloud, dict):
                    cloud_ok = bool(cloud.get("ok"))
                    cloud_license = cloud.get("license") if isinstance(cloud.get("license"), dict) else {}
                    cloud_modules = cloud_license.get("modules") if isinstance(cloud_license, dict) else []
                    if cloud_ok or (isinstance(cloud_modules, list) and cloud_modules):
                        # Mirror authoritative cloud license scope locally.
                        c_tenant = normalize_tenant_id(str(cloud.get("tenant_id") or resolved_tenant or tid))
                        c_edge = cloud.get("edge") if isinstance(cloud.get("edge"), dict) else {}
                        c_edge_id = str(c_edge.get("edge_id") or check_edge_id or "").strip()
                        c_customer = str(c_edge.get("customer_id") or "").strip()
                        c_license_id = str(cloud_license.get("license_id") or "").strip()
                        if c_customer:
                            control_plane_store.upsert_customer(
                                tenant_id=c_tenant,
                                customer_id=c_customer,
                                company_name=c_customer,
                                contact_email="",
                                status="active",
                                metadata={"source": "cloud_license_check_hydrate"},
                            )
                        if c_license_id:
                            control_plane_store.upsert_license(
                                tenant_id=c_tenant,
                                license_id=c_license_id,
                                customer_id=c_customer,
                                plan_code=str(cloud_license.get("plan_code") or "standard"),
                                status=str(cloud_license.get("status") or "active"),
                                start_utc=str(cloud_license.get("start_utc") or ""),
                                end_utc=str(cloud_license.get("end_utc") or ""),
                                max_edges=max(0, int(cloud_license.get("max_edges") or 0)),
                                max_users=max(0, int(cloud_license.get("max_users") or 0)),
                                metadata={"source": "cloud_license_check_hydrate"},
                            )
                            if isinstance(cloud_modules, list):
                                control_plane_store.set_license_modules(
                                    license_id=c_license_id,
                                    modules=list(cloud_modules or []),
                                )
                        if c_edge_id:
                            control_plane_store.upsert_edge(
                                tenant_id=c_tenant,
                                edge_id=c_edge_id,
                                edge_name=str(c_edge.get("edge_name") or c_edge_id),
                                customer_id=c_customer,
                                site=str(c_edge.get("site") or ""),
                                area=str(c_edge.get("area") or ""),
                                equipment=str(c_edge.get("equipment") or ""),
                                status=str(c_edge.get("status") or "active"),
                                metadata={"source": "cloud_license_check_hydrate", "license_id": c_license_id},
                            )
                        out = control_plane_store.check_edge_license(tenant_id=c_tenant, edge_id=c_edge_id or check_edge_id)
                        resolved_tenant = str(out.get("resolved_tenant_id") or c_tenant)

            # Last-mile recovery for legacy scopes: if still missing details, hydrate directly
            # from cloud license endpoints using known license_id from local state.
            post_license = dict(out.get("license") or {}) if isinstance(out.get("license"), dict) else {}
            post_modules = post_license.get("modules") if isinstance(post_license, dict) else []
            still_missing = (
                (not bool(out.get("ok")))
                or (not str(post_license.get("start_utc") or "").strip())
                or (not str(post_license.get("end_utc") or "").strip())
                or (not isinstance(post_modules, list))
                or (isinstance(post_modules, list) and len(post_modules) == 0)
            )
            recover_license_id = str(post_license.get("license_id") or local_license_id or "").strip()
            if still_missing and recover_license_id:
                lic_list_url = f"{cloud_url}/api/control-plane/licenses?tenant_id={requests.utils.quote(str(resolved_tenant or tid), safe='')}"
                mod_url = f"{cloud_url}/api/control-plane/licenses/{requests.utils.quote(recover_license_id, safe='')}/modules"
                lic_res = _cloud_get(lic_list_url, timeout=8)
                mod_res = _cloud_get(mod_url, timeout=8)
                if lic_res.status_code < 400:
                    lic_payload = lic_res.json() if lic_res.content else {}
                    rows = lic_payload.get("rows") if isinstance(lic_payload, dict) and isinstance(lic_payload.get("rows"), list) else []
                    cloud_license = next(
                        (
                            r for r in rows
                            if str((r or {}).get("license_id") or "").strip() == recover_license_id
                        ),
                        {},
                    )
                    cloud_modules = []
                    if mod_res.status_code < 400:
                        mod_payload = mod_res.json() if mod_res.content else {}
                        if isinstance(mod_payload, dict) and isinstance(mod_payload.get("rows"), list):
                            cloud_modules = list(mod_payload.get("rows") or [])
                    if isinstance(cloud_license, dict) and cloud_license:
                        c_tenant = normalize_tenant_id(str(cloud_license.get("tenant_id") or resolved_tenant or tid))
                        c_customer = str(cloud_license.get("customer_id") or "").strip()
                        if c_customer:
                            control_plane_store.upsert_customer(
                                tenant_id=c_tenant,
                                customer_id=c_customer,
                                company_name=c_customer,
                                contact_email="",
                                status="active",
                                metadata={"source": "cloud_license_direct_hydrate"},
                            )
                        control_plane_store.upsert_license(
                            tenant_id=c_tenant,
                            license_id=str(cloud_license.get("license_id") or recover_license_id),
                            customer_id=c_customer,
                            plan_code=str(cloud_license.get("plan_code") or "standard"),
                            status=str(cloud_license.get("status") or "active"),
                            start_utc=str(cloud_license.get("start_utc") or ""),
                            end_utc=str(cloud_license.get("end_utc") or ""),
                            max_edges=max(0, int(cloud_license.get("max_edges") or 0)),
                            max_users=max(0, int(cloud_license.get("max_users") or 0)),
                            metadata={"source": "cloud_license_direct_hydrate"},
                        )
                        if isinstance(cloud_modules, list) and cloud_modules:
                            control_plane_store.set_license_modules(
                                license_id=str(cloud_license.get("license_id") or recover_license_id),
                                modules=cloud_modules,
                            )
                        if check_edge_id and c_customer:
                            try:
                                existing_edge = control_plane_store.get_edge(tenant_id=c_tenant, edge_id=check_edge_id) or {}
                                control_plane_store.upsert_edge(
                                    tenant_id=c_tenant,
                                    edge_id=check_edge_id,
                                    edge_name=str(existing_edge.get("edge_name") or check_edge_id),
                                    customer_id=c_customer,
                                    site=str(existing_edge.get("site") or ""),
                                    area=str(existing_edge.get("area") or ""),
                                    equipment=str(existing_edge.get("equipment") or ""),
                                    status=str(existing_edge.get("status") or "active"),
                                    metadata={
                                        **(existing_edge.get("metadata") if isinstance(existing_edge.get("metadata"), dict) else {}),
                                        "license_id": str(cloud_license.get("license_id") or recover_license_id),
                                        "source": "cloud_license_direct_hydrate",
                                    },
                                )
                            except Exception:
                                pass
                        out = control_plane_store.check_edge_license(tenant_id=c_tenant, edge_id=check_edge_id)
                        resolved_tenant = str(out.get("resolved_tenant_id") or c_tenant)
    except Exception:
        pass

    # Secondary recovery path for legacy/partial links:
    # resolve full license details by app_settings.license_id and persist locally.
    try:
        bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
        app_settings = dict(bootstrap.get("app_settings") or {})
        linked_license_id = str(app_settings.get("license_id") or "").strip()
        linked_edge_id = str(app_settings.get("edge_id") or check_edge_id or "").strip()
        linked_tenant_id = normalize_tenant_id(str(app_settings.get("tenant_id") or resolved_tenant or tid))
        if linked_license_id and (
            not bool(out.get("ok"))
            or not str((out.get("license") or {}).get("start_utc") if isinstance(out.get("license"), dict) else "").strip()
            or not str((out.get("license") or {}).get("end_utc") if isinstance(out.get("license"), dict) else "").strip()
            or not isinstance(((out.get("license") or {}) if isinstance(out.get("license"), dict) else {}).get("modules"), list)
        ):
            cloud_url = str(app_settings.get("cloud_url") or app_settings.get("cloud_api_url") or "").strip().rstrip("/")
            if cloud_url and not _is_same_origin_as_request(cloud_url, request):
                auth = str(request.headers.get("authorization") or "").strip()
                fwd_headers: dict[str, str] = {"Content-Type": "application/json"}
                if auth:
                    fwd_headers["Authorization"] = auth
                def _cloud_get_recover(url: str, *, timeout: int = 15) -> requests.Response:
                    resp = requests.get(url, headers=fwd_headers, timeout=timeout)
                    if resp.status_code != 401:
                        return resp
                    try:
                        cloud_user = str(
                            os.getenv("TRUSTNODE_CLOUD_AUTH_USER")
                            or app_settings.get("cloud_auth_user")
                            or "admin"
                        ).strip()
                        cloud_pass = str(
                            os.getenv("TRUSTNODE_CLOUD_AUTH_PASSWORD")
                            or app_settings.get("cloud_auth_password")
                            or "admin"
                        ).strip()
                        if not cloud_user or not cloud_pass:
                            return resp
                        login = requests.post(
                            f"{cloud_url}/api/auth/login",
                            json={"username": cloud_user, "password": cloud_pass},
                            timeout=timeout,
                        )
                        if login.status_code >= 400:
                            return resp
                        body = login.json() if login.content else {}
                        token = str(body.get("access_token") or body.get("token") or "").strip()
                        if not token:
                            return resp
                        rh = dict(fwd_headers)
                        rh["Authorization"] = f"Bearer {token}"
                        return requests.get(url, headers=rh, timeout=timeout)
                    except Exception:
                        return resp
                tenant_candidates = []
                for cand in [linked_tenant_id, resolved_tenant, tid, "default"]:
                    c = normalize_tenant_id(str(cand or "default"))
                    if c and c not in tenant_candidates:
                        tenant_candidates.append(c)
                cloud_license: dict[str, Any] | None = None
                cloud_modules: list[dict[str, Any]] = []
                hit_tenant = linked_tenant_id
                for cand_tenant in tenant_candidates:
                    lr = _cloud_get_recover(
                        f"{cloud_url}/api/control-plane/licenses?tenant_id={requests.utils.quote(cand_tenant, safe='')}",
                        timeout=15,
                    )
                    if not (200 <= lr.status_code < 300):
                        continue
                    rows = (lr.json() or {}).get("rows") if lr.text else []
                    if not isinstance(rows, list):
                        continue
                    hit = next((r for r in rows if str((r or {}).get("license_id") or "").strip() == linked_license_id), None)
                    if isinstance(hit, dict):
                        cloud_license = dict(hit)
                        hit_tenant = cand_tenant
                        break
                if cloud_license:
                    mr = _cloud_get_recover(
                        f"{cloud_url}/api/control-plane/licenses/{requests.utils.quote(linked_license_id, safe='')}/modules",
                        timeout=15,
                    )
                    if 200 <= mr.status_code < 300:
                        mrows = (mr.json() or {}).get("rows") if mr.text else []
                        if isinstance(mrows, list):
                            cloud_modules = list(mrows)
                    resolved_customer = str(cloud_license.get("customer_id") or "").strip()
                    control_plane_store.upsert_license(
                        tenant_id=hit_tenant,
                        license_id=linked_license_id,
                        customer_id=resolved_customer,
                        plan_code=str(cloud_license.get("plan_code") or "standard"),
                        status=str(cloud_license.get("status") or "active"),
                        start_utc=str(cloud_license.get("start_utc") or ""),
                        end_utc=str(cloud_license.get("end_utc") or ""),
                        max_edges=max(0, int(cloud_license.get("max_edges") or 0)),
                        max_users=max(0, int(cloud_license.get("max_users") or 0)),
                        metadata={"source": "edge_license_check_linked_license_recovery"},
                    )
                    if cloud_modules:
                        control_plane_store.set_license_modules(
                            license_id=linked_license_id,
                            modules=cloud_modules,
                        )
                    if linked_edge_id:
                        edge_rows = control_plane_store.list_edges(tenant_id=hit_tenant) or []
                        edge_match = next((e for e in edge_rows if str(e.get("edge_id") or "").strip() == linked_edge_id), None)
                        if edge_match:
                            control_plane_store.upsert_edge(
                                tenant_id=hit_tenant,
                                edge_id=linked_edge_id,
                                edge_name=str(edge_match.get("edge_name") or linked_edge_id),
                                customer_id=resolved_customer or str(edge_match.get("customer_id") or "").strip(),
                                site=str(edge_match.get("site") or ""),
                                area=str(edge_match.get("area") or ""),
                                equipment=str(edge_match.get("equipment") or ""),
                                status=str(edge_match.get("status") or "active"),
                                metadata={"source": "edge_license_check_linked_license_recovery", "license_id": linked_license_id},
                            )
                    out = control_plane_store.check_edge_license(tenant_id=hit_tenant, edge_id=linked_edge_id or check_edge_id)
                    resolved_tenant = str(out.get("resolved_tenant_id") or hit_tenant)
    except Exception:
        pass

    resolved_edge_id = str(
        (
            (out.get("edge") or {}).get("edge_id")
            if isinstance(out.get("edge"), dict)
            else ""
        )
        or check_edge_id
        or ""
    ).strip()
    # Operator 2026-06-18: grandfather features the customer was already
    # running BEFORE the new license-module gate landed. On first boot
    # after the EXE upgrade, app_settings.grandfathered_modules captures
    # the currently-in-use connection/LAN feature keys. The frontend
    # treats these as licensed even if the license doesn't include them.
    grandfathered: list[str] = []
    try:
        bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
        s = bootstrap.get("app_settings") or {}
        if isinstance(s, dict):
            existing = s.get("grandfathered_modules")
            if isinstance(existing, list):
                grandfathered = [str(k).strip().lower() for k in existing if str(k).strip()]
            else:
                # First-boot capture: any feature toggled on or running RIGHT NOW
                # gets grandfathered. Persist so next boots are stable.
                derived: list[str] = []
                if bool(s.get("lan_sharing_enabled")):
                    derived.append("lan_access")
                conn = s.get("connections") if isinstance(s.get("connections"), dict) else {}
                if isinstance(conn.get("opcua"), dict) and bool(conn["opcua"].get("enabled")):
                    derived.append("opcua")
                if isinstance(conn.get("mqtt"), dict) and bool(conn["mqtt"].get("enabled")):
                    derived.append("mqtt")
                # Persist even if empty so the field exists (subsequent boots
                # skip the derivation branch and trust the persisted list).
                try:
                    s_new = dict(s)
                    s_new["grandfathered_modules"] = derived
                    app_store.upsert_domain("app_settings", s_new, actor="license_grandfather")
                    grandfathered = derived
                except Exception:
                    grandfathered = derived
    except Exception:
        pass
    # Operator 2026-06-18: SIGN the license payload server-side IF this
    # backend is running on the dev portal VPS (TRUSTNODE_LICENSE_SIGNING_PRIVATE_PEM
    # env var present). The customer's tray then verifies the signature
    # on every boot with the bundled public key. No private key on the
    # customer machine, no forging.
    try:
        import os as _os
        private_pem_str = _os.environ.get("TRUSTNODE_LICENSE_SIGNING_PRIVATE_PEM", "").strip()
        if private_pem_str and isinstance(out.get("license"), dict):
            from app.services.license_signature import sign_license_payload
            lic = dict(out["license"])
            # Strip any old signature before re-signing so the canonical
            # payload reflects the current modules + dates.
            lic.pop("signature", None)
            try:
                # If license has no "modules" yet (sometimes it's a separate
                # query), attach the latest from the store so the signature
                # actually covers what the customer will see.
                mod_rows = control_plane_store.list_license_modules(
                    license_id=str(lic.get("license_id") or ""),
                )
                lic["modules"] = [m for m in (mod_rows or []) if m.get("enabled")]
            except Exception:
                pass
            out["license"] = sign_license_payload(lic, private_pem_str.encode("utf-8"))
    except Exception as _exc:
        logger.warning("license_signature: server-side signing failed: %s", _exc)

    # Now verify on the response — the tray does the same check on its
    # end with the bundled public key. Verifying here too gives the dev
    # portal a sanity check.
    sig_status = {"verified": False, "reason": "not checked"}
    try:
        from app.services.license_signature import verify_license_signature
        # The license dict lives at out["license"] when out["ok"] is True.
        license_payload = out.get("license") if isinstance(out.get("license"), dict) else None
        sig_status = verify_license_signature(license_payload)
        # Persist last successful verification for the offline-grace
        # window. Used by the UI to decide whether to keep features open
        # when the tray boots offline.
        if sig_status.get("verified"):
            try:
                bs = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
                s2 = dict(bs.get("app_settings") or {})
                from datetime import datetime, timezone
                s2["license_last_verified_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                # Phase 3b: persist the latest signature status so the
                # license_gate has it without re-verifying.
                s2["license_signature_status"] = dict(sig_status)
                app_store.upsert_domain("app_settings", s2, actor="license_verifier")
            except Exception:
                pass
        else:
            # Phase 3c (operator 2026-06-18): TAMPER ALERT.
            # Signature was supplied but failed verification. Could be
            # someone editing the local SQLite to flip license flags,
            # someone moving a license file between machines, or a
            # corrupted boot. Either way it's an event the operator
            # MUST see in the logs.
            if sig_status.get("signature_present"):
                try:
                    from datetime import datetime, timezone
                    app_store.append_log_rows([{
                        "ts_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "level": "error",
                        "category": "license_tamper",
                        "message": f"License signature INVALID — reason={sig_status.get('reason') or 'unknown'}. "
                                   f"Premium modules locked until a valid license is re-issued.",
                        "gateway_id": "",
                        "gateway_name": "",
                        "device_name": "",
                        "database_name": "License",
                    }])
                except Exception:
                    pass
                # Also persist the bad status so license_gate can block writes.
                try:
                    bs = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
                    s2 = dict(bs.get("app_settings") or {})
                    s2["license_signature_status"] = dict(sig_status)
                    app_store.upsert_domain("app_settings", s2, actor="license_verifier")
                except Exception:
                    pass
    except Exception as exc:
        sig_status = {"verified": False, "reason": f"verifier error: {type(exc).__name__}"}

    return {
        "ok": bool(out.get("ok")),
        "tenant_id": resolved_tenant,
        "edge_id": resolved_edge_id,
        "grandfathered_modules": grandfathered,
        "license_signature": sig_status,
        **out,
    }


# ---------------------------------------------------------------------------
# Dashboard profile management (portal admin)
# ---------------------------------------------------------------------------
#
# The portal needs to manage the dashboard_configurations rows that live in
# Supabase — operators sometimes accumulate stale profiles (renamed edges,
# deactivated users) that clutter the Lite picker. We talk to Supabase via
# PostgREST (HTTPS REST) using the service-role key instead of opening a
# direct Postgres connection, because the VPS hits Supabase's session
# pooler (port 5432) and intermittently times out under load. The rest of
# this router already uses the same HTTPS path for cp_* tables.
#
# The edge's local SQLite is also cleaned up so the next mirror reconcile
# won't resurrect the deleted row.

class _TrialStartPayload(BaseModel):
    edge_id: str = ""
    trial_kind: str = ""
    actor: str = ""
    metadata: dict[str, Any] = {}


@router.post("/edge-link/trial/start")
def edge_link_trial_start(request: Request, payload: _TrialStartPayload, tenant_id: str | None = None) -> dict[str, Any]:
    """Issue an emergency-trial grant for a license that has expired.

    The edge calls this when the operator clicks "Start 2-Hour Trial" or
    "Renew (1 Hour)". The cloud is authoritative: a fresh edge probe AFTER
    this call will see the trial as active and return ok=True so the UI
    unlocks. Each edge may use each trial kind ONCE per license; the
    control plane enforces that via the unique index on
    cp_trial_grants(license_id, edge_id, trial_kind).
    """
    tid = _scoped_tenant(request, tenant_id)
    eid = str(payload.edge_id or "").strip()
    if not eid:
        bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
        app_settings = dict(bootstrap.get("app_settings") or {})
        eid = str(app_settings.get("edge_id") or "").strip()
    if not eid:
        return {"ok": False, "reason": "edge_id_required"}

    # Resolve the license_id from the current edge_license_check so the
    # operator can't accidentally start a trial against the wrong license.
    check = control_plane_store.check_edge_license(tenant_id=tid, edge_id=eid)
    lic = dict(check.get("license") or {}) if isinstance(check.get("license"), dict) else {}
    license_id = str(lic.get("license_id") or "").strip()
    if not license_id:
        return {"ok": False, "reason": "license_not_found"}
    actor = str(payload.actor or "").strip()
    if not actor:
        try:
            actor = str(request.state.user.get("username") or "") if hasattr(request, "state") and getattr(request.state, "user", None) else ""
        except Exception:
            actor = ""
    # Try the cloud first so the portal sees the trial event in
    # near-real-time. If the cloud is unreachable, fall back to the
    # local SQLite write so the edge still unlocks for the operator
    # mid-shift (machinery uptime trumps audit completeness; the
    # next successful mirror will sync the row up).
    cloud_ok = False
    try:
        bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
        app_settings = dict(bootstrap.get("app_settings") or {})
        cloud_url = str(app_settings.get("cloud_url") or app_settings.get("cloud_api_url") or "").strip().rstrip("/")
        if cloud_url and not _is_same_origin_as_request(cloud_url, request):
            fwd_headers = {"X-Tenant-Id": tid, "Content-Type": "application/json"}
            auth = str(request.headers.get("authorization") or "").strip()
            if auth:
                fwd_headers["Authorization"] = auth
            cloud_body = {
                "edge_id": eid,
                "trial_kind": str(payload.trial_kind or "").strip(),
                "actor": actor or "edge_operator",
                "metadata": dict(payload.metadata or {}),
            }
            resp = requests.post(
                f"{cloud_url}/api/control-plane/edge-link/trial/start",
                json=cloud_body,
                headers=fwd_headers,
                timeout=8,
            )
            if resp.status_code < 400:
                cloud_ok = True
                try:
                    return resp.json()
                except Exception:
                    pass
    except Exception:
        cloud_ok = False
    out = control_plane_store.start_trial(
        tenant_id=tid,
        license_id=license_id,
        edge_id=eid,
        trial_kind=str(payload.trial_kind or "").strip(),
        granted_by=actor or "edge_operator",
        source="edge_app" if not cloud_ok else "edge_app_cloud_fallback",
        metadata=dict(payload.metadata or {}),
    )
    if not out.get("ok"):
        return out
    # Audit trail for the portal.
    try:
        control_plane_store.audit(
            actor_type="edge_operator",
            actor_id=actor or "edge_operator",
            tenant_id=tid,
            action="trial_started",
            outcome="ok",
            correlation_id=eid,
            details={
                "license_id": license_id,
                "edge_id": eid,
                "trial_kind": str(payload.trial_kind or ""),
                "expires_utc": str((out.get("grant") or {}).get("expires_utc") or ""),
            },
        )
    except Exception:
        pass
    # Re-run the license check so the response carries the same shape
    # the edge already knows how to parse (`license`, `trial`,
    # `trial_eligibility`). The edge can store this verbatim as its
    # licenseSnapshot.
    refreshed = control_plane_store.check_edge_license(tenant_id=tid, edge_id=eid)
    refreshed["trial_grant"] = out.get("grant")
    return refreshed


@router.get("/edge-link/trial/history")
def edge_link_trial_history(request: Request, edge_id: str = "", license_id: str = "", tenant_id: str | None = None, limit: int = 200) -> dict[str, Any]:
    """List trial grants for the portal/edge UI. Operator sees their own
    edge's history; admin sees their whole tenant or filters by edge."""
    tid = _scoped_tenant(request, tenant_id)
    eid = str(edge_id or "").strip()
    lid = str(license_id or "").strip()
    rows = control_plane_store.list_trial_grants_for_tenant(
        tenant_id=tid,
        edge_id=eid or None,
        license_id=lid or None,
        limit=int(limit or 200),
    )
    return {"ok": True, "tenant_id": tid, "rows": rows}


def _supabase_rest_target() -> tuple[str, str, str] | None:
    """Return (url, service_key, schema) for Supabase PostgREST, or None
    when this backend isn't configured to talk to Supabase (e.g. an
    isolated edge without cloud credentials)."""
    base = str(os.environ.get("TRUSTNODE_SUPABASE_URL") or "").strip().rstrip("/")
    key = str(os.environ.get("TRUSTNODE_SUPABASE_SERVICE_KEY") or "").strip()
    if not base or not key:
        return None
    schema = str(os.environ.get("TRUSTNODE_CLOUD_DB_SCHEMA") or "public").strip() or "public"
    return base, key, schema


def _supabase_rest_headers(key: str, schema: str) -> dict[str, str]:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Accept-Profile": schema,
        "Content-Profile": schema,
    }


@router.get("/dashboard-profiles")
def list_dashboard_profiles(request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    """List dashboard_configurations rows from the Supabase mirror.
    Master admin sees all tenants; tenant admin sees only their tenant.
    Uses PostgREST so we never block on the Supabase Postgres pooler."""
    tid = _scoped_tenant(request, tenant_id)
    target = _supabase_rest_target()
    if not target:
        return {"ok": True, "tenant_id": tid, "rows": []}
    base, key, schema = target
    is_master = (tid == "default" and not (tenant_id or "").strip())
    # PostgREST query: select the columns we render + a JSON path expression
    # that lets us count widgets without round-tripping the whole payload.
    params: dict[str, str] = {
        "select": "tenant_id,scope_key,version,updated_utc,payload_json",
        "order": "updated_utc.desc",
    }
    if not is_master:
        params["tenant_id"] = f"eq.{tid}"
    url = f"{base}/rest/v1/dashboard_configurations"
    try:
        resp = requests.get(
            url,
            headers=_supabase_rest_headers(key, schema),
            params=params,
            timeout=8,
        )
    except requests.exceptions.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"cloud_query_failed: {exc}")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"cloud_query_failed: HTTP {resp.status_code} {resp.text[:200]}")
    try:
        data = resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"cloud_query_failed: {exc}")
    out: list[dict[str, Any]] = []
    for r in data or []:
        payload = r.get("payload_json") if isinstance(r, dict) else None
        widgets = []
        if isinstance(payload, dict):
            w = payload.get("widgets")
            if isinstance(w, list):
                widgets = w
        out.append({
            "tenant_id": str(r.get("tenant_id") or ""),
            "scope_key": str(r.get("scope_key") or ""),
            "version": int(r.get("version") or 0),
            "updated_utc": str(r.get("updated_utc") or ""),
            "widget_count": len(widgets),
        })
    return {"ok": True, "tenant_id": tid, "rows": out}


class DashboardProfileDeleteRequest(BaseModel):
    tenant_id: str = ""
    scope_key: str


@router.post("/dashboard-profiles/delete")
def delete_dashboard_profile(request: Request, payload: DashboardProfileDeleteRequest) -> dict[str, Any]:
    """Delete a dashboard profile row from the Supabase mirror, then drop
    the matching local row on the edge so the next mirror reconcile won't
    re-publish it. Master admin can delete any tenant; tenant admin only
    their own. Uses PostgREST (no Postgres pooler dependency)."""
    tid = _scoped_tenant(request, payload.tenant_id, require_admin_write=True)
    skey = str(payload.scope_key or "").strip()
    if not skey:
        raise HTTPException(status_code=400, detail="scope_key_required")
    target_tenant = str(payload.tenant_id or tid).strip() or tid
    cloud_deleted = 0
    target = _supabase_rest_target()
    if target:
        base, key, schema = target
        url = f"{base}/rest/v1/dashboard_configurations"
        try:
            resp = requests.delete(
                url,
                headers={**_supabase_rest_headers(key, schema), "Prefer": "return=representation"},
                params={
                    "tenant_id": f"eq.{target_tenant}",
                    "scope_key": f"eq.{skey}",
                },
                timeout=8,
            )
        except requests.exceptions.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"cloud_delete_failed: {exc}")
        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"cloud_delete_failed: HTTP {resp.status_code} {resp.text[:200]}")
        try:
            cloud_deleted = len(resp.json() or [])
        except Exception:
            cloud_deleted = 0
    # Local delete so the edge stops re-mirroring it. Best-effort.
    local_deleted = 0
    try:
        with app_store._lock:
            with app_store._connect() as conn:
                result = conn.execute(
                    "DELETE FROM config_documents_scoped WHERE scope_key = ? AND domain = ?",
                    (skey, "dashboard_configurations"),
                )
                local_deleted = int(result.rowcount or 0)
    except Exception:
        pass
    _audit(
        request,
        tenant_id=target_tenant,
        action="dashboard_profile.delete",
        outcome="ok" if (cloud_deleted or local_deleted) else "not_found",
        details={"scope_key": skey, "cloud_deleted": cloud_deleted, "local_deleted": local_deleted},
    )
    return {
        "ok": True,
        "tenant_id": target_tenant,
        "scope_key": skey,
        "cloud_deleted": cloud_deleted,
        "local_deleted": local_deleted,
    }
