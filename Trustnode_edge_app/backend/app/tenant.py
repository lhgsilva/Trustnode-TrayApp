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
_ALLOW_DYNAMIC_SUBDOMAIN = str(os.environ.get("TRUSTNODE_ALLOW_DYNAMIC_TENANT_SUBDOMAIN", "0")).strip().lower() in {"1", "true", "yes", "on"}


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
    if _ALLOW_DYNAMIC_SUBDOMAIN and host_txt.endswith(".trustnode.lsapps.app"):
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


ADMIN_ROLES = ("admin", "super")


def edge_session_decision(user_tenant: str, role: str, configured_tenant: str,
                          *, recovery: bool = False) -> tuple:
    """(allowed, tenant_to_run_as, reason) for a sign-in on this edge.

    2026-11-04, from the owner: "we never should use the developer tenant on
    local edges; the edge is linked to a customer and licences and the LAN
    should only see this; the developer tenant should never be linked to a
    local edge customer" - and do not move admin accounts into the customer.

    On an edge ACTIVATED into a customer tenant:
      * an account of that tenant signs in as it is;
      * a NON-admin account still filed under `default` (an operator or
        viewer created before the 2026-11-02 fix) signs in as the edge's
        tenant - it was always this edge's own account;
      * an ADMIN account under `default` is a developer account: refused;
      * an account of another customer's tenant: refused;
      * the local recovery login (`recovery`) runs as the edge's tenant.
    An edge with no identity (the portal, a fresh install) decides nothing.
    """
    conf = normalize_tenant_id(str(configured_tenant or ""))
    tok = normalize_tenant_id(str(user_tenant or "")) or "default"
    if not conf or conf == "default":
        return True, tok, ""
    if tok == conf:
        return True, tok, ""
    if tok == "default":
        if recovery:
            return True, conf, ""
        if str(role or "").strip().lower() in ADMIN_ROLES:
            return False, "", "developer_account"
        return True, conf, ""
    return False, "", "other_customer"


def edge_session_refusal_text(reason: str, user_tenant: str, configured_tenant: str) -> str:
    if reason == "developer_account":
        return ("This is a TrustNode developer account. It cannot sign in to a customer edge - "
                "sign in with an account of this edge.")
    return ("This account belongs to another customer (%s). This edge belongs to %s - sign in "
            "with an account of this edge." % (user_tenant or "?", configured_tenant or "?"))


def session_tenant(token_tenant: str, configured_tenant: str) -> str:
    """The tenant a SIGNED-IN session runs as.

    2026-11-04, reported: "when the LAN connection is opened by other users
    the reports generated are not showing". Every account created on an edge
    before the 2026-11-02 tenant fix was filed under `default`, the developer
    tenant, and its token carries that. The request then ran as `default`,
    where none of this edge's reports, templates or configuration live -
    measured: the same admin request returned 1 report for an account under
    the edge's tenant and 0 for one under `default`, on the desktop and over
    LAN alike.

    On an ACTIVATED edge such an account is this edge's own local account,
    so its session runs as the edge's tenant. A token that names a real
    customer tenant is never changed, and an edge with no identity (the
    portal, a fresh install) keeps `default`, because that is where its own
    data lives.
    """
    tok = normalize_tenant_id(str(token_tenant or ""))
    conf = normalize_tenant_id(str(configured_tenant or ""))
    if (not tok or tok == "default") and conf and conf != "default":
        return conf
    return tok


def effective_tenant(token_tenant: str, host_tenant: str, configured_tenant: str) -> str:
    """Which tenant a request runs as.

    2026-11-02, from the owner: `default` is TrustNode's own developer tenant,
    where the portal's licence controls live. "None of customer or new
    installations should have a default tenant user. Every new installation
    is always linked to a tenant customer id from the portal."

    Until now a request from the desktop app - always 127.0.0.1 - with no
    token tenant resolved to `default` (see _resolve_from_host), so every
    user, document and historian row a customer created locally could land
    in the developer tenant. Measured on a live edge: cp_users held the
    customer's own `admin` under tenant `default`.

    The rule, in order:
      1. the token's tenant - a signed-in user carries their own;
      2. a real host-mapped tenant (a hosted portal domain);
      3. the tenant this edge was ACTIVATED into - the configured one;
      4. `default`, which after activation should never be reached.
    """
    tok = normalize_tenant_id(str(token_tenant or ""))
    if tok and tok != "default":
        return tok
    host = normalize_tenant_id(str(host_tenant or ""))
    if host and host != "default":
        return host
    conf = normalize_tenant_id(str(configured_tenant or ""))
    if conf and conf != "default":
        return conf
    return tok or host or "default"


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
