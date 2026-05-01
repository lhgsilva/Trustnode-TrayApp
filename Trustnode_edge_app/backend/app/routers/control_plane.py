from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.state import app_store, control_plane_store, telemetry_service
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


class ActivationCodeUpdateRequest(BaseModel):
    status: str = ""
    expires_utc: str = ""


class EdgeRegisterRequest(BaseModel):
    activation_code: str
    edge_id: str
    edge_name: str = ""
    site: str = ""
    area: str = ""
    equipment: str = ""
    admin_username: str = "admin"
    admin_password: str


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
    control_plane_store.audit(
        actor_type="user",
        actor_id=str(payload.get("sub") or "unknown"),
        tenant_id=tenant_id,
        action=action,
        outcome=outcome,
        correlation_id=request.headers.get("X-Correlation-Id", "") or request.headers.get("X-Request-Id", "") or "-",
        details=details or {},
    )


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


@router.get("/customers")
def list_customers(request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id)
    return {"ok": True, "tenant_id": tid, "rows": control_plane_store.list_customers(tenant_id=tid)}


@router.post("/customers")
def upsert_customer(payload: CustomerUpsertRequest, request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    row = control_plane_store.upsert_customer(
        tenant_id=tid,
        customer_id=payload.customer_id,
        company_name=payload.company_name,
        contact_email=payload.contact_email,
        status=payload.status,
        metadata=payload.metadata,
    )
    _audit(
        request,
        tenant_id=tid,
        action="customer.upsert",
        outcome="ok",
        details={"customer_id": row.get("customer_id", "")},
    )
    return {"ok": True, "tenant_id": tid, "row": row}


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
    tid = _scoped_tenant(request, tenant_id)
    return {"ok": True, "tenant_id": tid, "rows": control_plane_store.list_edges(tenant_id=tid)}


@router.post("/edges")
def upsert_edge(payload: EdgeUpsertRequest, request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
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
    _audit(
        request,
        tenant_id=tid,
        action="edge.upsert",
        outcome="ok",
        details={"edge_id": row.get("edge_id", "")},
    )
    return {"ok": True, "tenant_id": tid, "row": row}


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


@router.post("/edges/heartbeat")
def heartbeat_edge(request: Request, edge_id: str, payload: dict[str, Any] | None = None, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id)
    row = control_plane_store.heartbeat_edge(tenant_id=tid, edge_id=edge_id, payload=payload or {})
    if not row:
        _audit(request, tenant_id=tid, action="edge.heartbeat", outcome="not_found", details={"edge_id": edge_id})
        raise HTTPException(status_code=404, detail="edge_not_found")
    _audit(request, tenant_id=tid, action="edge.heartbeat", outcome="ok", details={"edge_id": edge_id})
    return {"ok": True, "tenant_id": tid, "row": row}


@router.get("/licenses")
def list_licenses(request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id)
    return {"ok": True, "tenant_id": tid, "rows": control_plane_store.list_licenses(tenant_id=tid)}


@router.post("/licenses")
def upsert_license(payload: LicenseUpsertRequest, request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
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
    _audit(
        request,
        tenant_id=tid,
        action="license.upsert",
        outcome="ok",
        details={"license_id": row.get("license_id", "")},
    )
    return {"ok": True, "tenant_id": tid, "row": row}


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


@router.get("/licenses/{license_id}/modules")
def list_license_modules(request: Request, license_id: str) -> dict[str, Any]:
    _require_auth_payload(request)
    return {"ok": True, "license_id": license_id, "rows": control_plane_store.list_license_modules(license_id=license_id)}


@router.put("/licenses/{license_id}/modules")
def set_license_modules(request: Request, license_id: str, payload: LicenseModulesRequest) -> dict[str, Any]:
    tid = _scoped_tenant(request, None, require_admin_write=True)
    out = {"ok": True, **control_plane_store.set_license_modules(license_id=license_id, modules=payload.modules)}
    _audit(request, tenant_id=tid, action="license.modules.set", outcome="ok", details={"license_id": license_id})
    return out


@router.get("/users")
def list_users(request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id)
    return {"ok": True, "tenant_id": tid, "rows": control_plane_store.list_users(tenant_id=tid)}


@router.post("/users")
def upsert_user(payload: UserUpsertRequest, request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
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
    _audit(request, tenant_id=tid, action="user.upsert", outcome="ok", details={"username": payload.username})
    return {"ok": True, "tenant_id": tid, "row": row}


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
    return {"ok": True, "tenant_id": tid, "username": username}

@router.post("/users/{username}/delete")
def delete_user_post(request: Request, username: str, tenant_id: str | None = None) -> dict[str, Any]:
    return delete_user(request=request, username=username, tenant_id=tenant_id)


@router.post("/activation-code/issue")
def issue_activation_code(payload: ActivationCodeIssueRequest, request: Request, tenant_id: str | None = None) -> dict[str, Any]:
    tid = _scoped_tenant(request, tenant_id, require_admin_write=True)
    row = control_plane_store.issue_activation_code(
        tenant_id=tid,
        customer_id=payload.customer_id,
        edge_name=payload.edge_name,
        ttl_minutes=payload.ttl_minutes,
        metadata=payload.metadata,
    )
    _audit(
        request,
        tenant_id=tid,
        action="activation_code.issue",
        outcome="ok",
        details={"customer_id": payload.customer_id, "edge_name": payload.edge_name},
    )
    return {"ok": True, "tenant_id": tid, "row": row}


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
    cloud_url = f"{request.url.scheme}://{request.headers.get('host', '').split(':')[0]}".rstrip("/")
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
    cloud_url = f"{request.url.scheme}://{request.headers.get('host', '').split(':')[0]}".rstrip("/")
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
        tenant_id = normalize_tenant_id(str(row.get("tenant_id") or "default"))
        admin_username = str(payload.admin_username or "").strip() or "admin"
        admin_password = str(payload.admin_password or "")
        if not admin_password:
            raise ValueError("admin_password_required")

        # Create/refresh tenant admin in control-plane auth store.
        control_plane_store.upsert_user(
            tenant_id=tenant_id,
            username=admin_username,
            password=admin_password,
            role="admin",
            status="active",
            email="",
            mfa_enabled=False,
            modules=[],
            permissions={},
        )

        # Materialize local bootstrap so first login works immediately.
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
        app_settings_patch["customer_id"] = str(row.get("customer_id") or "")
        app_settings_patch["edge_linked"] = True
        app_settings_patch["license_id"] = str((row.get("license") or {}).get("license_id") or "")
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
        control_plane_store.audit(
            actor_type="device",
            actor_id=str(payload.edge_id or "edge"),
            tenant_id=tenant_id,
            action="edge_link.register",
            outcome="ok",
            correlation_id=request.headers.get("X-Correlation-Id", "") or request.headers.get("X-Request-Id", "") or "-",
            details={"edge_id": payload.edge_id, "admin_username": admin_username},
        )
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "edge_id": str(row.get("edge_id") or payload.edge_id),
            "customer_id": str(row.get("customer_id") or ""),
            "license_id": str((row.get("license") or {}).get("license_id") or ""),
            "cloud_api_url": str(row.get("cloud_api_url") or ""),
            "primary_domain": str(row.get("primary_domain") or ""),
        }
    except Exception as exc:
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
    app_store.save_bootstrap({"app_settings": reset_settings}, actor=f"edge_unlink:{payload.get('sub') or 'admin'}")
    telemetry_service.configure_from_bootstrap({"data": app_store.get_bootstrap(prefer_cloud_reads=False)})
    _audit(request, tenant_id=tenant_id, action="edge_link.unlink", outcome="ok", details={"edge_id": edge_id})
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
    return {"ok": bool(out.get("ok")), "tenant_id": tid, "edge_id": check_edge_id, **out}
