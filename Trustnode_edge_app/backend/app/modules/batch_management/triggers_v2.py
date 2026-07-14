"""Batch Management v2 trigger daemon (clean rebuild).

ONE daemon for the whole module. Polls the latest historian reading for every tag
referenced by any PUBLISHED definition's enabled trigger references, evaluates the
conditions (REUSING the proven evaluation helpers from triggers.py), and drives the
v2 execution service: auto-start / auto-stop / hold / resume / abort, plus creating
batch groups and their child batches.

Guardrail:
  * Best-effort. A failure here NEVER affects PLC collection or the historian.
  * The legacy triggers.py daemon is NOT started (see __init__.py) so two loops
    never both create batches.
  * Reuses triggers.py's _evaluate_condition / _parse_rule / _latest_values — the
    condition shape ({operator, rules:[{tag,kind,value,op,hysteresis}]}) is identical.
  * Edge state lives in memory; on restart we re-arm with current values (no
    retroactive fires). Debounced per (definition_version, scope).

Guide: docs/BATCH_MANAGEMENT_REDESIGN_2026-07-14.md
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# REUSE the evaluation logic from the legacy module (logic only — we do NOT call
# its _fire_start/_fire_stop, which drive the old tables).
from .triggers import _evaluate_condition, _latest_values, TRIGGER_POLL_INTERVAL_S

_log = logging.getLogger("trustnode.batch_management.triggers_v2")

DEBOUNCE_S = 5.0

_started = False
_thread: Optional[threading.Thread] = None
_lock = threading.Lock()
# prior values per (definition_version_id, trigger_scope) for edge detection.
_prior: Dict[Tuple[str, str], Dict[str, Any]] = {}
_last_fire: Dict[Tuple[str, str], float] = {}


def _services():
    from app.state import app_store as store
    from .service_v2 import BatchDefinitionService, BatchExecutionService, BatchGroupService
    return (BatchDefinitionService(store), BatchExecutionService(store), BatchGroupService(store))


def _published_trigger_refs(defsvc) -> List[Dict[str, Any]]:
    """Return active trigger references for every PUBLISHED definition, each
    enriched with its definition + version ids so a fire knows what to create."""
    out: List[Dict[str, Any]] = []
    try:
        defs = defsvc.list_definitions()
    except Exception:
        return out
    for d in defs:
        if str(d.get("status")) != "published":
            continue
        cfg = None
        try:
            full = defsvc.get_definition(d["id"], version_id=d.get("current_version_id"))
            cfg = (full or {}).get("config")
        except Exception:
            cfg = None
        if not cfg:
            continue
        for tr in (cfg.get("triggers") or []):
            if tr.get("enabled") is False:
                continue
            cond = tr.get("condition")
            if not isinstance(cond, dict) or not (cond.get("rules")):
                continue
            out.append({
                "definition_id": d["id"],
                "definition_version_id": d.get("current_version_id"),
                "equipment_id": d.get("equipment_id"),
                "batch_mode": cfg.get("batch_mode") or "individual",
                "trigger_scope": tr.get("trigger_scope"),
                "gateway_id": tr.get("gateway_id"),
                "condition": cond,
            })
    return out


def _collect_tags(refs: List[Dict[str, Any]]) -> List[str]:
    tags: set[str] = set()
    for r in refs:
        for rule in (r["condition"].get("rules") or []):
            t = str((rule or {}).get("tag") or "").strip()
            if t:
                tags.add(t)
    return sorted(tags)


def _debounced(key: Tuple[str, str]) -> bool:
    now = time.monotonic()
    last = _last_fire.get(key, 0.0)
    if now - last < DEBOUNCE_S:
        return True
    _last_fire[key] = now
    return False


def _tick() -> None:
    defsvc, exe, grp = _services()
    refs = _published_trigger_refs(defsvc)
    if not refs:
        return
    tags = _collect_tags(refs)
    current = _latest_values(tags) if tags else {}

    for ref in refs:
        scope = str(ref.get("trigger_scope") or "")
        vkey = (str(ref.get("definition_version_id") or ""), scope)
        prior = _prior.get(vkey, {})
        try:
            fired = _evaluate_condition(ref["condition"], current, prior)
        except Exception:
            fired = False
        # update prior for the tags in this condition
        newprior = dict(prior)
        for rule in (ref["condition"].get("rules") or []):
            t = str((rule or {}).get("tag") or "").strip()
            if t:
                newprior[t] = current.get(t)
        _prior[vkey] = newprior

        if not fired or _debounced(vkey):
            continue
        try:
            _dispatch(exe, grp, ref)
        except Exception as e:  # pragma: no cover - defensive
            _log.warning("v2 trigger dispatch failed: %s", e)


def _dispatch(exe, grp, ref: Dict[str, Any]) -> None:
    """Route a fired trigger to the right execution action."""
    scope = str(ref.get("trigger_scope") or "")
    defn_id = ref.get("definition_id")
    ver_id = ref.get("definition_version_id")
    eq = ref.get("equipment_id")
    src = "gateway"

    if scope == "BATCH_START":
        # idempotency key so repeated fires within the same running window don't dup
        idem = f"{ver_id}:BATCH_START:{eq or ''}"
        # only create if there isn't already a running/held batch for this definition+equipment
        running, _ = exe.list_batches(status="running", definition_id=defn_id, limit=1)
        held, _ = exe.list_batches(status="held", definition_id=defn_id, limit=1)
        if running or held:
            return
        b = exe.create_batch(
            {"definition_id": defn_id, "definition_version_id": ver_id,
             "equipment_id": eq, "trigger_mode": "gateway_trigger"},
            source=src, idempotency_key=None)  # each run is a fresh batch
        exe.start_batch(b["id"], source=src, reason="gateway trigger", equipment_id=eq)

    elif scope == "BATCH_STOP":
        for st in ("running", "held"):
            rows, _ = exe.list_batches(status=st, definition_id=defn_id, limit=50)
            for b in rows:
                exe.stop_batch(b["id"], source=src, reason="gateway trigger")

    elif scope == "HOLD":
        rows, _ = exe.list_batches(status="running", definition_id=defn_id, limit=50)
        for b in rows:
            exe.hold_batch(b["id"], source=src, reason="gateway trigger")

    elif scope == "RESUME":
        rows, _ = exe.list_batches(status="held", definition_id=defn_id, limit=50)
        for b in rows:
            exe.resume_batch(b["id"], source=src)

    elif scope == "ABORT":
        for st in ("running", "held"):
            rows, _ = exe.list_batches(status=st, definition_id=defn_id, limit=50)
            for b in rows:
                exe.abort_batch(b["id"], source=src, reason="gateway trigger")

    elif scope == "BATCH_GROUP_START":
        g = grp.create_group({"definition_id": defn_id, "equipment_id": eq}, actor=None)
        # optionally open the first child immediately
        b = exe.create_batch(
            {"definition_id": defn_id, "definition_version_id": ver_id,
             "equipment_id": eq, "batch_group_id": g["id"], "trigger_mode": "gateway_trigger"},
            source=src)
        exe.start_batch(b["id"], source=src, reason="gateway trigger", equipment_id=eq)

    elif scope == "BATCH_GROUP_STOP":
        active, _ = grp.list_groups(status="active", limit=50)
        for g in active:
            # stop any running children then complete the group
            for st in ("running", "held"):
                rows, _ = exe.list_batches(status=st, batch_group_id=g["id"], limit=50)
                for b in rows:
                    exe.stop_batch(b["id"], source=src, reason="group stop")
            try:
                grp.complete_group(g["id"], actor=None)
            except Exception:
                pass


def _loop() -> None:
    # restart recovery once at boot
    try:
        _defsvc, exe, _grp = _services()
        n = exe.recover_active_batches()
        if n:
            _log.info("v2 batch restart recovery: %d active batch(es) marked incomplete", n)
    except Exception:
        pass
    while True:
        try:
            _tick()
        except Exception as e:  # pragma: no cover
            _log.debug("v2 trigger tick error: %s", e)
        time.sleep(TRIGGER_POLL_INTERVAL_S)


def start_trigger_watcher_v2() -> None:
    """Idempotent. Starts the single v2 daemon thread."""
    global _started, _thread
    with _lock:
        if _started:
            return
        _started = True
        _thread = threading.Thread(target=_loop, name="tn-batch-v2-triggers", daemon=True)
        _thread.start()
        _log.info("batch v2 trigger watcher started")
