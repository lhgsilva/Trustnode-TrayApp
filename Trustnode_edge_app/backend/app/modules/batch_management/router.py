"""FastAPI router for the Batch Management module.

Mounted at /api/batch-management. Every endpoint is gated by
require_batch_management_license() so the entire surface is invisible
(404) on installs without the license.

Auth/tenant is provided by the global middleware that wraps every
request — same pattern as backend/app/routers/plc.py.
"""
from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from .license import require_batch_management_license, is_batch_management_enabled, MODULE_KEY
from .models import (
    BatchTypeIn, BatchIn, BatchStart, BatchStop, BatchValidationIn, BatchEventIn,
)
from .reports import render_single_batch_pdf


router = APIRouter(prefix="/api/batch-management", tags=["batch-management"])


def _service():
    """Resolve the BatchService lazily so app startup doesn't pay the
    import cost when the module is unlicensed."""
    from app.state import app_store
    from .service import BatchService
    return BatchService(app_store)


def _actor(request: Request) -> Optional[str]:
    """Pull the username off the request state set by the auth middleware."""
    try:
        u = getattr(request.state, "current_user", None)
        if isinstance(u, dict):
            return str(u.get("username") or u.get("name") or "") or None
    except Exception:
        pass
    return None


# ---- module status (UNGATED so the frontend can decide whether to render)
@router.get("/status")
def get_status() -> dict:
    enabled, reason = is_batch_management_enabled()
    return {"module": MODULE_KEY, "enabled": bool(enabled), "reason": reason}


# ---- batch types -----------------------------------------------------
@router.get("/batch-types", dependencies=[Depends(require_batch_management_license)])
def list_batch_types() -> dict:
    return {"rows": _service().list_batch_types()}


@router.post("/batch-types", dependencies=[Depends(require_batch_management_license)])
def create_batch_type(payload: BatchTypeIn, request: Request) -> dict:
    out = _service().save_batch_type(payload.model_dump(exclude_none=True), actor=_actor(request))
    return {"ok": True, "row": out}


@router.put("/batch-types/{batch_type_id}", dependencies=[Depends(require_batch_management_license)])
def update_batch_type(batch_type_id: str, payload: BatchTypeIn, request: Request) -> dict:
    try:
        out = _service().save_batch_type(
            payload.model_dump(exclude_none=True),
            actor=_actor(request),
            batch_type_id=batch_type_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="batch type not found")
    return {"ok": True, "row": out}


@router.delete("/batch-types/{batch_type_id}", dependencies=[Depends(require_batch_management_license)])
def delete_batch_type(batch_type_id: str, request: Request) -> dict:
    ok = _service().delete_batch_type(batch_type_id, actor=_actor(request))
    if not ok:
        raise HTTPException(status_code=404, detail="batch type not found")
    return {"ok": True}


# ---- batches ---------------------------------------------------------
@router.get("/batches", dependencies=[Depends(require_batch_management_license)])
def list_batches(
    limit: int = 200,
    offset: int = 0,
    status: Optional[str] = None,
    batch_type_id: Optional[str] = None,
    parent_batch_id: Optional[str] = None,
    search: Optional[str] = None,
) -> dict:
    rows, total = _service().list_batches(
        limit=limit, offset=offset, status_filter=status,
        batch_type_id=batch_type_id, parent_batch_id=parent_batch_id, search=search,
    )
    return {"rows": rows, "total": total}


@router.get("/batches/{batch_id}", dependencies=[Depends(require_batch_management_license)])
def get_batch(batch_id: str) -> dict:
    row = _service().get_batch(batch_id)
    if not row:
        raise HTTPException(status_code=404, detail="batch not found")
    return {"row": row}


@router.post("/batches", dependencies=[Depends(require_batch_management_license)])
def create_batch(payload: BatchIn, request: Request) -> dict:
    out = _service().create_batch(payload.model_dump(exclude_none=True), actor=_actor(request))
    return {"ok": True, "row": out}


@router.post("/batches/{batch_id}/start", dependencies=[Depends(require_batch_management_license)])
def start_batch(batch_id: str, payload: BatchStart, request: Request) -> dict:
    try:
        out = _service().start_batch(
            batch_id,
            operator=payload.operator,
            notes=payload.notes,
            gateway_id=payload.gateway_id,
            actor=_actor(request),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="batch not found")
    return {"ok": True, "row": out}


@router.post("/batches/{batch_id}/stop", dependencies=[Depends(require_batch_management_license)])
def stop_batch(batch_id: str, payload: BatchStop, request: Request) -> dict:
    try:
        out = _service().stop_batch(
            batch_id, result=payload.result, operator=payload.operator,
            notes=payload.notes, actor=_actor(request),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="batch not found")
    return {"ok": True, "row": out}


