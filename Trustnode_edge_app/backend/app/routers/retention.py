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
        # Read-only: these are PRAGMA size probes, and a read/write handle
        # queues behind the historian writer - which is why the page showed
        # 0 MB for a 5 GB store (2026-10-02).
        with _store._connect_readonly() as conn:
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


# ------------------------------------------------- operator-driven deletion
# 2026-10-20, requested: "we also should have options to delete (real delete)
# data from local databases and cloud (supabase) to save space so the customer
# can control of their data when is logged with admin privilegy".
#
# Retention is a POLICY - it decides by age, on a schedule. This is the other
# thing an operator needs: "remove this, now, because I say so". Keeping the
# two apart matters: a manual purge that edited the policy would quietly
# change what happens every night afterwards.
#
# Three rules this endpoint does not bend:
#   * ADMIN only, enforced HERE, on the server. The legacy cleanup routes were
#     gated in the browser alone, so any authenticated viewer could wipe the
#     historian.
#   * TENANT SCOPED, always. The cloud database is shared between customers
#     and the edge connects with a role that bypasses RLS, so an unscoped
#     DELETE there would take another company's history with it. That is a
#     data-protection failure, not a bug (CLAUDE.md 1b).
#   * COUNT BEFORE DELETE. dry_run is the default, and a cutoff is required:
#     a purge with no cutoff is a wipe wearing a different name.

#: What an operator may purge, and where each thing actually lives.
_PURGE_TARGETS: Dict[str, Dict[str, str]] = {
    "historian_raw": {"db": "store", "table": "historian_readings", "ts": "ts_utc"},
    "historian_rollups": {"db": "store", "table": "historian_rollup", "ts": "bucket_ms"},
    "app_logs": {"db": "store", "table": "app_logs", "ts": "ts_utc"},
    "reports_metadata": {"db": "store", "table": "generated_reports", "ts": "created_utc"},
    "telemetry_cycles": {"db": "telemetry", "table": "telemetry_samples_raw",
                         "ts": "sample_ts_utc"},
    "telemetry_audit": {"db": "telemetry", "table": "ingest_audit_log_local",
                        "ts": "ts_utc"},
}


class PurgeIn(BaseModel):
    targets: List[str] = Field(default_factory=list)
    before_utc: str = ""
    scope: str = "local"          # local | cloud | both
    dry_run: bool = True
    confirm: bool = False


def _purge_tenant(request: Request) -> str:
    """This edge's tenant. Never a value the caller supplied."""
    from app.state import app_store as _store
    try:
        parts = str(_store.site_scope_key() or "").split("|")
        if parts and parts[0].strip():
            return parts[0].strip()
    except Exception:
        pass
    try:
        from app.tenant import get_current_tenant
        return str(get_current_tenant() or "default")
    except Exception:
        return "default"


def _purge_local(spec: Dict[str, str], tenant: str, before: str,
                 dry_run: bool) -> int:
    """Count, or delete, one table's rows older than `before` for one tenant."""
    import sqlite3
    from app.state import app_store as _store
    store_path = _store._db_path                                  # noqa: SLF001
    base = os.path.dirname(store_path)
    path = (store_path if spec["db"] == "store"
            else os.path.join(base, "trustnode_telemetry.db"))
    if not os.path.exists(path):
        return 0
    table, tscol = spec["table"], spec["ts"]
    conn = sqlite3.connect(path, timeout=60.0)
    conn.isolation_level = None
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info([%s])" % table)}
        if tscol not in cols:
            return 0
        # bucket_ms is epoch milliseconds; every other cutoff is an ISO string.
        if tscol == "bucket_ms":
            import datetime as _dt
            try:
                cut = int(_dt.datetime.fromisoformat(before).timestamp() * 1000)
            except Exception:
                return 0
            where, args = "bucket_ms < ?", [cut]
        else:
            where, args = "%s < ?" % tscol, [before]
        if "tenant_id" in cols:
            where += " AND tenant_id = ?"
            args.append(tenant)
        n = int(conn.execute("SELECT COUNT(*) FROM [%s] WHERE %s" % (table, where),
                             args).fetchone()[0] or 0)
        if dry_run or not n:
            return n
        # Paced: a large purge must not hold a write lock for minutes with
        # collection queued behind it.
        removed = 0
        while True:
            cur = conn.execute(
                "DELETE FROM [%s] WHERE rowid IN (SELECT rowid FROM [%s] "
                "WHERE %s LIMIT 5000)" % (table, table, where), args)
            got = max(0, int(cur.rowcount or 0))
            if not got:
                break
            removed += got
        return removed
    finally:
        conn.close()


