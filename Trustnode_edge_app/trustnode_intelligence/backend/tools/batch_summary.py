"""Batch tools — read the Batch Management v2 module so the AI can answer
questions about batches: list/recent, per-batch summary + KPIs + limit
excursions + per-tag pass/fail, and the batch definitions catalog.

All access is READ-ONLY over the v2 services (BatchExecutionService /
BatchCalcService / BatchDefinitionService), the same ones the v2 REST router
uses. The legacy v1 BatchService is NOT used — current batches live in v2 and a
v1 lookup can't find them.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def _app_store():
    from app.state import app_store  # type: ignore
    return app_store


def _exe():
    from app.modules.batch_management.service_v2 import BatchExecutionService  # type: ignore
    return BatchExecutionService(_app_store())


def _calc():
    from app.modules.batch_management.calc_v2 import BatchCalcService  # type: ignore
    return BatchCalcService(_app_store())


def _defs():
    from app.modules.batch_management.service_v2 import BatchDefinitionService  # type: ignore
    return BatchDefinitionService(_app_store())


def _batch_enabled() -> bool:
    try:
        from app.modules.batch_management.license import is_batch_management_enabled  # type: ignore
        return bool(is_batch_management_enabled())
    except Exception:
        # If the license helper is unavailable, assume enabled — the module import
        # below will fail cleanly if it truly isn't installed.
        return True


def _slim_batch(b: Dict[str, Any]) -> Dict[str, Any]:
    """A compact batch shape for the LLM (drop verbose/internal fields)."""
    return {
        "id": b.get("id"),
        "reference": b.get("reference"),
        "status": b.get("status"),
        "quality": b.get("quality_status"),
        "data_quality": b.get("data_quality_status"),
        "equipment_id": b.get("equipment_id"),
        "product": b.get("product"),
        "started_utc": b.get("started_utc"),
        "ended_utc": b.get("ended_utc"),
        "definition_id": b.get("definition_id"),
        "batch_group_id": b.get("batch_group_id"),
    }


def _duration_seconds(b: Dict[str, Any]) -> Optional[float]:
    from datetime import datetime, timezone
    s = str(b.get("started_utc") or "").strip()
    e = str(b.get("ended_utc") or "").strip()
    if not s:
        return None
    def _parse(t):
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(t[:26], fmt).replace(tzinfo=timezone.utc)
            except Exception:
                continue
        return None
    ds = _parse(s)
    if not ds:
        return None
    de = _parse(e) if e else datetime.now(timezone.utc)
    if not de:
        return None
    return max(0.0, (de - ds).total_seconds())


def run_list_recent_batches(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    if not _batch_enabled():
        return {"count": 0, "batches": [], "note": "Batch Management module is not licensed."}
    limit = int(args.get("limit") or 20)
    status = str(args.get("state") or args.get("status") or "").strip().lower()
    try:
        rows, total = _exe().list_batches(limit=max(1, min(limit, 200)),
                                          status=status or None)
    except Exception as exc:
        return {"error": str(exc), "count": 0, "batches": []}
    out = []
    for b in rows:
        sb = _slim_batch(b)
        dur = _duration_seconds(b)
        if dur is not None:
            sb["duration_s"] = round(dur, 1)
        out.append(sb)
    return {"count": len(out), "total": int(total), "batches": out}


def run_get_batch_summary(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    if not _batch_enabled():
        return {"error": "Batch Management module is not licensed."}
    bid = str(args.get("batch_id") or "").strip()
    ref = str(args.get("reference") or "").strip()
    exe = _exe()
    b = None
    if bid:
        b = exe.get_batch(bid)
    if not b and ref:
        rows, _ = exe.list_batches(limit=200, search=ref)
        b = next((r for r in rows if str(r.get("reference") or "") == ref), (rows[0] if rows else None))
    if not b and not bid and not ref:
        # default: the most recent batch
        rows, _ = exe.list_batches(limit=1)
        b = rows[0] if rows else None
    if not b:
        return {"error": "Batch not found."}

    summary = _slim_batch(b)
    dur = _duration_seconds(b)
    if dur is not None:
        summary["duration_s"] = round(dur, 1)
    # KPIs
    try:
        kpis = _calc().list_kpis(b["id"]) or []
        summary["kpis"] = [
            {"code": k.get("kpi_code"), "label": k.get("label"),
             "value": k.get("numeric_value"), "unit": k.get("unit"),
             "quality": k.get("quality_status")}
            for k in kpis if k.get("numeric_value") is not None or k.get("text_value")
        ]
    except Exception:
        summary["kpis"] = []
    # limit excursions
    try:
        exc = _calc().list_excursions(batch_id=b["id"]) or []
        summary["excursion_count"] = len(exc)
        summary["failing_excursions"] = sum(1 for e in exc if str(e.get("severity") or "") in ("error", "critical"))
        summary["excursions"] = [
            {"tag": e.get("tag_name"), "limit_type": e.get("limit_type"),
             "limit_value": e.get("limit_value"), "severity": e.get("severity")}
            for e in exc[:20]
        ]
    except Exception:
        summary["excursions"] = []
    # per-tag min/max/avg + pass/fail
    try:
        mx = exe.tag_matrix(b["id"], max_rows=5000)
        cols = mx.get("tags") or []
        spec = set(mx.get("spec_tags") or [])
        tag_stats = []
        for c in cols:
            vals = [(r.get("values") or {}).get(c) for r in (mx.get("rows") or [])]
            vals = [v for v in vals if isinstance(v, (int, float))]
            if not vals:
                continue
            out_n = sum(1 for r in (mx.get("rows") or [])
                        if isinstance((r.get("values") or {}).get(c), (int, float)) and r.get("in_limits") is False)
            tag_stats.append({
                "tag": c, "min": min(vals), "max": max(vals),
                "avg": round(sum(vals) / len(vals), 4), "samples": len(vals),
                "result": ("pass" if out_n == 0 else f"fail ({out_n} out)") if c in spec else "n/a",
            })
        summary["tag_stats"] = tag_stats
        summary["sample_count"] = mx.get("total")
    except Exception:
        summary["tag_stats"] = []
    return summary


def run_list_batch_definitions(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    if not _batch_enabled():
        return {"count": 0, "definitions": [], "note": "Batch Management module is not licensed."}
    try:
        defs = _defs().list_definitions() or []
    except Exception as exc:
        return {"error": str(exc), "count": 0, "definitions": []}
    out = [{"id": d.get("id"), "name": d.get("name"), "code": d.get("code"),
            "equipment_id": d.get("equipment_id"), "status": d.get("status"),
            "product": d.get("product")} for d in defs]
    return {"count": len(out), "definitions": out}
