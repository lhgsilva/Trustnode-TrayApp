from typing import Any, Dict, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.state import app_store, telemetry_service
from app.tenant import get_current_tenant

router = APIRouter(prefix="/api/app-store", tags=["app-store"])


def _normalize_host_header(request: Request) -> str:
    raw = str(request.headers.get("host") or "").strip().lower()
    if not raw:
        return ""
    # IPv6 bracketed: [::1]:8000
    if raw.startswith("["):
        end = raw.find("]")
        if end > 0:
            return raw[1:end]
    # host:port
    if ":" in raw:
        left, right = raw.rsplit(":", 1)
        if right.isdigit():
            return left
    return raw


def _resolve_prefer_cloud_reads(request: Request, prefer_cloud: str = "") -> bool:
    forced = str(prefer_cloud or "").strip().lower()
    if forced in {"1", "true", "yes", "on"}:
        return True
    if forced in {"0", "false", "no", "off"}:
        return False
    host = _normalize_host_header(request)
    return bool(host and host not in {"localhost", "127.0.0.1", "::1"})


# Domains that represent COMPANY ASSETS shared by every operator on the
# same edge. Their scope_key drops the trailing `|<username>` segment so
# everyone reading the same edge sees the same gateways, databases,
# meters, reports, alarm rules, etc.
#
# Per-user domains (NOT in this set) keep the full
# `tenant|customer|edge|username` key — dashboards, app_settings, the
# user's personal preferences.
_SHARED_EDGE_DOMAINS = frozenset({
    "gateway_configurations",   # physical PLC/OPC gateways
    "database_configurations",  # DB sinks for historian writes
    "power_management_config",  # power-meter device list
    "devices",                  # device catalog
    "triggers_limits",          # alarm rules
    "alarms_setup",             # alarm event log
    "reporting_setup",          # report templates + schedules
    "tags",                     # tag catalog
    "email_notifications",      # email server config
    # users_access MUST be edge-wide. Every operator on the same physical
    # edge logs into the same set of users; saving it per-user-scope
    # makes newly-created users invisible to the login endpoint (which
    # reads unscoped only) and breaks both 'admin creates user, user
    # logs in' AND 'activation creates admin, admin logs in'.
    "users_access",
    # Dashboards are also shared per user's explicit requirement: every
    # operator on the edge should see the same dashboards/charts; finer
    # personalisation (last-selected profile) lives in app_settings.
    "dashboard_configurations",
})


def _build_scope_key(request: Request, bootstrap_hint: Dict[str, Any] | None = None,
                     *, domain: str | None = None) -> str:
    payload = getattr(request.state, "user_payload", {}) or {}
    username = str(payload.get("sub") or "").strip().lower()
    tenant_id = str(payload.get("tenant_id") or get_current_tenant() or "default").strip().lower()
    bootstrap = bootstrap_hint if isinstance(bootstrap_hint, dict) else {}
    if not bootstrap:
        try:
            bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
        except Exception:
            bootstrap = {}
    app_settings = bootstrap.get("app_settings") if isinstance(bootstrap.get("app_settings"), dict) else {}
    edge_profile = app_settings.get("edge_profile") if isinstance(app_settings.get("edge_profile"), dict) else {}
    edge_id = (
        str(edge_profile.get("edge_id") or "").strip().lower()
        or str(app_settings.get("edge_id") or "").strip().lower()
    )
    # Last-resort fallback to the hostname-derived edge id. Without this
    # an edge whose app_settings hasn't been written yet (fresh install,
    # bootstrap interrupted mid-save, etc.) returns scope_key="" which
    # routes every save through the unscoped path and BREAKS the cloud
    # mirror — that's exactly how Lucas's dashboards stayed invisible to
    # Lite even after every other fix was applied.
    if not edge_id:
        try:
            edge_id = str(getattr(app_store, "_local_edge_id", "") or "").strip().lower()
        except Exception:
            edge_id = ""
    # Read customer_id from edge_profile.linked_customer_id (canonical),
    # then fall back to app_settings.customer_id (set by activation but
    # historically missing from edge_profile), then the bootstrap root.
    # Without this fallback, edges activated before the linked_*
    # fields were copied into edge_profile end up with a malformed
    # scope key 'tenant|-|edge' and the cloud mirror writes nothing
    # Lite can attach to a customer.
    customer_id = (
        str(edge_profile.get("linked_customer_id") or "").strip().lower()
        or str(app_settings.get("customer_id") or "").strip().lower()
        or str(bootstrap.get("customer_id") or "").strip().lower()
    )
    if not edge_id:
        return ""
    # Shared domains drop the user segment so every operator on the edge
    # reads/writes the same row.
    if domain and str(domain).lower() in _SHARED_EDGE_DOMAINS:
        return f"{tenant_id}|{customer_id or '-'}|{edge_id}"
    if not username:
        return ""
    return f"{tenant_id}|{customer_id or '-'}|{edge_id}|{username}"


