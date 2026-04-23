from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.state import control_plane_store
from app.tenant import get_current_tenant, normalize_tenant_id

router = APIRouter(prefix="/api/control-plane", tags=["control-plane"])


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
    username: str
    password: str | None = None
    role: str = "viewer"
    status: str = "active"
    email: str = ""
    mfa_enabled: bool = False
    modules: list[str] = Field(default_factory=list)
    permissions: dict[str, Any] = Field(default_factory=dict)


class ActivationCodeIssueRequest(BaseModel):
    customer_id: str = ""
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


class PasswordResetIssueRequest(BaseModel):
    username: str
    ttl_minutes: int = 15


class PasswordResetApplyRequest(BaseModel):
    username: str
    reset_token: str
    new_password: str


def _tenant_or_current(tenant_id: str | None) -> str:
    return normalize_tenant_id(tenant_id or get_current_tenant())


@router.get("/modules")
def list_module_catalog() -> dict[str, Any]:
    return {"ok": True, "modules": control_plane_store.module_catalog()}


@router.get("/tenants")
def list_tenants(include_suspended: bool = True) -> dict[str, Any]:
    return {"ok": True, "rows": control_plane_store.list_tenants(include_suspended=include_suspended)}


@router.post("/tenants")
def upsert_tenant(payload: TenantUpsertRequest) -> dict[str, Any]:
    row = control_plane_store.upsert_tenant(
        tenant_id=payload.tenant_id,
        name=payload.name,
        status=payload.status,
        primary_domain=payload.primary_domain,
        timezone_name=payload.timezone,
        metadata=payload.metadata,
    )
    return {"ok": True, "row": row}


@router.get("/customers")
def list_customers(tenant_id: str | None = None) -> dict[str, Any]:
    tid = _tenant_or_current(tenant_id)
    return {"ok": True, "tenant_id": tid, "rows": control_plane_store.list_customers(tenant_id=tid)}


@router.post("/customers")
def upsert_customer(payload: CustomerUpsertRequest, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _tenant_or_current(tenant_id)
    row = control_plane_store.upsert_customer(
        tenant_id=tid,
        customer_id=payload.customer_id,
        company_name=payload.company_name,
        contact_email=payload.contact_email,
        status=payload.status,
        metadata=payload.metadata,
    )
    return {"ok": True, "tenant_id": tid, "row": row}


@router.get("/edges")
def list_edges(tenant_id: str | None = None) -> dict[str, Any]:
    tid = _tenant_or_current(tenant_id)
    return {"ok": True, "tenant_id": tid, "rows": control_plane_store.list_edges(tenant_id=tid)}


@router.post("/edges")
def upsert_edge(payload: EdgeUpsertRequest, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _tenant_or_current(tenant_id)
    row = control_plane_store.upsert_edge(
        tenant_id=tid,
        edge_id=payload.edge_id,
        edge_name=payload.edge_name,
        customer_id=payload.customer_id,
        site=payload.site,
        area=payload.area,
        equipment=payload.equipment,
        status=payload.status,
        metadata=payload.metadata,
    )
    return {"ok": True, "tenant_id": tid, "row": row}


@router.post("/edges/heartbeat")
def heartbeat_edge(edge_id: str, payload: dict[str, Any] | None = None, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _tenant_or_current(tenant_id)
    row = control_plane_store.heartbeat_edge(tenant_id=tid, edge_id=edge_id, payload=payload or {})
    if not row:
        raise HTTPException(status_code=404, detail="edge_not_found")
    return {"ok": True, "tenant_id": tid, "row": row}


@router.get("/licenses")
def list_licenses(tenant_id: str | None = None) -> dict[str, Any]:
    tid = _tenant_or_current(tenant_id)
    return {"ok": True, "tenant_id": tid, "rows": control_plane_store.list_licenses(tenant_id=tid)}


@router.post("/licenses")
def upsert_license(payload: LicenseUpsertRequest, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _tenant_or_current(tenant_id)
    row = control_plane_store.upsert_license(
        tenant_id=tid,
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
    return {"ok": True, "tenant_id": tid, "row": row}


@router.get("/licenses/{license_id}/modules")
def list_license_modules(license_id: str) -> dict[str, Any]:
    return {"ok": True, "license_id": license_id, "rows": control_plane_store.list_license_modules(license_id=license_id)}


@router.put("/licenses/{license_id}/modules")
def set_license_modules(license_id: str, payload: LicenseModulesRequest) -> dict[str, Any]:
    return {"ok": True, **control_plane_store.set_license_modules(license_id=license_id, modules=payload.modules)}


@router.get("/users")
def list_users(tenant_id: str | None = None) -> dict[str, Any]:
    tid = _tenant_or_current(tenant_id)
    return {"ok": True, "tenant_id": tid, "rows": control_plane_store.list_users(tenant_id=tid)}


@router.post("/users")
def upsert_user(payload: UserUpsertRequest, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _tenant_or_current(tenant_id)
    row = control_plane_store.upsert_user(
        tenant_id=tid,
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
    return {"ok": True, "tenant_id": tid, "row": row}


@router.delete("/users/{username}")
def delete_user(username: str, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _tenant_or_current(tenant_id)
    if str(username or "").strip().lower() == "admin":
        raise HTTPException(status_code=400, detail="builtin_admin_cannot_be_deleted")
    deleted = control_plane_store.delete_user(tenant_id=tid, username=username)
    if not deleted:
        raise HTTPException(status_code=404, detail="user_not_found")
    return {"ok": True, "tenant_id": tid, "username": username}


@router.post("/activation-code/issue")
def issue_activation_code(payload: ActivationCodeIssueRequest, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _tenant_or_current(tenant_id)
    row = control_plane_store.issue_activation_code(
        tenant_id=tid,
        customer_id=payload.customer_id,
        edge_name=payload.edge_name,
        ttl_minutes=payload.ttl_minutes,
        metadata=payload.metadata,
    )
    return {"ok": True, "tenant_id": tid, "row": row}


@router.post("/activation-code/apply")
def apply_activation_code(payload: ActivationCodeApplyRequest) -> dict[str, Any]:
    row = control_plane_store.activate_edge_with_code(
        activation_code=payload.activation_code,
        edge_id=payload.edge_id,
        edge_name=payload.edge_name,
        site=payload.site,
        area=payload.area,
        equipment=payload.equipment,
    )
    return {"ok": True, "row": row}


@router.post("/password-reset/issue")
def issue_password_reset(payload: PasswordResetIssueRequest, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _tenant_or_current(tenant_id)
    row = control_plane_store.issue_password_reset(
        tenant_id=tid,
        username=payload.username,
        ttl_minutes=payload.ttl_minutes,
    )
    return {"ok": True, "tenant_id": tid, "row": row}


@router.post("/password-reset/apply")
def apply_password_reset(payload: PasswordResetApplyRequest, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _tenant_or_current(tenant_id)
    row = control_plane_store.reset_password_with_token(
        tenant_id=tid,
        username=payload.username,
        reset_token=payload.reset_token,
        new_password=payload.new_password,
    )
    row.pop("password_hash", None)
    return {"ok": True, "tenant_id": tid, "row": row}


@router.get("/summary")
def tenant_summary(tenant_id: str | None = None) -> dict[str, Any]:
    tid = _tenant_or_current(tenant_id)
    return {"ok": True, "tenant_id": tid, **control_plane_store.tenant_summary(tenant_id=tid)}


@router.get("/runtime-context")
def runtime_context(request: Request) -> dict[str, Any]:
    payload = getattr(request.state, "user_payload", {}) or {}
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
