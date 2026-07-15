"""FastAPI router for Batch Management v2 (clean rebuild).

Mounted at /api/batch-management/v2 — a PARALLEL base to the legacy
/api/batch-management router, so both coexist. The new UI targets v2; the legacy
endpoints remain but are unused by the new pages.

Every mutating/reading endpoint is gated by require_batch_management_license()
(same license gate as the legacy router). Report generation/preview/email reuse
the EXISTING Report module + email module via ReportIntegrationService.

Guide: docs/BATCH_MANAGEMENT_REDESIGN_2026-07-14.md
"""
from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Body, Query
from fastapi.responses import FileResponse

from .license import require_batch_management_license, is_batch_management_enabled, MODULE_KEY
from .models_v2 import (
    BatchDefinitionIn, BatchGroupIn, BatchIn, BatchActionIn, BatchCommentIn, ExcursionAckIn,
)
from .service_v2 import (
    BatchDefinitionService, BatchExecutionService, BatchGroupService, BatchStateError,
)
from .calc_v2 import BatchCalcService
from .reports_v2 import ReportIntegrationService, seed_report_templates, list_batch_templates


router = APIRouter(prefix="/api/batch-management/v2", tags=["batch-management-v2"])

_LIC = [Depends(require_batch_management_license)]


def _store():
    from app.state import app_store
    return app_store


def _defs():
    return BatchDefinitionService(_store())


def _exe():
    return BatchExecutionService(_store())


def _groups():
    return BatchGroupService(_store())


def _calc():
    return BatchCalcService(_store())


def _reports():
    return ReportIntegrationService(_store())


def _actor(request: Request) -> Optional[str]:
    try:
        u = getattr(request.state, "current_user", None)
        if isinstance(u, dict):
            return str(u.get("username") or u.get("name") or "") or None
    except Exception:
        pass
    return None


def _guard(fn):
    """Translate service-layer errors into HTTP codes."""
    try:
        return fn()
    except BatchStateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---- status (UNGATED) ----------------------------------------------------
@router.get("/status")
def status() -> dict:
    enabled, reason = is_batch_management_enabled()
    return {"module": MODULE_KEY, "enabled": bool(enabled), "reason": reason, "api": "v2"}


@router.post("/seed-report-templates", dependencies=_LIC)
def seed_templates() -> dict:
    return {"ok": True, "created": seed_report_templates()}


@router.get("/report-templates", dependencies=_LIC)
def report_templates() -> dict:
    """Report templates the definition wizard can pick from, split into
    {batch, group}. Includes CUSTOM templates created in the Reports module
    (any template scoped batch/group, or untyped -> offered in both). Seeds the
    defaults first so a fresh install always has the built-ins."""
    try:
        seed_report_templates()
    except Exception:
        pass
    return list_batch_templates()


# ---- Batch Definitions ---------------------------------------------------
@router.get("/definitions", dependencies=_LIC)
def list_definitions() -> dict:
    return {"rows": _defs().list_definitions()}


@router.post("/definitions", dependencies=_LIC)
def create_definition(payload: BatchDefinitionIn, request: Request) -> dict:
    row = _guard(lambda: _defs().save_definition(payload.model_dump(), actor=_actor(request)))
    return {"ok": True, "row": row}


@router.get("/definitions/{definition_id}", dependencies=_LIC)
def get_definition(definition_id: str, version_id: Optional[str] = None) -> dict:
    row = _defs().get_definition(definition_id, version_id=version_id)
    if not row:
        raise HTTPException(status_code=404, detail="definition not found")
    return {"row": row}


@router.put("/definitions/{definition_id}", dependencies=_LIC)
def update_definition(definition_id: str, payload: BatchDefinitionIn, request: Request) -> dict:
    row = _guard(lambda: _defs().save_definition(payload.model_dump(), actor=_actor(request), definition_id=definition_id))
    return {"ok": True, "row": row}


@router.delete("/definitions/{definition_id}", dependencies=_LIC)
def delete_definition(definition_id: str, request: Request) -> dict:
    return {"ok": _guard(lambda: _defs().delete_definition(definition_id, actor=_actor(request)))}


@router.post("/definitions/{definition_id}/validate", dependencies=_LIC)
def validate_definition(definition_id: str) -> dict:
    return _defs().validate_definition(definition_id)


@router.post("/definitions/{definition_id}/publish", dependencies=_LIC)
def publish_definition(definition_id: str, request: Request) -> dict:
    row = _guard(lambda: _defs().publish_definition(definition_id, actor=_actor(request)))
    return {"ok": True, "row": row}


@router.get("/definitions/{definition_id}/versions", dependencies=_LIC)
def list_versions(definition_id: str) -> dict:
    return {"rows": _defs().list_versions(definition_id)}