class DomainSaveRequest(BaseModel):
    domain: str
    payload: Any
    actor: str = "system"


class BootstrapSaveRequest(BaseModel):
    data: Dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"


class AppendRowsRequest(BaseModel):
    rows: list[dict[str, Any]] = Field(default_factory=list)


class HistorianRuleStatsRequest(BaseModel):
    rules: list[dict[str, Any]] = Field(default_factory=list)
    from_utc: str = ""
    to_utc: str = ""
    gateway: str = ""
    edge_id: str = ""
    prefer_cloud: str = ""


class RetentionPolicyPayload(BaseModel):
    enabled: bool = False
    schedule_minutes: int = 60
    raw_keep_days: int = 7
    minute_keep_days: int = 30
    hour_keep_days: int = 180
    day_keep_days: int = 730
    backup_before_cleanup: bool = True
    max_delete_rows_per_run: int = 50000


class RetentionRunRequest(BaseModel):
    dry_run: bool = True
    actor: str = "manual"


class BackupCreateRequest(BaseModel):
    actor: str = "manual"
    label: str = ""


class BackupRestoreRequest(BaseModel):
    filename: str
    actor: str = "manual"


class CleanupDataRequest(BaseModel):
    mode: Literal["period", "last_hours", "last_day", "last_week", "last_month", "all"] = "last_day"
    actor: str = "manual"

class ForceSyncRequest(BaseModel):
    actor: str = "manual"


class ManualDataSyncRequest(BaseModel):
    from_utc: str
    to_utc: str
    max_rows: int = 20000
    include_logs: bool = False
    actor: str = "manual"


class ClearSyncQueueRequest(BaseModel):
    include_sent: bool = False
    actor: str = "manual"


class DropBacklogRequest(BaseModel):
    actor: str = "manual"

class ClearEdgeIngestQueueRequest(BaseModel):
    include_acked: bool = False
    actor: str = "manual"


class FullResetRequest(BaseModel):
    actor: str = "manual"
    clear_cloud_data: bool = True


@router.get("/bootstrap")
def get_bootstrap(request: Request) -> dict:
    # Bootstrap configuration must be served from local app-store authority
    # to keep cloud client UI consistent and avoid stale/slow cloud-read drift.
    prefer_cloud_reads = False
    user_scope = _build_scope_key(request)
    shared_scope = _build_scope_key(request, domain=next(iter(_SHARED_EDGE_DOMAINS)))
    # Start from the user-scoped view (which already overlays the global
    # bootstrap), then overlay the shared-edge scope so company assets
    # (gateways, DBs, alarm rules, …) are always the same for every user.
    if user_scope:
        data = app_store.get_bootstrap_scoped(user_scope, prefer_cloud_reads=prefer_cloud_reads)
    else:
        data = app_store.get_bootstrap(prefer_cloud_reads=prefer_cloud_reads)
    if shared_scope and shared_scope != user_scope:
        try:
            shared = app_store.get_bootstrap_scoped(shared_scope, prefer_cloud_reads=prefer_cloud_reads)
            for d in _SHARED_EDGE_DOMAINS:
                if d in shared:
                    data[d] = shared[d]
        except Exception:
            pass
    return {
        "ok": True,
        "tenant_id": get_current_tenant(),
        "scope_key": user_scope,
        "shared_scope_key": shared_scope,
        "data": data,
    }


