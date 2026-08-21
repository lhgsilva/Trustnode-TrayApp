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
    BatchDefinitionIn, BatchGroupIn, BatchIn, BatchActionIn, BatchScanIn, BatchCommentIn, ExcursionAckIn,
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


@router.post("/report-templates/{template_id}/duplicate", dependencies=_LIC)
def duplicate_report_template(template_id: str, payload: dict = Body(default={})) -> dict:
    """Duplicate a batch/group report template into a new editable template
    (same definition + batch_scope, fresh id + name). The copy lands in the
    shared Reports store, so it's listable/editable/exportable in the Reports
    module and selectable in the definition wizard."""
    from app.services.reports_store import _new_id
    from .reports_v2 import _reports_store
    rs = _reports_store()
    src = rs.get_template(template_id)
    if not src:
        raise HTTPException(status_code=404, detail="Template not found")
    definition = dict(src.get("definition") or {})
    # preserve batch/group classification so the copy stays in the right list
    if "batch_scope" not in definition and src.get("batch_scope"):
        definition["batch_scope"] = src.get("batch_scope")
    new_name = str((payload or {}).get("name") or f"{src.get('name') or 'Batch Report'} (copy)")
    created = rs.upsert_template({
        "id": _new_id("tpl"),
        "name": new_name,
        "description": src.get("description") or "",
        "definition": definition,
    })
    return {"ok": True, "template": created}


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


@router.delete("/batches/{batch_id}", dependencies=_LIC)
def delete_batch(batch_id: str, request: Request) -> dict:
    return {"ok": _guard(lambda: _exe().delete_batch(batch_id, actor=_actor(request)))}


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


def _barcode_rule(batch_id: str, which: str) -> Optional[dict]:
    """The barcode rule dict for start_config|stop_config, or None when the
    batch's definition doesn't gate that transition on a barcode."""
    exe = _exe()
    b = exe.get_batch(batch_id)
    if not b or not b.get("definition_version_id"):
        return None
    cfg = exe.version_config_for_batch(batch_id) or {}
    node = cfg.get(which) or {}
    if str(node.get("method") or "") != "barcode":
        return None
    return node.get("barcode") or {}