@router.post("/definitions/{definition_id}/versions", dependencies=_LIC)
def new_version(definition_id: str, request: Request) -> dict:
    row = _guard(lambda: _defs().new_version(definition_id, actor=_actor(request)))
    return {"ok": True, "row": row}


# ---- Batches -------------------------------------------------------------
@router.get("/batches", dependencies=_LIC)
def list_batches(
    limit: int = 200, offset: int = 0, status: Optional[str] = None,
    batch_group_id: Optional[str] = None, definition_id: Optional[str] = None,
    equipment_id: Optional[str] = None, search: Optional[str] = None,
) -> dict:
    rows, total = _exe().list_batches(
        limit=limit, offset=offset, status=status, batch_group_id=batch_group_id,
        definition_id=definition_id, equipment_id=equipment_id, search=search)
    return {"rows": rows, "total": total}


@router.post("/batches", dependencies=_LIC)
def create_batch(payload: BatchIn, request: Request) -> dict:
    row = _guard(lambda: _exe().create_batch(payload.model_dump(), actor=_actor(request), source="manual"))
    return {"ok": True, "row": row}


@router.get("/batches/{batch_id}", dependencies=_LIC)
def get_batch(batch_id: str) -> dict:
    row = _exe().get_batch(batch_id)
    if not row:
        raise HTTPException(status_code=404, detail="batch not found")
    return {"row": row}


def _terminal_hook(batch_id: str, actor: Optional[str]) -> None:
    """After a batch reaches a terminal state: compute + optionally auto-report/email.
    Best-effort; never raises."""
    try:
        _calc().compute_batch(batch_id)
    except Exception:
        pass
    try:
        _reports().on_batch_terminal(batch_id, actor=actor)
    except Exception:
        pass


@router.post("/batches/{batch_id}/start", dependencies=_LIC)
def start_batch(batch_id: str, payload: BatchActionIn, request: Request) -> dict:
    row = _guard(lambda: _exe().start_batch(batch_id, actor=_actor(request), source="manual",
                                            reason=payload.reason, equipment_id=payload.equipment_id))
    return {"ok": True, "row": row}


@router.post("/batches/{batch_id}/stop", dependencies=_LIC)
def stop_batch(batch_id: str, payload: BatchActionIn, request: Request) -> dict:
    actor = _actor(request)
    row = _guard(lambda: _exe().stop_batch(batch_id, actor=actor, source="manual",
                                           reason=payload.reason, quality_status=payload.quality_status))
    _terminal_hook(batch_id, actor)
    return {"ok": True, "row": _exe().get_batch(batch_id) or row}


@router.post("/batches/{batch_id}/hold", dependencies=_LIC)
def hold_batch(batch_id: str, payload: BatchActionIn, request: Request) -> dict:
    row = _guard(lambda: _exe().hold_batch(batch_id, actor=_actor(request), source="manual", reason=payload.reason))
    return {"ok": True, "row": row}


@router.post("/batches/{batch_id}/resume", dependencies=_LIC)
def resume_batch(batch_id: str, payload: BatchActionIn, request: Request) -> dict:
    row = _guard(lambda: _exe().resume_batch(batch_id, actor=_actor(request), source="manual"))
    return {"ok": True, "row": row}


@router.post("/batches/{batch_id}/abort", dependencies=_LIC)
def abort_batch(batch_id: str, payload: BatchActionIn, request: Request) -> dict:
    actor = _actor(request)
    row = _guard(lambda: _exe().abort_batch(batch_id, actor=actor, source="manual", reason=payload.reason))
    _terminal_hook(batch_id, actor)
    return {"ok": True, "row": _exe().get_batch(batch_id) or row}


@router.post("/batches/{batch_id}/comments", dependencies=_LIC)
def add_comment(batch_id: str, payload: BatchCommentIn, request: Request) -> dict:
    return _guard(lambda: _exe().add_comment(batch_id, payload.message, actor=payload.actor or _actor(request)))


@router.get("/batches/{batch_id}/events", dependencies=_LIC)
def list_events(batch_id: str, limit: int = 200) -> dict:
    return {"rows": _exe().list_events(batch_id, limit=limit)}