@router.put("/bootstrap")
def save_bootstrap(payload: BootstrapSaveRequest, request: Request) -> dict:
    # Split the bootstrap payload by scope: shared-edge domains go to the
    # per-edge scope key, personal domains to the per-user scope key. This
    # is what lets every operator on the same edge see the same gateways,
    # databases, alarm rules, etc., while keeping each user's dashboard
    # layout private to them.
    user_scope = _build_scope_key(request, payload.data if isinstance(payload.data, dict) else None)
    shared_scope = _build_scope_key(
        request, payload.data if isinstance(payload.data, dict) else None,
        domain=next(iter(_SHARED_EDGE_DOMAINS)),
    )
    if not user_scope and not shared_scope:
        versions = app_store.save_bootstrap(payload.data, actor=payload.actor)
        return {"ok": True, "tenant_id": get_current_tenant(), "scope_key": "", "versions": versions}

    versions: Dict[str, Any] = {}
    user_payload: Dict[str, Any] = {}
    shared_payload: Dict[str, Any] = {}
    for domain, value in (payload.data or {}).items():
        if not isinstance(domain, str) or not domain.strip():
            continue
        if domain.strip().lower() in _SHARED_EDGE_DOMAINS:
            shared_payload[domain] = value
        else:
            user_payload[domain] = value
    if user_payload and user_scope:
        versions.update(app_store.save_bootstrap_scoped(user_scope, user_payload, actor=payload.actor))
    if shared_payload and shared_scope:
        versions.update(app_store.save_bootstrap_scoped(shared_scope, shared_payload, actor=payload.actor))
    return {
        "ok": True, "tenant_id": get_current_tenant(),
        "scope_key": user_scope, "shared_scope_key": shared_scope,
        "versions": versions,
    }


@router.put("/domain")
def save_domain(payload: DomainSaveRequest, request: Request) -> dict:
    # Shared domains (gateway_configurations, database_configurations,
    # power_management_config, …) use a per-edge scope so every operator
    # on the same physical edge shares the company assets. Personal
    # domains (dashboards, app_settings) keep the per-user scope.
    scope_key = _build_scope_key(request, domain=payload.domain)
    result = (
        app_store.upsert_domain_scoped(scope_key, payload.domain, payload.payload, actor=payload.actor)
        if scope_key
        else app_store.upsert_domain(payload.domain, payload.payload, actor=payload.actor)
    )
    return {"ok": True, "tenant_id": get_current_tenant(), "scope_key": scope_key, "result": result}


@router.post("/append/historian")
def append_historian(payload: AppendRowsRequest) -> dict:
    # Phase 3b (operator 2026-06-18): refuse data writes when the
    # license is expired (no active trial) or the signature is invalid.
    # The frontend already locks the UI in this state; this is the
    # data-layer enforcement so a customer can't keep collecting
    # historian data on an expired or tampered license.
    try:
        from app.services.license_gate import is_data_writes_allowed
        allowed, reason = is_data_writes_allowed()
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail=f"data writes blocked: {reason}",
            )
    except HTTPException:
        raise
    except Exception:
        # License gate failure is never allowed to block data writes —
        # see the comment in license_gate.py.
        pass
    count = app_store.append_historian_rows(payload.rows)
    return {"ok": True, "tenant_id": get_current_tenant(), "count": count}


@router.post("/append/logs")
def append_logs(payload: AppendRowsRequest) -> dict:
    count = app_store.append_log_rows(payload.rows)
    return {"ok": True, "tenant_id": get_current_tenant(), "count": count}