def _purge_cloud(spec: Dict[str, str], tenant: str, before: str,
                 dry_run: bool) -> Dict[str, Any]:
    """The same, in the cloud database. Tenant-scoped without exception."""
    if spec["db"] != "store":
        return {"skipped": "not mirrored to the cloud"}
    from app.state import app_store as _store
    cloud = _store._get_cloud_database_target()                   # noqa: SLF001
    if not cloud:
        return {"skipped": "no cloud database configured"}
    try:
        from sqlalchemy import text
        schema = str(cloud.get("schema") or "public")
        engine, _ = _store._get_or_create_cloud_engine(cloud, schema)   # noqa: SLF001
        table = '"%s"."%s"' % (schema, spec["table"])
        tscol = spec["ts"]
        with engine.begin() as cx:
            cnt = cx.execute(
                text("SELECT COUNT(*) FROM %s WHERE %s < :b AND tenant_id = :t"
                     % (table, tscol)), {"b": before, "t": tenant}).scalar() or 0
            if dry_run or not cnt:
                return {"rows": int(cnt), "deleted": 0}
            cx.execute(
                text("DELETE FROM %s WHERE %s < :b AND tenant_id = :t"
                     % (table, tscol)), {"b": before, "t": tenant})
        return {"rows": int(cnt), "deleted": int(cnt)}
    except Exception as exc:  # noqa: BLE001
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


@router.get("/data/purge/targets")
def purge_targets(request: Request) -> Dict[str, Any]:
    """What can be purged, so the UI never has to invent a name."""
    _require_admin(request)
    return {"ok": True, "targets": sorted(_PURGE_TARGETS.keys()),
            "scopes": ["local", "cloud", "both"]}


@router.post("/data/purge")
def purge_data(payload: PurgeIn, request: Request) -> Dict[str, Any]:
    actor = _require_admin(request)
    targets = [t for t in (payload.targets or []) if t in _PURGE_TARGETS]
    if not targets:
        raise HTTPException(status_code=400,
                            detail="name at least one known target")
    before = str(payload.before_utc or "").strip()
    if not before:
        raise HTTPException(
            status_code=400,
            detail="before_utc is required - a purge with no cutoff is a wipe")
    dry = bool(payload.dry_run) or not bool(payload.confirm)
    scope = str(payload.scope or "local").strip().lower()
    if scope not in ("local", "cloud", "both"):
        raise HTTPException(status_code=400,
                            detail="scope must be local, cloud or both")
    tenant = _purge_tenant(request)

    out: Dict[str, Any] = {"ok": True, "dry_run": dry, "tenant_id": tenant,
                           "before_utc": before, "scope": scope, "results": {}}
    for name in targets:
        spec = _PURGE_TARGETS[name]
        entry: Dict[str, Any] = {}
        if scope in ("local", "both"):
            entry["local"] = _purge_local(spec, tenant, before, dry)
        if scope in ("cloud", "both"):
            entry["cloud"] = _purge_cloud(spec, tenant, before, dry)
        out["results"][name] = entry

    if not dry:
        try:
            from app.state import app_store as _store
            import datetime as _dt
            _now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            _store.append_log_rows([{
                "ts_utc": _now, "created_utc": _now, "level": "WARN",
                "category": "retention", "tenant_id": tenant,
                "message": ("admin purge by %s: %s before %s (scope=%s)"
                            % (actor, ",".join(targets), before, scope)),
            }])
        except Exception:
            pass
    return out


