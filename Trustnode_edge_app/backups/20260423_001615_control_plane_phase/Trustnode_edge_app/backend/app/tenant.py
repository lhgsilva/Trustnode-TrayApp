import re
import os
import sqlite3
import time
from contextvars import ContextVar
from typing import Optional

from fastapi import Request, WebSocket


_TENANT_CTX: ContextVar[str] = ContextVar("trustnode_tenant_id", default="default")
_SAFE_TENANT_RE = re.compile(r"[^a-z0-9_\-]")
_DOMAIN_CACHE: dict[str, str] = {}
_DOMAIN_CACHE_TS: float = 0.0
_DOMAIN_CACHE_TTL_SECONDS = 15.0


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
    # Dynamic tenant by configured domains.
    mapped = _tenant_from_configured_domains(host_txt)
    if mapped:
        return mapped
    if host_txt.endswith(".trustnode.lsapps.app"):
        sub = host_txt[: -len(".trustnode.lsapps.app")]
        if sub and sub != "www":
            return normalize_tenant_id(sub)
    return None


def _resolve_db_path() -> str:
    env_path = os.environ.get("TRUSTNODE_APP_STORE_PATH", "").strip()
    if env_path:
        base = env_path
    else:
        data_dir = os.environ.get("TRUSTNODE_DATA_DIR", "").strip()
        if not data_dir:
            data_dir = os.path.join(os.path.expanduser("~"), ".trustnode_edge", "data")
        os.makedirs(data_dir, exist_ok=True)
        base = os.path.join(data_dir, "trustnode_app_store.db")
    return os.path.abspath(base)


def _tenant_from_configured_domains(host_txt: str) -> Optional[str]:
    global _DOMAIN_CACHE, _DOMAIN_CACHE_TS
    now = time.time()
    if _DOMAIN_CACHE and (now - _DOMAIN_CACHE_TS) < _DOMAIN_CACHE_TTL_SECONDS:
        return _DOMAIN_CACHE.get(host_txt)
    mapping: dict[str, str] = {}
    db_path = _resolve_db_path()
    if not os.path.exists(db_path):
        _DOMAIN_CACHE = mapping
        _DOMAIN_CACHE_TS = now
        return None
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT tenant_id, primary_domain FROM cp_tenants WHERE COALESCE(primary_domain,'') <> ''"
        ).fetchall()
        conn.close()
        for row in rows:
            tenant_id = normalize_tenant_id(str(row["tenant_id"] or ""))
            domain = str(row["primary_domain"] or "").strip().lower()
            if tenant_id and domain:
                mapping[domain] = tenant_id
    except Exception:
        mapping = {}
    _DOMAIN_CACHE = mapping
    _DOMAIN_CACHE_TS = now
    return mapping.get(host_txt)


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