@router.post("/batches/scan", dependencies=_LIC)
def scan_batch(payload: BatchScanIn, request: Request) -> dict:
    """One-shot resolver for a scanned/typed barcode (operator 2026-07-30).

    Used by the dashboard Batch ID widget + the Batch Overview scan field, so a
    keyboard-wedge scanner needs NO other UI interaction. Precedence:
      1. STOP a running/held batch whose definition stop-mode is barcode and
         the code passes its rule (incl. same-code-as-start).
      2. If a running batch was STARTED with this exact code -> idempotent
         no-op (prevents duplicate batches on a double scan).
      3. START an existing planned/ready batch whose start-mode is barcode and
         the code passes its rule.
      4. CREATE + START a batch from the single published definition whose
         start-mode is barcode and whose rule accepts the code (scoped to
         payload.definition_id when the widget is configured for one). When the
         definition's identification method is 'barcode', the code becomes the
         batch reference.
      5. Widget scoped to a specific non-barcode definition: the scan is the
         manual start — create under that definition with the code as reference.
      6. Only when NO barcode-gated definition exists at all: legacy ad-hoc
         scan-to-start (reference = code), matching the old v1 widget.
    Returns {ok, action: started|stopped|already_running, row}.
    """
    from .models_v2 import validate_barcode
    exe = _exe()
    actor = (payload.actor or "").strip() or _actor(request)
    code = (payload.barcode or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="Empty barcode.")
    scope_def = (payload.definition_id or "").strip() or None

    def _rule_of(cfg: Optional[dict], which: str) -> Optional[dict]:
        node = (cfg or {}).get(which) or {}
        if str(node.get("method") or "") != "barcode":
            return None
        return node.get("barcode") or {}

    def _batches(status: str) -> list:
        rows, _total = exe.list_batches(limit=200, status=status)
        if scope_def:
            rows = [b for b in rows if str(b.get("definition_id") or "") == scope_def]
        return rows

    # 1) stop a barcode-gated active batch this code satisfies
    active = _batches("running") + _batches("held")
    for b in active:
        rule = _rule_of(exe.version_config_for_batch(b["id"]), "stop_config")
        if rule is None:
            continue
        start_code = ((b.get("metadata") or {}).get("start_barcode"))
        if validate_barcode(rule, code, start_code=start_code) is None:
            row = _guard(lambda: exe.stop_batch(b["id"], actor=actor, source="barcode",
                                                reason=f"barcode scan {code}"))
            _terminal_hook(b["id"], actor)
            return {"ok": True, "action": "stopped", "row": exe.get_batch(b["id"]) or row}

    # 2) double-scan guard: this code already started a running batch
    for b in active:
        if str((b.get("metadata") or {}).get("start_barcode") or "") == code:
            return {"ok": True, "action": "already_running", "row": b}

    def _start(batch_id: str) -> dict:
        row = _guard(lambda: exe.start_batch(batch_id, actor=actor, source="barcode",
                                             reason=f"barcode scan {code}"))
        try:
            exe.set_batch_metadata(batch_id, {"start_barcode": code})
        except Exception:
            pass
        return {"ok": True, "action": "started", "row": exe.get_batch(batch_id) or row}

    # 3) start an existing planned/ready barcode-gated batch
    for b in _batches("ready") + _batches("planned"):
        rule = _rule_of(exe.version_config_for_batch(b["id"]), "start_config")
        if rule is not None and validate_barcode(rule, code) is None:
            return _start(b["id"])

    # 4) create + start from a published barcode-start definition
    defs = _defs()
    matches, errors = [], []
    for d in defs.list_definitions():
        if str(d.get("status") or "") != "published":
            continue
        if scope_def and str(d.get("id") or "") != scope_def:
            continue
        cfg = (defs.get_definition(d["id"]) or {}).get("config") or {}
        rule = _rule_of(cfg, "start_config")
        if rule is None:
            continue
        err = validate_barcode(rule, code)
        if err:
            errors.append(f"{d.get('name') or d['id']}: {err}")
        else:
            matches.append((d, cfg))
    if len(matches) > 1:
        names = ", ".join(str(d.get("name") or d["id"]) for d, _cfg in matches)
        raise HTTPException(
            status_code=409,
            detail=f"Barcode matches several definitions ({names}). "
                   f"Configure the scan widget with a specific definition.")
    if len(matches) == 1:
        d, cfg = matches[0]
        ident_method = str(((cfg.get("identification") or {}).get("method")) or "")
        created = _guard(lambda: exe.create_batch(
            {"definition_id": d["id"],
             "reference": code if ident_method == "barcode" else None},
            actor=actor, source="barcode"))
        return _start(created["id"])
    if errors:
        # Barcode-gated definitions exist but none accepted the code — surface
        # WHY instead of silently creating an ad-hoc batch from a typo.
        raise HTTPException(status_code=400, detail=errors[0])

    # 5) widget scoped to a specific NON-barcode definition -> the scan acts as
    #    the manual start for it: create under that definition, code = reference.
    if scope_def:
        if not defs.get_definition(scope_def):
            raise HTTPException(status_code=404, detail="Configured batch definition not found.")
        created = _guard(lambda: exe.create_batch(
            {"definition_id": scope_def, "reference": code}, actor=actor, source="barcode"))
        return _start(created["id"])

    # 6) no barcode-gated definitions on this system -> legacy ad-hoc behavior
    created = _guard(lambda: exe.create_batch({"reference": code}, actor=actor, source="barcode"))
    return _start(created["id"])