@router.get("/historian")
def get_historian(
    request: Request,
    limit: int = 1000,
    gateway: str = "",
    device: str = "",
    tag: str = "",
    edge_id: str = "",
    prefer_cloud: str = "",
) -> dict:
    prefer_cloud_reads = _resolve_prefer_cloud_reads(request, prefer_cloud=prefer_cloud)
    # Protect cloud API from expensive oversized scans under high fan-out clients.
    safe_limit = max(50, min(int(limit or 1000), 1000 if prefer_cloud_reads else 5000))
    return {
        "ok": True,
        "tenant_id": get_current_tenant(),
        "rows": app_store.get_historian_rows(
            limit=safe_limit,
            prefer_cloud_reads=prefer_cloud_reads,
            gateway=gateway,
            device=device,
            tag=tag,
            edge_id=edge_id,
        ),
    }


@router.get("/historian/range")
def get_historian_range(
    request: Request,
    from_utc: str = "",
    to_utc: str = "",
    limit: int = 5000,
    offset: int = 0,
    gateway: str = "",
    device: str = "",
    tag: str = "",
    edge_id: str = "",
    prefer_cloud: str = "",
) -> dict:
    prefer_cloud_reads = _resolve_prefer_cloud_reads(request, prefer_cloud=prefer_cloud)
    safe_limit = max(50, min(int(limit or 5000), 10000 if prefer_cloud_reads else 50000))
    safe_offset = max(0, int(offset or 0))
    return {
        "ok": True,
        "tenant_id": get_current_tenant(),
        "rows": app_store.get_historian_rows_range(
            from_utc=from_utc,
            to_utc=to_utc,
            limit=safe_limit,
            offset=safe_offset,
            prefer_cloud_reads=prefer_cloud_reads,
            gateway=gateway,
            device=device,
            tag=tag,
            edge_id=edge_id,
        ),
    }


@router.get("/historian/agg")
def get_historian_agg(
    request: Request,
    bucket: str = "minute",
    from_utc: str = "",
    to_utc: str = "",
    gateway: str = "",
    tag: str = "",
    source: str = "",
    limit: int = 50000,
) -> dict:
    """Operator 2026-06-17: serve pre-bucketed historian rows from the
    `historian_agg_<bucket>` tables (populated by the retention worker).
    Lets dashboards skip pulling 1 Hz raw rows when the window is wide
    (e.g. 24 h × Minute → 1 440 rows instead of ~17 000)."""
    safe_limit = max(50, min(int(limit or 50000), 100000))
    return {
        "ok": True,
        "tenant_id": get_current_tenant(),
        "bucket": bucket,
        "rows": app_store.get_historian_agg_rows(
            bucket=bucket,
            from_utc=from_utc,
            to_utc=to_utc,
            gateway=gateway,
            tag=tag,
            source=source,
            limit=safe_limit,
        ),
    }


@router.get("/historian/stats")
def get_historian_stats(
    request: Request,
    from_utc: str = "",
    to_utc: str = "",
    gateway: str = "",
    device: str = "",
    tag: str = "",
    edge_id: str = "",
    prefer_cloud: str = "",
) -> dict:
    prefer_cloud_reads = _resolve_prefer_cloud_reads(request, prefer_cloud=prefer_cloud)
    return {
        "ok": True,
        "tenant_id": get_current_tenant(),
        "rows": app_store.get_historian_stats(
            from_utc=from_utc,
            to_utc=to_utc,
            gateway=gateway,
            device=device,
            tag=tag,
            edge_id=edge_id,
            prefer_cloud_reads=prefer_cloud_reads,
        ),
    }


@router.post("/historian/rule-stats")
def get_historian_rule_stats(
    payload: HistorianRuleStatsRequest,
    request: Request,
) -> dict:
    prefer_cloud_reads = _resolve_prefer_cloud_reads(request, prefer_cloud=payload.prefer_cloud)
    return {
        "ok": True,
        "tenant_id": get_current_tenant(),
        "rows": app_store.get_historian_rule_stats(
            rules=payload.rules,
            from_utc=payload.from_utc,
            to_utc=payload.to_utc,
            gateway=payload.gateway,
            edge_id=payload.edge_id,
            prefer_cloud_reads=prefer_cloud_reads,
        ),
    }


