# -*- coding: utf-8 -*-
"""OEE REST surface, mounted at /api/oee.

Permission model follows the rest of the app: reading needs the `oee` key,
writing needs `oee_configuration` (or admin). Nothing here is licence-gated in
v1 - the module is local-first and must work on every deployment, including
Lite - but the module key exists in MODULE_CATALOG so it CAN be gated later
without touching this file.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

_LOG = logging.getLogger("trustnode.oee")
router = APIRouter(prefix="/api/oee", tags=["oee"])

_SERVICE: Any = None


def _service():
    global _SERVICE
    if _SERVICE is None:
        from app.state import app_store
        from .service import OeeService
        _SERVICE = OeeService(app_store)
    return _SERVICE


# --------------------------------------------------------------- permissions
def _perms(request: Request) -> Dict[str, Any]:
    """The decoded token payload.

    The auth middleware puts it on `request.state.user_payload` (main.py) —
    NOT `claims`, which is what every other router in this app reads. It
    carries `sub`, `role`, `permissions` and `tenant_id`.
    """
    payload = getattr(request.state, "user_payload", None) or {}
    return payload if isinstance(payload, dict) else {}


def _role(request: Request) -> str:
    return str(_perms(request).get("role") or "").strip().lower()


def _can_write(request: Request) -> bool:
    if _role(request) in ("admin", "super"):
        return True
    try:
        from app.services import permission_catalog as pc
        perms = _perms(request).get("permissions") or {}
        return bool(pc.resolve(perms, "oee_configuration"))
    except Exception:
        return False


def _require_write(request: Request) -> None:
    if not _can_write(request):
        raise HTTPException(status_code=403,
                            detail="You do not have permission to change OEE configuration.")


def _actor(request: Request) -> str:
    return str(_perms(request).get("sub") or _perms(request).get("username") or "")


# -------------------------------------------------------------------- models
class EntityPayload(BaseModel):
    """Free-form config row; the store keeps only columns that exist."""
    model_config = {"extra": "allow"}


class CountPayload(BaseModel):
    machine_id: str
    total_count: float = 0.0
    good_count: Optional[float] = None
    reject_count: Optional[float] = None
    source: str = "manual"
    order_id: str = ""
    product_id: str = ""
    cycle_id: str = ""


class QualityPayload(BaseModel):
    machine_id: str
    quantity: float = 0.0
    result: str = "reject"
    quality_reason_id: str = ""
    comment: str = ""
    order_id: str = ""
    product_id: str = ""
    cycle_id: str = ""


class CyclePayload(BaseModel):
    machine_id: str
    product_id: str = ""
    order_id: str = ""
    result: str = "unknown"
    source: str = "manual"


class DowntimePayload(BaseModel):
    """A partial edit of one downtime event.

    Every field defaults to None, not "". The Machine Detail page offers
    "add a comment" and "mark planned" as separate actions, and neither may
    wipe the reason somebody walked to the machine to establish.
    """
    event_id: str
    downtime_reason_id: Optional[str] = None
    downtime_category: Optional[str] = None
    comment: Optional[str] = None
    is_planned: Optional[bool] = None


class StatePayload(BaseModel):
    machine_id: str
    state: str
    comment: str = ""


# ------------------------------------------------------------------- windows
def _window(from_utc: str, to_utc: str, hours: float = 24.0) -> tuple:
    """Default to the last N hours when the caller gives no range."""
    if from_utc and to_utc:
        return from_utc, to_utc
    now = _dt.datetime.now(_dt.timezone.utc)
    start = now - _dt.timedelta(hours=float(hours or 24.0))
    fmt = "%Y-%m-%d %H:%M:%S.%f"
    return (from_utc or start.strftime(fmt)[:-3],
            to_utc or now.strftime(fmt)[:-3])


# ------------------------------------------------------------------ metadata
@router.get("/meta")
def get_meta() -> dict:
    """Enumerations the UI builds its dropdowns from.

    Served from the backend so the frontend cannot drift out of step with what
    the state engine actually understands.
    """
    from .state_engine import (MACHINE_STATES, OEE_FUNCTIONS, CONDITION_OPS,
                               CONF_HIGH, CONF_MEDIUM, CONF_LOW, CONF_CONFLICT,
                               CONF_MISSING)
    return {
        "ok": True,
        "states": list(MACHINE_STATES),
        "oee_functions": list(OEE_FUNCTIONS),
        "condition_ops": list(CONDITION_OPS),
        "status_sources": ["signal", "power", "manual", "combined"],
        "confidences": [CONF_HIGH, CONF_MEDIUM, CONF_LOW, CONF_CONFLICT, CONF_MISSING],
        "source_types": ["plc", "sensor", "manual", "energy_meter"],
        "power_statuses": ["off", "stopped", "idle", "running", "production",
                           "high_consumption", "energy_waste", "unknown"],
        "measurements": ["power_kw", "current_a"],
        "repeat_rules": ["none", "daily", "weekdays", "weekly"],
        "order_statuses": ["planned", "running", "paused", "completed", "cancelled"],
    }


# ------------------------------------------------------------------- config
_KINDS = ("machines", "signal_mappings", "power_meter_mappings",
          "power_state_rules", "products", "orders", "shifts",
          "planned_stops", "downtime_reasons", "quality_reasons",
          # 2026-08-29: the planning calendar. This tuple is a SECOND list of
          # sections beside store._CRUD_TABLES; a kind missing here answers 404
          # while the table sits there working, so the two must move together.
          "planned_events")


@router.get("/config/{kind}")
def list_config(kind: str, machine_id: str = "") -> dict:
    if kind not in _KINDS:
        raise HTTPException(status_code=404, detail=f"Unknown OEE section '{kind}'.")
    rows = _service().store.list_entities(kind, machine_id=machine_id)
    return {"ok": True, "kind": kind, "items": rows, "count": len(rows)}


@router.post("/config/{kind}")
def save_config(kind: str, payload: EntityPayload, request: Request) -> dict:
    if kind not in _KINDS:
        raise HTTPException(status_code=404, detail=f"Unknown OEE section '{kind}'.")
    _require_write(request)
    data = payload.model_dump()
    svc = _service()

    # Validate the SOFT references into the collection system. A mapping that
    # points at a tag nobody collects is the single most common way an OEE
    # machine ends up permanently "unknown", so it is refused at save time
    # rather than discovered on the dashboard a week later.
    problem = _validate_references(kind, data, request)
    if problem:
        raise HTTPException(status_code=400, detail=problem)

    row = svc.store.save_entity(kind, data, actor=_actor(request))

    # A new machine with power monitoring on gets starter rules, because a
    # power source with no rules can never produce a state.
    if kind == "machines" and row.get("power_enabled"):
        try:
            from .seed import seed_power_rules_for_machine
            seed_power_rules_for_machine(svc.store, str(row["id"]))
        except Exception:
            pass
    return {"ok": True, "item": row}


@router.delete("/config/{kind}/{entity_id}")
def delete_config(kind: str, entity_id: str, request: Request) -> dict:
    if kind not in _KINDS:
        raise HTTPException(status_code=404, detail=f"Unknown OEE section '{kind}'.")
    _require_write(request)
    ok = _service().store.delete_entity(kind, entity_id)
    return {"ok": ok, "deleted": entity_id}


def _gateways_for(request: Request) -> List[Dict[str, Any]]:
    """The gateways THIS caller can see.

    `gateway_configurations` is a SHARED-EDGE domain, so the real gateways live
    in a per-edge scoped document; the unscoped bootstrap holds only the legacy
    seed. Reading the wrong one is the bug that made /api/plc/gateways/status
    report a collecting gateway as stopped (see the comment there), and here it
    would reject every valid tag mapping with "gateway does not exist".

    Scoped first, unscoped as the fallback.
    """
    from app.state import app_store
    rows: List[Dict[str, Any]] = []
    try:
        from app.routers.app_store import _build_scope_key  # type: ignore
        scope_key = _build_scope_key(request, domain="gateway_configurations")
        if scope_key:
            scoped = app_store.get_bootstrap_scoped(scope_key, prefer_cloud_reads=False) or {}
            cand = scoped.get("gateway_configurations")
            if isinstance(cand, list):
                rows = cand
    except Exception:
        rows = []
    if not rows:
        try:
            bootstrap = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
            cand = bootstrap.get("gateway_configurations")
            rows = cand if isinstance(cand, list) else []
        except Exception:
            rows = []
    return rows


def _validate_references(kind: str, data: Dict[str, Any],
                         request: Request) -> str:
    """Check gateway/device/tag soft references resolve. Returns '' when fine."""
    if kind not in ("signal_mappings", "power_meter_mappings"):
        return ""
    gw_id = str(data.get("gateway_id") or "").strip()
    if not gw_id:
        return ""
    gateways = _gateways_for(request)
    if not gateways:
        return ""     # nothing to validate against - do not block the save
    gw = next((g for g in gateways if str(g.get("id")) == gw_id), None)
    if gw is None:
        return (f"Gateway '{gw_id}' does not exist. Pick one from the list — OEE "
                f"references the gateways you already collect with.")
    tags = {str(t) for t in (gw.get("tags") or [])}
    for field in ("tag_name", "power_tag", "energy_tag", "current_tag",
                  "voltage_tag", "power_factor_tag"):
        tag = str(data.get(field) or "").strip()
        if tag and tags and tag not in tags:
            return (f"Tag '{tag}' is not collected by gateway "
                    f"'{gw.get('name') or gw_id}'. Add it to the gateway first, "
                    f"or choose a tag it already reads.")
    return ""


# ----------------------------------------------------------------- overview
@router.get("/overview")
def overview(from_utc: str = "", to_utc: str = "", hours: float = 24.0,
             machine_ids: str = "", line: str = "", compare: int = 0) -> dict:
    """The whole Overview page in one call.

    `compare=1` adds the same totals for the equal-length window immediately
    before this one, which is what the KPI cards' "vs previous" is measured
    against. Off by default: it doubles the work, and not every caller wants it.
    """
    a, b = _window(from_utc, to_utc, hours)
    ids = [x for x in str(machine_ids or "").split(",") if x.strip()]
    return _service().overview(a, b, machine_ids=ids or None, line=line,
                               with_previous=bool(int(compare or 0)))


@router.get("/trend")
def trend(from_utc: str = "", to_utc: str = "", hours: float = 24.0,
          machine_ids: str = "", buckets: int = 24) -> dict:
    a, b = _window(from_utc, to_utc, hours)
    ids = [x for x in str(machine_ids or "").split(",") if x.strip()]
    return {"ok": True, "buckets": _service().trend(ids, a, b, buckets)}


# ==================== dashboard aggregates (2026-08-29) ====================
# One endpoint per dashboard surface. Pages and widgets read these; none of
# them re-implements an OEE formula, because two implementations of the same
# formula will disagree and the one on screen is the one nobody can trace.


@router.get("/dashboard/timeline")
def dashboard_timeline(from_utc: str = "", to_utc: str = "", hours: float = 24.0,
                       machine_ids: str = "", max_blocks: int = 2000) -> dict:
    a, b = _window(from_utc, to_utc, hours)
    ids = [x for x in str(machine_ids or "").split(",") if x.strip()]
    return _service().status_timeline(ids or None, a, b,
                                      max_blocks=max(50, min(int(max_blocks or 2000), 20000)))


@router.get("/dashboard/downtime-pareto")
def dashboard_downtime_pareto(from_utc: str = "", to_utc: str = "", hours: float = 24.0,
                              machine_ids: str = "", limit: int = 12,
                              group_by: str = "reason", metric: str = "duration") -> dict:
    """Downtime ranked by duration or by number of stops.

    `group_by` is one of the service's PARETO_GROUPS - reason, category,
    machine, line. The response repeats which groupings are supported, so the
    page offers exactly those: an event carries no product or order, and a
    grouping the data cannot support should not appear in a menu.
    """
    a, b = _window(from_utc, to_utc, hours)
    ids = [x for x in str(machine_ids or "").split(",") if x.strip()]
    return _service().downtime_pareto(ids or None, a, b,
                                      limit=max(1, min(int(limit or 12), 100)),
                                      group_by=group_by, metric=metric)


@router.get("/dashboard/energy")
def dashboard_energy(from_utc: str = "", to_utc: str = "", hours: float = 24.0,
                     machine_ids: str = "") -> dict:
    a, b = _window(from_utc, to_utc, hours)
    ids = [x for x in str(machine_ids or "").split(",") if x.strip()]
    return _service().energy_summary(ids or None, a, b)


@router.get("/dashboard/shifts")
def dashboard_shifts(from_utc: str = "", to_utc: str = "", hours: float = 24.0,
                     machine_ids: str = "") -> dict:
    a, b = _window(from_utc, to_utc, hours)
    ids = [x for x in str(machine_ids or "").split(",") if x.strip()]
    return _service().shift_performance(ids or None, a, b)


@router.get("/planning")
def planning_events(from_utc: str = "", to_utc: str = "", hours: float = 168.0,
                    machine_ids: str = "", line: str = "") -> dict:
    """Planning-calendar events overlapping the window.

    Default window is a week, because the calendar is read by week far more
    often than by day.
    """
    a, b = _window(from_utc, to_utc, hours)
    ids = [x for x in str(machine_ids or "").split(",") if x.strip()]
    return _service().planned_events(a, b, machine_ids=ids or None, line=line)


@router.get("/machines/live")
def machines_live() -> dict:
    """Machine cards only - cheap enough for the Operator screen to poll."""
    svc = _service()
    live = svc._live_values()
    cards = [svc.machine_snapshot(m, live, record=True)
             for m in svc._machines()]
    return {"ok": True, "machines": cards}


@router.get("/machines/{machine_id}/result")
def machine_result(machine_id: str, from_utc: str = "", to_utc: str = "",
                   hours: float = 24.0, compare: int = 0) -> dict:
    svc = _service()
    machine = svc.store.get_entity("machines", machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found.")
    a, b = _window(from_utc, to_utc, hours)
    out = {"ok": True, "result": svc.machine_result(machine, a, b)}
    if compare:
        # The equal-length window immediately before, through the SAME code
        # path - so "vs previous shift" on a KPI card compares like with like
        # instead of against a number the page worked out for itself.
        pa, pb = svc._previous_window(a, b)
        out["previous"] = {"from_utc": pa, "to_utc": pb,
                           "result": svc.machine_result(machine, pa, pb)}
    return out


@router.get("/machines/{machine_id}/events")
def machine_events(machine_id: str, from_utc: str = "", to_utc: str = "",
                   hours: float = 24.0, limit: int = 500) -> dict:
    a, b = _window(from_utc, to_utc, hours)
    return {"ok": True,
            "events": _service().store.list_events(machine_id, a, b, limit)}


# --------------------------------------------------------------- operator
@router.post("/operator/cycle/start")
def cycle_start(payload: CyclePayload, request: Request) -> dict:
    svc = _service()
    machine = svc.store.get_entity("machines", payload.machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found.")
    if not machine.get("manual_enabled") and payload.source == "manual":
        raise HTTPException(status_code=400,
                            detail="Manual input is switched off for this machine.")
    cyc = svc.store.start_cycle(payload.machine_id, payload.product_id,
                                payload.order_id, payload.source, _actor(request))
    return {"ok": True, "cycle": cyc}


@router.post("/operator/cycle/stop")
def cycle_stop(payload: CyclePayload, request: Request) -> dict:
    cyc = _service().store.stop_cycle(payload.machine_id, payload.result,
                                      _actor(request))
    if not cyc:
        raise HTTPException(status_code=404, detail="No open cycle on this machine.")
    return {"ok": True, "cycle": cyc}


@router.post("/operator/count")
def add_count(payload: CountPayload, request: Request) -> dict:
    svc = _service()
    machine = svc.store.get_entity("machines", payload.machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found.")
    if payload.source == "manual" and not machine.get("manual_enabled"):
        raise HTTPException(status_code=400,
                            detail="Manual input is switched off for this machine.")
    if payload.total_count < 0 or (payload.good_count or 0) < 0 or (payload.reject_count or 0) < 0:
        raise HTTPException(status_code=400, detail="Counts cannot be negative.")
    rec = svc.store.add_count(
        payload.machine_id, payload.total_count, payload.good_count,
        payload.reject_count, payload.source, payload.order_id,
        payload.product_id, payload.cycle_id, _actor(request))
    return {"ok": True, "record": rec}


@router.post("/operator/quality")
def add_quality(payload: QualityPayload, request: Request) -> dict:
    rec = _service().store.add_quality_result(
        payload.machine_id, payload.quantity, payload.result,
        payload.quality_reason_id, _actor(request), payload.comment,
        payload.order_id, payload.product_id, payload.cycle_id)
    return {"ok": True, "record": rec}


@router.post("/operator/downtime")
def confirm_downtime(payload: DowntimePayload, request: Request) -> dict:
    """Attach a reason to a stop. No reason given stays Unknown, by design."""
    ev = _service().store.confirm_downtime(
        payload.event_id, payload.downtime_reason_id,
        payload.downtime_category, payload.comment, _actor(request),
        is_planned=payload.is_planned)
    if not ev:
        raise HTTPException(status_code=404, detail="Downtime event not found.")
    return {"ok": True, "event": ev}


@router.post("/operator/state")
def set_state(payload: StatePayload, request: Request) -> dict:
    """Operator sets the machine state by hand (manual/combined machines)."""
    svc = _service()
    machine = svc.store.get_entity("machines", payload.machine_id)
    if not machine:
        raise HTTPException(status_code=404, detail="Machine not found.")
    if not machine.get("manual_enabled"):
        raise HTTPException(status_code=400,
                            detail="Manual input is switched off for this machine.")
    from .state_engine import MACHINE_STATES
    if payload.state not in MACHINE_STATES:
        raise HTTPException(status_code=400, detail=f"Unknown state '{payload.state}'.")
    ev = svc.store.open_event(payload.machine_id, payload.state, "manual",
                              "medium", payload.comment or "set by operator")
    return {"ok": True, "event": ev}


# ---------------------------------------------------------------- diagnostics
@router.get("/health")
def health() -> dict:
    """Row counts per table — used by the release gate and by support."""
    try:
        return {"ok": True, "tables": _service().store.counts()}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