@router.post("/batches/{batch_id}/start", dependencies=_LIC)
def start_batch(batch_id: str, payload: BatchActionIn, request: Request) -> dict:
    from .models_v2 import validate_barcode
    rule = _barcode_rule(batch_id, "start_config")
    if rule is not None:
        err = validate_barcode(rule, payload.barcode)
        if err:
            raise HTTPException(status_code=400, detail=err)
    row = _guard(lambda: _exe().start_batch(batch_id, actor=_actor(request), source="manual",
                                            reason=payload.reason, equipment_id=payload.equipment_id))
    # Remember the start barcode so a same-code stop rule can compare against it.
    if payload.barcode:
        try:
            _exe().set_batch_metadata(batch_id, {"start_barcode": payload.barcode.strip()})
        except Exception:
            pass
    return {"ok": True, "row": _exe().get_batch(batch_id) or row}


@router.post("/batches/{batch_id}/stop", dependencies=_LIC)
def stop_batch(batch_id: str, payload: BatchActionIn, request: Request) -> dict:
    from .models_v2 import validate_barcode
    actor = _actor(request)
    rule = _barcode_rule(batch_id, "stop_config")
    if rule is not None:
        b = _exe().get_batch(batch_id)
        start_code = ((b or {}).get("metadata") or {}).get("start_barcode")
        err = validate_barcode(rule, payload.barcode, start_code=start_code)
        if err:
            raise HTTPException(status_code=400, detail=err)
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


# Live-KPI throttle: recompute a RUNNING/HELD batch's KPIs at most this often
# when its KPIs are polled, so the batch page shows live KPIs without a separate
# recompute call or hammering the historian every poll.
_LIVE_KPI_MIN_INTERVAL_S = 3.0
_live_kpi_last: dict[str, float] = {}


@router.get("/batches/{batch_id}/kpis", dependencies=_LIC)
def batch_kpis(batch_id: str) -> dict:
    calc = _calc()
    # Live KPIs: if the batch is still collecting, recompute over the open window
    # (throttled) so values update as data arrives — not just on stop.
    try:
        b = _exe().get_batch(batch_id)
        if b and str(b.get("status") or "") in ("running", "held"):
            import time as _t
            now = _t.monotonic()
            last = _live_kpi_last.get(batch_id, 0.0)
            if now - last >= _LIVE_KPI_MIN_INTERVAL_S:
                _live_kpi_last[batch_id] = now
                try:
                    calc.compute_batch(batch_id)
                except Exception:
                    pass  # transient (partial window) — serve last-known KPIs
    except Exception:
        pass
    return {"rows": calc.list_kpis(batch_id)}


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
def batch_matrix(batch_id: str, tags: str = "", max_rows: int = 5000) -> dict:
    """Aligned tag matrix (rows=timestamps, cols=tags, per-row in-limits).
    Returns EVERY collected row up to max_rows so the table reflects the
    gateway's real collection cadence (e.g. one row per second for a 1s
    gateway); only very long batches are downsampled, and the response flags
    that. Powers the single-batch time-series section + group child expand."""
    taglist = [t.strip() for t in (tags or "").split(",") if t.strip()] or None
    return _exe().tag_matrix(batch_id, tags=taglist, max_rows=max(10, min(max_rows, 50000)))


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


@router.delete("/batches/{batch_id}/reports/{reference_id}", dependencies=_LIC)
def delete_batch_report(batch_id: str, reference_id: str, request: Request) -> dict:
    return {"ok": _guard(lambda: _reports().delete_report(reference_id, actor=_actor(request)))}


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


@router.delete("/groups/{group_id}", dependencies=_LIC)
def delete_group(group_id: str, request: Request) -> dict:
    return {"ok": _guard(lambda: _groups().delete_group(group_id, actor=_actor(request)))}


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


@router.delete("/groups/{group_id}/reports/{reference_id}", dependencies=_LIC)
def delete_group_report(group_id: str, reference_id: str, request: Request) -> dict:
    return {"ok": _guard(lambda: _reports().delete_report(reference_id, actor=_actor(request)))}


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