@router.get("/live")
def get_live(request: Request, limit: int = 5000, edge_id: str = "") -> dict:
    prefer_cloud_reads = _resolve_prefer_cloud_reads(request)
    safe_limit = max(50, min(int(limit or 5000), 800 if prefer_cloud_reads else 5000))
    return {
        "ok": True,
        "tenant_id": get_current_tenant(),
        "rows": app_store.get_live_rows(limit=safe_limit, prefer_cloud_reads=prefer_cloud_reads, edge_id=edge_id),
    }


@router.get("/logs")
def get_logs(request: Request, limit: int = 2000, edge_id: str = "") -> dict:
    prefer_cloud_reads = _resolve_prefer_cloud_reads(request)
    safe_limit = max(50, min(int(limit or 2000), 1000 if prefer_cloud_reads else 5000))
    return {
        "ok": True,
        "tenant_id": get_current_tenant(),
        "rows": app_store.get_log_rows(limit=safe_limit, prefer_cloud_reads=prefer_cloud_reads, edge_id=edge_id),
    }


@router.get("/inspector")
def get_inspector(preview_limit: int = 10) -> dict:
    return {"ok": True, "tenant_id": get_current_tenant(), "inspector": app_store.get_inspector_snapshot(preview_limit=preview_limit)}


@router.get("/tenant/context")
def get_tenant_context() -> dict:
    return {
        "ok": True,
        "tenant_id": get_current_tenant(),
    }


@router.get("/sync/mirror-check")
def get_sync_mirror_check() -> dict:
    return {"ok": True, "tenant_id": get_current_tenant(), "mirror": app_store.get_mirror_check()}


@router.get("/retention/policy")
def get_retention_policy() -> dict:
    return {"ok": True, "policy": app_store.get_retention_policy()}


@router.put("/retention/policy")
def set_retention_policy(payload: RetentionPolicyPayload) -> dict:
    policy = app_store.set_retention_policy(payload.model_dump())
    return {"ok": True, "policy": policy}


@router.post("/retention/run")
def run_retention(payload: RetentionRunRequest) -> dict:
    return app_store.run_retention(dry_run=payload.dry_run, actor=payload.actor)


@router.get("/retention/runs")
def get_retention_runs(limit: int = 50) -> dict:
    return {"ok": True, "runs": app_store.get_retention_runs(limit=limit)}


@router.get("/backups")
def get_backups(limit: int = 200) -> dict:
    return {"ok": True, "rows": app_store.list_backups(limit=limit)}


@router.post("/backups/create")
def create_backup(payload: BackupCreateRequest) -> dict:
    return app_store.create_backup(actor=payload.actor, label=payload.label)


@router.post("/backups/restore")
def restore_backup(payload: BackupRestoreRequest) -> dict:
    return app_store.restore_backup(filename=payload.filename, actor=payload.actor)


@router.delete("/backups/{filename}")
def delete_backup(filename: str) -> dict:
    return app_store.delete_backup(filename=filename)


@router.post("/cleanup-data")
def cleanup_data(payload: CleanupDataRequest) -> dict:
    return app_store.cleanup_data(mode=payload.mode, actor=payload.actor)


@router.post("/sync/force")
def force_sync(payload: ForceSyncRequest) -> dict:
    return app_store.force_sync_now(actor=payload.actor)


@router.post("/sync/repair_scope")
def repair_scope(payload: ForceSyncRequest) -> dict:
    """Lightweight one-shot recovery endpoint. Repairs stale 'tenant|-|edge'
    scope keys to 'tenant|customer|edge' and re-mirrors Lite-visible scoped
    docs to the cloud. Does NOT run config/live/data outbox flushes, so the
    edge's auto-recovery doesn't pile work onto the periodic sync loop and
    doesn't slow Lite's realtime channel.
    """
    out = {"ok": True, "actor": payload.actor, "errors": []}
    try:
        app_store._repair_scope_keys_with_customer_id()
    except Exception as exc:
        out["errors"].append(f"repair_scope: {exc}")
    try:
        app_store._remirror_scoped_docs_to_cloud()
    except Exception as exc:
        out["errors"].append(f"remirror_scoped: {exc}")
    out["ok"] = not out["errors"]
    return out


