"""Batch tools — wrap the batch_management module's read endpoints when present."""
from __future__ import annotations

from typing import Any, Dict


def _import_batch_service():
    try:
        from app.modules.batch_management import service  # type: ignore
        return service
    except Exception:
        return None


def run_get_batch_summary(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    bid = str(args.get("batch_id") or "").strip()
    if not bid:
        return {"error": "Missing 'batch_id'."}
    svc = _import_batch_service()
    if not svc:
        return {"error": "Batch Management module not installed."}
    try:
        summary = svc.get_batch_summary(bid)  # type: ignore[attr-defined]
        return summary or {"error": "Batch not found."}
    except AttributeError:
        # Fallback: probe by name from list_batches
        try:
            batches = svc.list_batches(limit=500) or []  # type: ignore[attr-defined]
            for b in batches:
                if str(b.get("id")) == bid:
                    return b
            return {"error": "Batch not found."}
        except Exception as exc:
            return {"error": f"Could not load batch: {exc}"}
    except Exception as exc:
        return {"error": str(exc)}


def run_list_recent_batches(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    limit = int(args.get("limit") or 20)
    state = str(args.get("state") or "all").lower()
    svc = _import_batch_service()
    if not svc:
        return {"count": 0, "batches": [], "note": "Batch Management module not installed."}
    try:
        rows = svc.list_batches(limit=limit) or []  # type: ignore[attr-defined]
    except Exception as exc:
        return {"error": str(exc), "count": 0, "batches": []}
    if state != "all":
        rows = [r for r in rows if str(r.get("state") or "").lower() == state]
    return {"count": len(rows), "batches": rows[:limit]}