@router.post("/batches/{batch_id}/validate", dependencies=[Depends(require_batch_management_license)])
def validate_batch(batch_id: str, payload: BatchValidationIn, request: Request) -> dict:
    try:
        out = _service().validate_batch(
            batch_id, decision=payload.decision, notes=payload.notes,
            actor=_actor(request),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="batch not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "row": out}


@router.post("/batches/{batch_id}/events", dependencies=[Depends(require_batch_management_license)])
def add_event(batch_id: str, payload: BatchEventIn, request: Request) -> dict:
    return _service().add_event(batch_id, payload.model_dump(exclude_none=True), actor=_actor(request))


@router.get("/batches/{batch_id}/events", dependencies=[Depends(require_batch_management_license)])
def list_events(batch_id: str, limit: int = 200) -> dict:
    return {"rows": _service().list_events(batch_id, limit=limit)}


@router.get("/batches/{batch_id}/summaries", dependencies=[Depends(require_batch_management_license)])
def list_summaries(batch_id: str) -> dict:
    return {"rows": _service().list_summaries(batch_id)}


@router.post("/batches/{batch_id}/recompute-summaries", dependencies=[Depends(require_batch_management_license)])
def recompute_summaries(batch_id: str) -> dict:
    n = _service().compute_summaries(batch_id)
    return {"ok": True, "rows_written": n}


@router.get("/batches/{batch_id}/historian", dependencies=[Depends(require_batch_management_license)])
def historian_rows(batch_id: str, limit: int = 5000) -> dict:
    return {"rows": _service().historian_rows_for_batch(batch_id, limit=limit)}


@router.delete("/batches/{batch_id}", dependencies=[Depends(require_batch_management_license)])
def delete_batch(batch_id: str, request: Request) -> dict:
    ok = _service().delete_batch(batch_id, actor=_actor(request))
    if not ok:
        raise HTTPException(status_code=404, detail="batch not found")
    return {"ok": True}


# ---- parent / child rollup -------------------------------------------
@router.get("/batches/{batch_id}/rollup", dependencies=[Depends(require_batch_management_license)])
def batch_rollup(batch_id: str) -> dict:
    """Aggregate all child batches under this parent: per-tag stats
    (weighted-avg + global min/max), child list, status totals.
    Used by the UI parent-batch detail view + parent PDF export."""
    try:
        return {"ok": True, **_service().rollup_children(batch_id)}
    except KeyError:
        raise HTTPException(status_code=404, detail="parent batch not found")


@router.get("/batches/{batch_id}/rollup-report.pdf", dependencies=[Depends(require_batch_management_license)])
def batch_rollup_pdf(batch_id: str) -> Response:
    """Single PDF that covers a parent batch + every child: cover page
    with aggregated stats, then one section per child."""
    svc = _service()
    parent = svc.get_batch(batch_id)
    if not parent:
        raise HTTPException(status_code=404, detail="parent batch not found")
    rollup = svc.rollup_children(batch_id)
    children = rollup.get("children") or []
    # Build per-child sections by calling the existing per-batch renderer
    # for each, then concatenate. Keeps render code in one place.
    from .reports import render_single_batch_pdf, render_parent_rollup_pdf
    pdf = render_parent_rollup_pdf(parent, svc.get_batch_type(parent.get("batch_type_id") or ""), rollup, children, svc)
    filename = f"batch-rollup-{parent.get('identifier') or batch_id}.pdf".replace("/", "-")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ---- reports ---------------------------------------------------------
@router.get("/batches/{batch_id}/report.pdf", dependencies=[Depends(require_batch_management_license)])
def batch_report_pdf(batch_id: str) -> Response:
    svc = _service()
    batch = svc.get_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="batch not found")
    batch_type = svc.get_batch_type(batch["batch_type_id"]) if batch.get("batch_type_id") else None
    events = svc.list_events(batch_id, limit=200)
    summaries = svc.list_summaries(batch_id)
    pdf = render_single_batch_pdf(batch, batch_type, events, summaries)
    filename = f"batch-{batch.get('identifier') or batch_id}.pdf".replace("/", "-")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ---- audit -----------------------------------------------------------
@router.get("/audit", dependencies=[Depends(require_batch_management_license)])
def list_audit(limit: int = 200, batch_id: Optional[str] = None) -> dict:
    return {"rows": _service().list_audit(limit=limit, batch_id=batch_id)}