@router.post("/sync/force-resync")
def force_resync() -> dict:
    """Synchronously re-run the boot remirror so the operator can push
    every locally-saved dashboard / alarm / trigger / gateway / device
    to cloud on demand. Returns the per-domain success / error breakdown
    AFTER the batch completes so the operator immediately sees what
    landed and what failed."""
    # Reset state so the response only reflects THIS batch.
    setattr(app_store, "_mirror_state", {"last_error": {}, "last_success_utc": {}, "attempts": {}})
    try:
        app_store._remirror_scoped_docs_to_cloud()  # synchronous
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    state = getattr(app_store, "_mirror_state", None) or {}
    return {
        "ok": True,
        "attempts": dict(state.get("attempts") or {}),
        "last_success_utc": dict(state.get("last_success_utc") or {}),
        "last_error": dict(state.get("last_error") or {}),
    }


@router.get("/sync/mirror-state")
def get_mirror_state() -> dict:
    """Diagnose the per-table config mirror. Returns the last_error,
    last_success_utc and total attempts for each Lite-mirrored domain
    (dashboard_configurations, alarms_setup, triggers_limits,
    gateway_configurations, devices). Lets the operator see exactly why
    a mirror upsert is failing without grepping the backend log.

    Output:
      {
        "ok": true,
        "cloud_target_configured": true,
        "attempts": { "dashboard_configurations": 12, ... },
        "last_success_utc": { "alarms_setup": "...", ... },
        "last_error": { "dashboard_configurations": "ProgrammingError: ..." }
      }
    """
    state = getattr(app_store, "_mirror_state", None) or {}
    try:
        cloud = app_store._get_cloud_database_target()
    except Exception:
        cloud = None
    return {
        "ok": True,
        "cloud_target_configured": bool(cloud),
        "cloud_target_host": str((cloud or {}).get("host") or ""),
        "attempts": dict(state.get("attempts") or {}),
        "last_success_utc": dict(state.get("last_success_utc") or {}),
        "last_error": dict(state.get("last_error") or {}),
    }


@router.get("/sync/status")
def get_sync_status() -> dict:
    """Read-only summary of the cloud sync worker state.

    Returned shape matches the `summary` block of force_sync_now() but
    runs no flush. Cheap enough for the UI to poll every 1–2 seconds.
    Includes whether telemetry-v1 is enabled, the live/data sync errors
    if any, and the historian/log backlog counts so the UI can decide
    whether to show the "big backlog" prompt.
    """
    snap = app_store.get_inspector_snapshot(preview_limit=0) or {}
    outbox = snap.get("sync_outbox_status") or {}
    data_sync = snap.get("data_sync") or {}
    sync_target = snap.get("sync_target") or {}
    # Pull telemetry-v1 state via the same service singleton the routers
    # already use so the UI sees a consistent picture.
    try:
        v1_summary = telemetry_service.sync_summary() if hasattr(telemetry_service, "sync_summary") else {}
    except Exception:
        v1_summary = {}
    return {
        "ok": True,
        "config_pending": int(outbox.get("pending") or 0),
        "config_failed": int(outbox.get("failed") or 0),
        "config_sent_total": int(outbox.get("sent") or 0),
        "historian_backlog": int(data_sync.get("historian_backlog") or 0),
        "logs_backlog": int(data_sync.get("logs_backlog") or 0),
        "historian_synced_total": int(data_sync.get("total_historian_synced") or 0),
        "logs_synced_total": int(data_sync.get("total_logs_synced") or 0),
        "last_config_sync_utc": str(sync_target.get("last_sync_utc") or ""),
        "last_data_sync_utc": str(data_sync.get("last_data_sync_utc") or ""),
        "last_config_error": str(sync_target.get("last_error") or ""),
        "last_data_error": str(data_sync.get("last_data_error") or ""),
        "cloud_target": {
            "name": str(sync_target.get("name") or ""),
            "host": str(sync_target.get("host") or ""),
            "enabled": bool(sync_target.get("enabled")),
        },
        "telemetry_v1": v1_summary,
    }