# ------------------------------------------------------------ usage metering
# 2026-10-20, asked: "this way I would know how big would be the customer
# database to cherge them." Nothing counted bytes per customer, so there was
# no number to bill against.
#
# Bytes are ESTIMATED, deliberately and visibly. SQLite can only report the
# size of a whole file, and one file holds several tenants; dbstat is not
# compiled into the shipped interpreter. So we take the real average cost of
# a row in each table - measured from the file itself, indexes included - and
# multiply by that tenant's row count. On 2026-10-20 that average was 646 B
# for a historian reading: 165 B of payload and the rest four indexes.
#
# An estimate labelled as one beats an exact number for the wrong thing (the
# file size), which is what a naive implementation would report.

_USAGE_TABLES = (
    ("historian_readings", "store", "full-detail history"),
    ("historian_rollup", "store", "summarised history"),
    ("app_logs", "store", "application logs"),
    ("generated_reports", "store", "report records"),
    ("telemetry_samples_raw", "telemetry", "collection cycles"),
    ("ingest_audit_log_local", "telemetry", "collection audit"),
)


def _usage_db_paths() -> Dict[str, str]:
    from app.state import app_store as _store
    store_path = _store._db_path                                  # noqa: SLF001
    return {"store": store_path,
            "telemetry": os.path.join(os.path.dirname(store_path),
                                      "trustnode_telemetry.db")}


def _avg_row_bytes(conn, table: str) -> float:
    """Real cost of one row in this table, indexes included.

    file_pages / total_rows across the whole file would be wrong when a file
    holds several tables, so scale the file by this table's share of the rows.
    Crude, but honest and stable - and it is the number an operator is
    actually billed on.
    """
    try:
        page = int(conn.execute("PRAGMA page_size").fetchone()[0])
        count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        free = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        used = max(0, count - free) * page
        rows_here = int(conn.execute("SELECT COUNT(*) FROM [%s]" % table).fetchone()[0] or 0)
        if rows_here <= 0:
            return 0.0
        total = 0
        for (t,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"):
            try:
                total += int(conn.execute("SELECT COUNT(*) FROM [%s]" % t).fetchone()[0] or 0)
            except Exception:
                pass
        if total <= 0:
            return 0.0
        return (used / float(total))
    except Exception:
        return 0.0


@router.get("/data/usage")
def data_usage(request: Request) -> Dict[str, Any]:
    """Rows and estimated bytes per tenant, per table."""
    _require_admin(request)
    import sqlite3
    paths = _usage_db_paths()
    per_tenant: Dict[str, Dict[str, Any]] = {}
    tables: List[Dict[str, Any]] = []

    for table, which, label in _USAGE_TABLES:
        path = paths.get(which) or ""
        if not path or not os.path.exists(path):
            continue
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"),
                                   uri=True, timeout=30)
        except Exception:
            continue
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info([%s])" % table)}
            if not cols:
                continue
            per_row = _avg_row_bytes(conn, table)
            if "tenant_id" in cols:
                rows = conn.execute(
                    "SELECT tenant_id, COUNT(*) FROM [%s] GROUP BY tenant_id" % table
                ).fetchall()
            else:
                rows = [("(untenanted)",
                         conn.execute("SELECT COUNT(*) FROM [%s]" % table).fetchone()[0])]
            total = 0
            for tenant, n in rows:
                tid = str(tenant or "(none)")
                n = int(n or 0)
                total += n
                slot = per_tenant.setdefault(tid, {"rows": 0, "est_bytes": 0.0,
                                                   "tables": {}})
                slot["rows"] += n
                slot["est_bytes"] += n * per_row
                slot["tables"][table] = {"rows": n,
                                         "est_bytes": int(n * per_row)}
            tables.append({"table": table, "label": label, "database": which,
                           "rows": total, "avg_row_bytes": round(per_row, 1)})
        finally:
            conn.close()

    out_tenants = []
    for tid, v in sorted(per_tenant.items(), key=lambda kv: -kv[1]["rows"]):
        out_tenants.append({"tenant_id": tid, "rows": v["rows"],
                            "est_bytes": int(v["est_bytes"]),
                            "tables": v["tables"]})
    files = {}
    for which, path in paths.items():
        files[which] = {"path": path,
                        "bytes": os.path.getsize(path) if os.path.exists(path) else 0}
    return {"ok": True, "estimated": True, "tenants": out_tenants,
            "tables": tables, "files": files,
            "note": ("Bytes are estimated: a SQLite file reports its own size "
                     "only, and one file holds several tenants. Rows are exact.")}
