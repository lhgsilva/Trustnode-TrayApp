"""Retention, storage and backup API (operator 2026-08-21).

Mounted under the existing /api/app-store prefix so the frontend keeps using
one base URL. Every mutating route is ADMIN-ONLY and enforced HERE, on the
server: the legacy retention/backup/cleanup routes were gated in the browser
only, which meant any authenticated viewer could wipe the historian.

Design: docs/historian-retention-and-forwarding-architecture-2026-08-21.md
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.services import retention_engine as R

router = APIRouter(prefix="/api/app-store", tags=["retention"])


# ---------------------------------------------------------------- helpers
def _require_admin(request: Request) -> str:
    """Same contract as routers/workspace.py: the auth middleware has already
    rejected anonymous callers; we refuse anyone who is not an admin."""
    payload = getattr(request.state, "user_payload", None) or {}
    role = str(payload.get("role") or "").strip().lower()
    if role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    return str(payload.get("sub") or payload.get("username") or "admin")


def _engine() -> R.RetentionEngine:
    eng = R.get_engine()
    if eng is None:
        raise HTTPException(status_code=503, detail="Retention engine is not available yet.")
    return eng


def _policy_error(exc: R.PolicyError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


# ---------------------------------------------------------------- models
class PolicyIn(BaseModel):
    id: str = ""
    name: str = "Retention policy"
    raw: Dict[str, Any] = Field(default_factory=dict)
    tiers: List[Dict[str, Any]] = Field(default_factory=list)
    text_tags: Dict[str, Any] = Field(default_factory=dict)
    maintenance: Dict[str, Any] = Field(default_factory=dict)
    other_data: Dict[str, Any] = Field(default_factory=dict)
    backups: Dict[str, Any] = Field(default_factory=dict)
    activate: bool = False


class RunIn(BaseModel):
    dry_run: bool = True
    force: bool = False
    # 2026-08-23: the UI starts long passes in the background and polls
    # status.engine.busy. Default stays synchronous so existing callers
    # (scripts, the tray) behave exactly as before.
    background: bool = False


class BackupCreateIn(BaseModel):
    kind: str = R.BACKUP_KIND_CONFIG      # "config" | "full"
    label: str = ""


class BackupNameIn(BaseModel):
    filename: str


# ---------------------------------------------------------------- status
@router.get("/retention/v2/storage")
def retention_storage() -> dict:
    """Size of the local store and the cloud database, with the big tables.

    The question behind this is always the same - "why is it so big, and what
    would cleaning up actually give me back" - so it reports dead space and
    the largest tables rather than one total that cannot be acted on.
    """
    import os

    from app.state import app_store as _store

    out: dict = {"ok": True, "local": {}, "cloud": {}, "retention": {}}

    # --- local -----------------------------------------------------------
    try:
        path = _store._db_path
        file_bytes = 0
        for suffix in ("", "-wal", "-shm"):
            p = path + suffix
            if os.path.exists(p):
                file_bytes += os.path.getsize(p)
        with _store._connect() as conn:
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
            freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            hist_rows = int(conn.execute(
                "SELECT COUNT(*) FROM historian_readings").fetchone()[0])
            span = conn.execute(
                "SELECT MIN(ts_utc), MAX(ts_utc) FROM historian_readings").fetchone()
        dead = freelist * page_size
        out["local"] = {
            "path": path,
            "file_bytes": file_bytes,
            "wal_bytes": os.path.getsize(path + "-wal") if os.path.exists(path + "-wal") else 0,
            "dead_bytes": dead,
            "dead_pct": round((freelist / float(page_count)) * 100.0, 1) if page_count else 0.0,
            "historian_rows": hist_rows,
            "oldest": (span[0] if span else "") or "",
            "newest": (span[1] if span else "") or "",
            # What a compact would hand back right now.
            "reclaimable_bytes": dead,
        }
    except Exception as exc:  # noqa: BLE001
        out["local"] = {"error": "%s: %s" % (type(exc).__name__, exc)}

    # --- cloud -------------------------------------------------------------
    try:
        cloud = _store._get_cloud_database_target()
        if not cloud:
            out["cloud"] = {"configured": False}
        else:
            from sqlalchemy import text  # type: ignore
            schema = str(cloud.get("schema") or "public")
            engine, _ = _store._get_or_create_cloud_engine(cloud, schema)
            with engine.connect() as c:
                db_bytes = int(c.execute(text(
                    "SELECT pg_database_size(current_database())")).scalar() or 0)
                rows = c.execute(text(
                    "SELECT relname, pg_total_relation_size(c.oid) AS bytes "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = :s AND c.relkind = 'r' "
                    "ORDER BY 2 DESC LIMIT 10"), {"s": schema}).fetchall()
                hist = int(c.execute(text(
                    'SELECT COUNT(*) FROM "%s"."historian_readings"' % schema)).scalar() or 0)
            out["cloud"] = {
                "configured": True,
                "name": str(cloud.get("name") or ""),
                "schema": schema,
                "database_bytes": db_bytes,
                "historian_rows": hist,
                # The historian is usually the culprit, but not always: after it
                # was emptied on 2026-08-31 the database was still 20 GB.
                "tables": [{"name": str(r[0]), "bytes": int(r[1] or 0)} for r in rows],
            }
    except Exception as exc:  # noqa: BLE001
        out["cloud"] = {"configured": True, "error": "%s: %s" % (type(exc).__name__, exc)}

    # --- is anything cleaning up on its own? -------------------------------
    try:
        pol = _engine().store.get_active_policy() or {}
        maint = pol.get("maintenance") or {}
        cloud_cfg = pol.get("cloud") or {}
        out["retention"] = {
            "active": bool(pol),
            "name": str(pol.get("name") or ""),
            "raw_keep": str((pol.get("raw") or {}).get("keep") or ""),
            "auto_compact": bool(maint.get("auto_compact", True)),
            "auto_compact_free_pct": int(maint.get("auto_compact_free_pct") or 25),
            "cloud_enabled": bool(cloud_cfg.get("enabled")),
            "cloud_keep": str(cloud_cfg.get("keep") or ""),
        }
    except Exception as exc:  # noqa: BLE001
        out["retention"] = {"error": "%s: %s" % (type(exc).__name__, exc)}
    return out


@router.get("/retention/v2/status")
def retention_status(request: Request) -> Dict[str, Any]:
    """Storage picture for the Backup & Retention page. Readable by any signed-in
    user (it is diagnostics, not a mutation)."""
    return {"ok": True, "status": _engine().status()}


@router.get("/retention/v2/options")
def retention_options() -> Dict[str, Any]:
    return {
        "ok": True,
        "resolutions": [{"label": lbl, "seconds": s} for lbl, s in R.RESOLUTION_CHOICES],
        "aggregates": list(R.AGGREGATES),
        "max_keep": R.format_duration(R.MAX_KEEP_S),
        "max_tiers": R.MAX_TIERS,
        "presets": R.BUILTIN_PRESETS,
        "defaults": {
            "other_data": R.DEFAULT_OTHER_DATA,
            "maintenance": R.DEFAULT_MAINTENANCE,
            "backups": R.DEFAULT_BACKUPS,
        },
    }


# ---------------------------------------------------------------- policies
@router.get("/retention/v2/policies")
def list_policies() -> Dict[str, Any]:
    eng = _engine()
    return {"ok": True, "policies": eng.store.list_policies(),
            "active": eng.store.get_active_policy()}


@router.put("/retention/v2/policies")
def save_policy(payload: PolicyIn, request: Request) -> Dict[str, Any]:
    actor = _require_admin(request)
    eng = _engine()
    try:
        doc = R.validate_policy(payload.model_dump(exclude={"activate"}))
    except R.PolicyError as exc:
        raise _policy_error(exc)
    saved = eng.store.save_policy(doc, actor=actor)
    if payload.activate:
        eng.store.activate_policy(saved["id"], actor)
        eng.wake()
        saved = eng.store.get_active_policy() or saved
    return {"ok": True, "policy": saved}


@router.post("/retention/v2/policies/{policy_id}/activate")
def activate_policy(policy_id: str, request: Request) -> Dict[str, Any]:
    actor = _require_admin(request)
    eng = _engine()
    active = eng.store.activate_policy(policy_id, actor)
    if active is None:
        raise HTTPException(status_code=404, detail="Policy not found.")
    eng.wake()
    return {"ok": True, "active": active}


@router.post("/retention/v2/deactivate")
def deactivate_policy(request: Request) -> Dict[str, Any]:
    """The 'no policy' state: collect everything, delete nothing."""
    actor = _require_admin(request)
    eng = _engine()
    eng.store.activate_policy("", actor)
    return {"ok": True, "active": None}


@router.delete("/retention/v2/policies/{policy_id}")
def delete_policy(policy_id: str, request: Request) -> Dict[str, Any]:
    _require_admin(request)
    eng = _engine()
    active = eng.store.get_active_policy()
    if active and active.get("id") == policy_id:
        raise HTTPException(status_code=409,
                            detail="That policy is active. Deactivate it first.")
    if not eng.store.delete_policy(policy_id):
        raise HTTPException(status_code=404, detail="Policy not found.")
    return {"ok": True}


@router.post("/retention/v2/estimate")
def estimate(payload: PolicyIn, request: Request) -> Dict[str, Any]:
    """Live figure for the policy editor, using THIS machine's measured
    bytes-per-row and its actual tag count / poll interval."""
    _require_admin(request)
    eng = _engine()
    try:
        doc = R.validate_policy(payload.model_dump(exclude={"activate"}))
    except R.PolicyError as exc:
        raise _policy_error(exc)
    costs = eng.measured_row_costs()
    stats = eng._tag_stats()
    est = R.estimate_policy_size(
        doc,
        tag_count=int(stats.get("tag_count") or 1),
        interval_s=float(stats.get("interval_s") or 1.0),
        bytes_per_raw_row=costs["bytes_per_raw_row"],
        bytes_per_rollup_row=costs["bytes_per_rollup_row"],
    )
    return {"ok": True, "estimate": est, "policy": doc, "collection": stats}


# ---------------------------------------------------------------- run
@router.post("/retention/v2/run")
def run_now(payload: RunIn, request: Request) -> Dict[str, Any]:
    actor = _require_admin(request)
    eng = _engine()
    if payload.background:
        started = eng.run_in_background(reason=f"manual:{actor}", dry_run=payload.dry_run,
                                        force=payload.force)
        # started=False means a pass is already running; either way the caller
        # polls status.engine.busy and then reads /retention/v2/runs.
        return {"ok": True, "background": True, "started": started, "busy": True}
    summary = eng.run_once(reason=f"manual:{actor}", dry_run=payload.dry_run, force=payload.force)
    return {"ok": True, "summary": summary}


@router.get("/retention/v2/runs")
def list_runs(limit: int = 50) -> Dict[str, Any]:
    eng = _engine()
    conn = eng.store.connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT id, run_utc, dry_run, status, details_json FROM retention_runs "
            "ORDER BY id DESC LIMIT ?", (max(1, min(int(limit or 50), 500)),)).fetchall()
    finally:
        conn.close()
    import json as _json
    out = []
    for r in rows:
        try:
            details = _json.loads(r["details_json"] or "{}")
        except Exception:
            details = {}
        out.append({"id": r["id"], "run_utc": r["run_utc"], "dry_run": bool(r["dry_run"]),
                    "status": r["status"], "details": details})
    return {"ok": True, "runs": out}


# ---------------------------------------------------------------- compaction
@router.post("/retention/v2/compact")
def compact(request: Request) -> Dict[str, Any]:
    """SQLite never shrinks on DELETE. This builds a compacted copy online and
    stages it; it is swapped in at the next start."""
    _require_admin(request)
    return _engine().compact()


@router.post("/retention/v2/compact/cancel")
def cancel_compact(request: Request) -> Dict[str, Any]:
    _require_admin(request)
    return _engine().cancel_compaction()


# ---------------------------------------------------------------- backups
@router.get("/backups/v2")
def list_backups(limit: int = 200) -> Dict[str, Any]:
    eng = _engine()
    policy = eng.store.get_active_policy() or {}
    location = str((policy.get("backups") or {}).get("location") or "")
    rows = eng.backups.list_backups(location, limit)
    return {"ok": True, "rows": rows, "directory": eng.backups.backup_dir(location),
            "pending_restore": eng.backups.pending_restore()}


@router.post("/backups/v2/create")
def create_backup(payload: BackupCreateIn, request: Request) -> Dict[str, Any]:
    actor = _require_admin(request)
    eng = _engine()
    policy = eng.store.get_active_policy() or {}
    location = str((policy.get("backups") or {}).get("location") or "")
    kind = (payload.kind or R.BACKUP_KIND_CONFIG).strip().lower()
    label = payload.label or actor
    if kind == R.BACKUP_KIND_FULL:
        res = eng.backups.create_full_backup(location, label=label)
    elif kind == R.BACKUP_KIND_CONFIG:
        res = eng.backups.create_config_backup(location, label=label)
    else:
        raise HTTPException(status_code=400, detail="kind must be 'config' or 'full'.")
    if not res.get("ok"):
        raise HTTPException(status_code=500, detail=res.get("message") or "Backup failed.")
    return {"ok": True, "backup": res}


@router.post("/backups/v2/restore")
def restore_backup(payload: BackupNameIn, request: Request) -> Dict[str, Any]:
    """Staged, never applied to a live database: the swap happens at next start
    (after a safety copy of the current database)."""
    _require_admin(request)
    eng = _engine()
    policy = eng.store.get_active_policy() or {}
    location = str((policy.get("backups") or {}).get("location") or "")
    res = eng.backups.stage_restore(payload.filename, location)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("message") or "Restore failed.")
    return res


@router.post("/backups/v2/restore/cancel")
def cancel_restore(request: Request) -> Dict[str, Any]:
    _require_admin(request)
    return _engine().backups.cancel_restore()


@router.delete("/backups/v2/{filename}")
def delete_backup(filename: str, request: Request) -> Dict[str, Any]:
    _require_admin(request)
    eng = _engine()
    policy = eng.store.get_active_policy() or {}
    location = str((policy.get("backups") or {}).get("location") or "")
    res = eng.backups.delete(filename, location)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("message"))
    return res


@router.get("/backups/v2/{filename}/download")
def download_backup(filename: str, request: Request):
    """Served through the API because the desktop UI runs from file:// — a plain
    link to the path cannot be opened there, and the hosted UI is on another
    machine entirely."""
    _require_admin(request)
    eng = _engine()
    policy = eng.store.get_active_policy() or {}
    location = str((policy.get("backups") or {}).get("location") or "")
    clean = os.path.basename(str(filename or "").strip())
    path = os.path.join(eng.backups.backup_dir(location), clean)
    if not clean or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Backup not found.")
    return FileResponse(path, media_type="application/octet-stream", filename=clean)