@router.get("/batches/{batch_id}/trends", dependencies=_LIC)
def batch_trends(batch_id: str, tags: str = "", max_points: int = 400) -> dict:
    """Downsampled per-tag series over the batch window (read-only historian join).
    Reuses the legacy chart helper's approach via the v2 window reads."""
    from .service import BatchService  # legacy chart helper works on windows generically
    taglist = [t.strip() for t in (tags or "").split(",") if t.strip()]
    exe = _exe()
    if not taglist:
        taglist = exe.collected_tags_in_window(batch_id)
    # Build the series directly from v2 windows (mirror of legacy chart_series_for_batch).
    rows = exe.historian_rows_for_batch(batch_id, limit=50000)
    from collections import defaultdict
    import math
    series_pts: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        tg = str(r.get("tag_name") or "")
        if taglist and tg not in taglist:
            continue
        v = r.get("value")
        if v is None:
            continue
        series_pts[tg].append({"ts": str(r.get("ts_utc") or ""), "value": float(v)})
    out_series = []
    for tg, pts in series_pts.items():
        pts.sort(key=lambda p: p["ts"])
        n = len(pts)
        stride = max(1, math.ceil(n / max(1, max_points)))
        ds = pts[::stride]
        if ds and ds[-1] is not pts[-1]:
            ds.append(pts[-1])
        vals = [p["value"] for p in pts]
        out_series.append({"tag": tg, "points": ds,
                           "min": min(vals) if vals else None,
                           "max": max(vals) if vals else None,
                           "avg": (sum(vals) / len(vals)) if vals else None})
    return {"series": out_series}


@router.get("/batches/{batch_id}/kpis", dependencies=_LIC)
def batch_kpis(batch_id: str) -> dict:
    return {"rows": _calc().list_kpis(batch_id)}


@router.post("/batches/{batch_id}/recompute", dependencies=_LIC)
def recompute_batch(batch_id: str) -> dict:
    return {"ok": True, **_guard(lambda: _calc().compute_batch(batch_id))}


@router.get("/batches/{batch_id}/excursions", dependencies=_LIC)
def batch_excursions(batch_id: str) -> dict:
    return {"rows": _calc().list_excursions(batch_id=batch_id)}


@router.get("/batches/{batch_id}/collected-tags", dependencies=_LIC)
def batch_collected_tags(batch_id: str) -> dict:
    return {"tags": _exe().collected_tags_in_window(batch_id)}


@router.get("/batches/{batch_id}/matrix", dependencies=_LIC)
def batch_matrix(batch_id: str, tags: str = "", max_rows: int = 200) -> dict:
    """Aligned tag matrix (rows=timestamps, cols=tags, per-row in-limits),
    downsampled. Powers the single-batch time-series section + group child
    expand."""
    taglist = [t.strip() for t in (tags or "").split(",") if t.strip()] or None
    return _exe().tag_matrix(batch_id, tags=taglist, max_rows=max(10, min(max_rows, 2000)))


# ---- Custom properties (barcode / order # / equipment / ...) -------------
@router.get("/batches/{batch_id}/properties", dependencies=_LIC)
def batch_properties(batch_id: str) -> dict:
    """Captured property values for a batch (detail header + reports)."""
    return {"rows": _exe().list_properties(batch_id)}


@router.get("/batches/{batch_id}/definition-properties", dependencies=_LIC)
def batch_definition_properties(batch_id: str) -> dict:
    """Property SCHEMA that applies to this batch, so the UI can prompt the
    operator for the MANUAL ones at start (linked ones snapshot automatically)."""
    return {"rows": _exe().definition_properties_for_batch(batch_id)}


# ---- Batch reports (reuse Report module) --------------------------------
@router.get("/batches/{batch_id}/reports", dependencies=_LIC)
def list_batch_reports(batch_id: str) -> dict:
    return {"rows": _reports().list_batch_reports(batch_id)}


@router.post("/batches/{batch_id}/reports", dependencies=_LIC)
def generate_batch_report(batch_id: str, request: Request, payload: dict = Body(default={})) -> dict:
    tpl = (payload or {}).get("template_id")
    return _guard(lambda: _reports().generate_batch_report(batch_id, template_id=tpl,
                                                           triggered_by="manual", actor=_actor(request)))


@router.post("/batches/{batch_id}/reports/{reference_id}/email", dependencies=_LIC)
def email_batch_report(batch_id: str, reference_id: str, payload: dict = Body(default={})) -> dict:
    p = payload or {}
    return _guard(lambda: _reports().email_report(
        reference_id, recipients=p.get("recipients") or [], subject=p.get("subject"),
        body=p.get("html_body") or p.get("body"), email_settings=p.get("email_settings")))


# ---- Batch Groups --------------------------------------------------------
@router.get("/groups", dependencies=_LIC)
def list_groups(limit: int = 200, offset: int = 0, status: Optional[str] = None) -> dict:
    rows, total = _groups().list_groups(status=status, limit=limit, offset=offset)
    return {"rows": rows, "total": total}


@router.post("/groups", dependencies=_LIC)
def create_group(payload: BatchGroupIn, request: Request) -> dict:
    row = _guard(lambda: _groups().create_group(payload.model_dump(), actor=_actor(request)))
    return {"ok": True, "row": row}