@router.get("/sync/health")
def get_sync_health() -> dict:
    """Operator-friendly health summary of the cloud data plane.

    Designed for monitoring UIs (and humans during incident triage). Wraps
    /sync/status with a 'healthy'/'degraded'/'down' verdict so you don't
    have to interpret half a dozen counters at once. Also returns the
    absolute app_store_db path so support sessions can immediately tell
    which DB the running backend is using — the portable EXE / install
    split-state issue was diagnosed entirely by hunting for the live DB
    among temp directories.
    """
    import os as _os
    snap = app_store.get_inspector_snapshot(preview_limit=0) or {}
    data_sync = snap.get("data_sync") or {}
    sync_target = snap.get("sync_target") or {}
    outbox = snap.get("sync_outbox_status") or {}

    cloud_enabled = bool(sync_target.get("enabled"))
    data_err = str(data_sync.get("last_data_error") or "")
    cfg_err = str(sync_target.get("last_error") or "")
    backlog = int(data_sync.get("historian_backlog") or 0)
    last_sync_utc = str(data_sync.get("last_data_sync_utc") or "")

    age_seconds: int | None = None
    if last_sync_utc:
        try:
            from datetime import datetime, timezone
            txt = last_sync_utc.replace("Z", "+00:00")
            if " " in txt and "T" not in txt:
                txt = txt.replace(" ", "T")
            dt = datetime.fromisoformat(txt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_seconds = int((datetime.now(timezone.utc) - dt).total_seconds())
        except Exception:
            age_seconds = None

    # Verdict heuristic — generous on first-boot windows where last_sync is
    # empty but no errors are recorded either.
    if not cloud_enabled and not data_err and not cfg_err:
        verdict = "disabled"
    elif data_err or cfg_err:
        verdict = "degraded"
    elif backlog > 0 and age_seconds is not None and age_seconds > 300:
        verdict = "degraded"
    else:
        verdict = "healthy"

    db_path = ""
    try:
        db_path = _os.path.abspath(getattr(app_store, "_db_path", "") or "")
    except Exception:
        db_path = ""

    return {
        "ok": True,
        "verdict": verdict,
        "cloud_target_enabled": cloud_enabled,
        "cloud_target_name": str(sync_target.get("name") or ""),
        "cloud_target_host": str(sync_target.get("host") or ""),
        "historian_backlog": backlog,
        "logs_backlog": int(data_sync.get("logs_backlog") or 0),
        "config_pending": int(outbox.get("pending") or 0),
        "last_data_sync_utc": last_sync_utc,
        "last_data_sync_age_seconds": age_seconds,
        "last_data_error": data_err,
        "last_config_error": cfg_err,
        "total_historian_synced": int(data_sync.get("total_historian_synced") or 0),
        "app_store_db_path": db_path,
    }


@router.post("/sync/manual-period")
def manual_period_sync(payload: ManualDataSyncRequest) -> dict:
    return app_store.manual_sync_data_period(
        from_utc=payload.from_utc,
        to_utc=payload.to_utc,
        actor=payload.actor,
        max_rows=payload.max_rows,
        include_logs=payload.include_logs,
    )


@router.post("/sync/queue/clear")
def clear_sync_queue(payload: ClearSyncQueueRequest) -> dict:
    return app_store.clear_sync_queue(actor=payload.actor, include_sent=payload.include_sent)


@router.post("/sync/backlog/drop")
def drop_sync_backlog(payload: DropBacklogRequest) -> dict:
    return app_store.drop_data_backlog(actor=payload.actor)

@router.post("/sync/edge-ingest/clear")
def clear_edge_ingest_queue(payload: ClearEdgeIngestQueueRequest) -> dict:
    return telemetry_service.clear_outbox(include_acked=payload.include_acked, actor=payload.actor)


@router.post("/reset/full")
def reset_full(payload: FullResetRequest) -> dict:
    return app_store.reset_all_data_and_config(actor=payload.actor, clear_cloud_data=payload.clear_cloud_data)
