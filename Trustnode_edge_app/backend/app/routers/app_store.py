from typing import Any, Dict, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.state import app_store
from app.tenant import get_current_tenant

router = APIRouter(prefix="/api/app-store", tags=["app-store"])


class DomainSaveRequest(BaseModel):
    domain: str
    payload: Any
    actor: str = "system"


class BootstrapSaveRequest(BaseModel):
    data: Dict[str, Any] = Field(default_factory=dict)
    actor: str = "system"


class AppendRowsRequest(BaseModel):
    rows: list[dict[str, Any]] = Field(default_factory=list)


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


class FullResetRequest(BaseModel):
    actor: str = "manual"
    clear_cloud_data: bool = True


@router.get("/bootstrap")
def get_bootstrap(request: Request) -> dict:
    host = str(request.headers.get("host") or "").strip().lower().split(":")[0]
    prefer_cloud_reads = bool(host and host not in {"localhost", "127.0.0.1"})
    return {
        "ok": True,
        "tenant_id": get_current_tenant(),
        "data": app_store.get_bootstrap(prefer_cloud_reads=prefer_cloud_reads),
    }


@router.put("/bootstrap")
def save_bootstrap(payload: BootstrapSaveRequest) -> dict:
    versions = app_store.save_bootstrap(payload.data, actor=payload.actor)
    return {"ok": True, "tenant_id": get_current_tenant(), "versions": versions}


@router.put("/domain")
def save_domain(payload: DomainSaveRequest) -> dict:
    result = app_store.upsert_domain(payload.domain, payload.payload, actor=payload.actor)
    return {"ok": True, "tenant_id": get_current_tenant(), "result": result}


@router.post("/append/historian")
def append_historian(payload: AppendRowsRequest) -> dict:
    count = app_store.append_historian_rows(payload.rows)
    return {"ok": True, "tenant_id": get_current_tenant(), "count": count}


@router.post("/append/logs")
def append_logs(payload: AppendRowsRequest) -> dict:
    count = app_store.append_log_rows(payload.rows)
    return {"ok": True, "tenant_id": get_current_tenant(), "count": count}


@router.get("/historian")
def get_historian(limit: int = 1000) -> dict:
    return {"ok": True, "tenant_id": get_current_tenant(), "rows": app_store.get_historian_rows(limit=limit)}


@router.get("/live")
def get_live(limit: int = 5000) -> dict:
    return {"ok": True, "tenant_id": get_current_tenant(), "rows": app_store.get_live_rows(limit=limit)}


@router.get("/logs")
def get_logs(limit: int = 2000) -> dict:
    return {"ok": True, "tenant_id": get_current_tenant(), "rows": app_store.get_log_rows(limit=limit)}


@router.get("/inspector")
def get_inspector(preview_limit: int = 10) -> dict:
    return {"ok": True, "tenant_id": get_current_tenant(), "inspector": app_store.get_inspector_snapshot(preview_limit=preview_limit)}


@router.get("/tenant/context")
def get_tenant_context() -> dict:
    return {
        "ok": True,
        "tenant_id": get_current_tenant(),
    }


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


@router.post("/reset/full")
def reset_full(payload: FullResetRequest) -> dict:
    return app_store.reset_all_data_and_config(actor=payload.actor, clear_cloud_data=payload.clear_cloud_data)
