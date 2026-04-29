import re
from contextvars import ContextVar
from typing import Optional

from fastapi import Request, WebSocket


_TENANT_CTX: ContextVar[str] = ContextVar("trustnode_tenant_id", default="default")
_SAFE_TENANT_RE = re.compile(r"[^a-z0-9_\-]")


def normalize_tenant_id(raw: str | None) -> str:
    txt = str(raw or "").strip().lower()
    if not txt:
        return "default"
    txt = txt.replace(".", "_")
    txt = _SAFE_TENANT_RE.sub("", txt)
    return txt or "default"


def set_current_tenant(tenant_id: str | None) -> str:
    normalized = normalize_tenant_id(tenant_id)
    _TENANT_CTX.set(normalized)
    return normalized


def get_current_tenant() -> str:
    return normalize_tenant_id(_TENANT_CTX.get("default"))


def _resolve_from_host(host: str | None) -> Optional[str]:
    host_txt = str(host or "").strip().lower()
    if not host_txt:
        return None
    host_txt = host_txt.split(":")[0]
    # Local dev and direct IP should stay in default tenant.
    if host_txt in {"localhost", "127.0.0.1"}:
        return "default"
    if host_txt.replace(".", "").isdigit():
        return "default"

    # For current test setup, base host is the default tenant.
    if host_txt == "trustnode.lsapps.app":
        return "default"
    if host_txt.endswith(".trustnode.lsapps.app"):
        sub = host_txt[: -len(".trustnode.lsapps.app")]
        if sub and sub != "www":
            return normalize_tenant_id(sub)
    return None


def resolve_request_tenant(request: Request) -> str:
    forced = request.headers.get("X-Trustnode-Tenant", "").strip()
    if forced:
        return normalize_tenant_id(forced)
    q_tenant = request.query_params.get("tenant")
    if q_tenant:
        return normalize_tenant_id(q_tenant)
    host_tenant = _resolve_from_host(request.headers.get("host"))
    if host_tenant:
        return host_tenant
    return "default"


def resolve_websocket_tenant(websocket: WebSocket) -> str:
    forced = websocket.headers.get("X-Trustnode-Tenant", "").strip()
    if forced:
        return normalize_tenant_id(forced)
    q_tenant = websocket.query_params.get("tenant")
    if q_tenant:
        return normalize_tenant_id(q_tenant)
    host_tenant = _resolve_from_host(websocket.headers.get("host"))
    if host_tenant:
        return host_tenant
    return "default"