@router.get("/groups/{group_id}", dependencies=_LIC)
def get_group(group_id: str) -> dict:
    row = _groups().get_group(group_id)
    if not row:
        raise HTTPException(status_code=404, detail="group not found")
    return {"row": row}


@router.post("/groups/{group_id}/complete", dependencies=_LIC)
def complete_group(group_id: str, request: Request) -> dict:
    actor = _actor(request)
    row = _guard(lambda: _groups().complete_group(group_id, actor=actor))
    try:
        _calc().compute_group(group_id)
        _reports().on_group_terminal(group_id, actor=actor)
    except Exception:
        pass
    return {"ok": True, "row": _groups().get_group(group_id) or row}


@router.post("/groups/{group_id}/abort", dependencies=_LIC)
def abort_group(group_id: str, request: Request) -> dict:
    row = _guard(lambda: _groups().abort_group(group_id, actor=_actor(request)))
    return {"ok": True, "row": row}


@router.get("/groups/{group_id}/batches", dependencies=_LIC)
def group_batches(group_id: str) -> dict:
    return {"rows": _groups().child_batches(group_id)}


@router.get("/groups/{group_id}/kpis", dependencies=_LIC)
def group_kpis(group_id: str) -> dict:
    return {"rows": _calc().list_group_kpis(group_id)}


@router.post("/groups/{group_id}/recompute", dependencies=_LIC)
def recompute_group(group_id: str) -> dict:
    return {"ok": True, **_guard(lambda: _calc().compute_group(group_id))}


@router.get("/groups/{group_id}/reports", dependencies=_LIC)
def list_group_reports(group_id: str) -> dict:
    return {"rows": _reports().list_group_reports(group_id)}


@router.post("/groups/{group_id}/reports", dependencies=_LIC)
def generate_group_report(group_id: str, request: Request, payload: dict = Body(default={})) -> dict:
    tpl = (payload or {}).get("template_id")
    return _guard(lambda: _reports().generate_group_report(group_id, template_id=tpl,
                                                           triggered_by="manual", actor=_actor(request)))


@router.post("/groups/{group_id}/reports/{reference_id}/email", dependencies=_LIC)
def email_group_report(group_id: str, reference_id: str, payload: dict = Body(default={})) -> dict:
    p = payload or {}
    return _guard(lambda: _reports().email_report(
        reference_id, recipients=p.get("recipients") or [], subject=p.get("subject"),
        body=p.get("html_body") or p.get("body"), email_settings=p.get("email_settings")))


# ---- Analysis ------------------------------------------------------------
@router.get("/analysis/excursions", dependencies=_LIC)
def analysis_excursions(limit: int = 500) -> dict:
    return {"rows": _calc().list_excursions(limit=limit)}


@router.post("/analysis/excursions/{excursion_id}/ack", dependencies=_LIC)
def ack_excursion(excursion_id: str, payload: ExcursionAckIn, request: Request) -> dict:
    row = _guard(lambda: _calc().acknowledge_excursion(
        excursion_id, acknowledged=payload.acknowledged,
        actor=payload.actor or _actor(request), comment=payload.comment))
    return {"ok": True, "row": row}


@router.get("/analysis/comparison", dependencies=_LIC)
def analysis_comparison(batch_ids: str = Query(""), tags: str = Query(""), max_points: int = 400) -> dict:
    """Elapsed-time-aligned trends for a few batches (spec Batch Comparison).
    Returns per-batch, per-tag series with an elapsed-seconds x-axis so charts
    can overlay batches of different absolute times."""
    ids = [b.strip() for b in batch_ids.split(",") if b.strip()][:6]
    taglist = [t.strip() for t in tags.split(",") if t.strip()]
    exe = _exe()
    import math
    out = []
    for bid in ids:
        b = exe.get_batch(bid)
        if not b:
            continue
        rows = exe.historian_rows_for_batch(bid, limit=50000)
        # elapsed from earliest ts in window
        ts_all = sorted({str(r.get("ts_utc") or "") for r in rows if r.get("ts_utc")})
        base = ts_all[0] if ts_all else None
        from .service import _window_seconds
        series = {}
        for r in rows:
            tg = str(r.get("tag_name") or "")
            if taglist and tg not in taglist:
                continue
            v = r.get("value")
            if v is None or not base:
                continue
            series.setdefault(tg, []).append(
                {"elapsed_s": _window_seconds(base, str(r.get("ts_utc") or "")), "value": float(v)})
        packed = []
        for tg, pts in series.items():
            pts.sort(key=lambda p: p["elapsed_s"])
            stride = max(1, math.ceil(len(pts) / max(1, max_points)))
            packed.append({"tag": tg, "points": pts[::stride]})
        out.append({"batch_id": bid, "reference": b.get("reference"), "series": packed})
    return {"batches": out}
