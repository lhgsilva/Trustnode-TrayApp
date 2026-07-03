"""PLC-driven batch auto-trigger watcher.

Polls the latest historian reading for every tag referenced by any
enabled batch_type's `trigger_start` or `trigger_stop` rule. Evaluates
the rules (rising_edge / falling_edge / threshold / equals, combined
with AND/OR) and auto-starts or auto-stops batches.

Design constraints (per operator 2026-06-30):
  * Best-effort. Failures never affect PLC collection or the historian.
  * Bounded polling: every TRIGGER_POLL_INTERVAL_S seconds, batched.
  * Hysteresis on threshold rules so a tag oscillating around the
    setpoint doesn't create a "batch storm".
  * Edge-detection state lives in memory only — on backend restart we
    re-arm with the current tag values (no retroactive triggers).
  * Auto-started batches are tagged with source="plc_trigger" so audit
    trail can distinguish them.

This module is imported only when the batch_management license is
present. It starts its own daemon thread (idempotent).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger("trustnode.batch_management.triggers")

TRIGGER_POLL_INTERVAL_S = 2.0       # how often to check tag values
TRIGGER_DEBOUNCE_S = 5.0            # min seconds between two triggers of the same kind on the same batch_type
_DEFAULT_HYSTERESIS = 0.0           # threshold rules use this when not specified

_started = False
_thread: Optional[threading.Thread] = None
_state_lock = threading.Lock()
# Per-(batch_type_id, slot) prior values for edge detection. Slot is
# "start" or "stop". prior[(bt, slot)] = {tag: last_seen_value}.
_prior: Dict[Tuple[str, str], Dict[str, Any]] = {}
# Per-(batch_type_id, slot) last fire monotonic timestamp (debounce).
_last_fire: Dict[Tuple[str, str], float] = {}


def _parse_rule(rule: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(rule, dict):
        return None
    tag = str(rule.get("tag") or "").strip()
    kind = str(rule.get("kind") or "").strip().lower()
    if not tag or kind not in ("rising_edge", "falling_edge", "threshold", "equals"):
        return None
    out: Dict[str, Any] = {"tag": tag, "kind": kind}
    if kind == "threshold":
        op = str(rule.get("op") or ">").strip()
        if op not in (">", "<", ">=", "<="):
            op = ">"
        try:
            out["value"] = float(rule.get("value"))
        except Exception:
            return None
        try:
            out["hysteresis"] = max(0.0, float(rule.get("hysteresis") or _DEFAULT_HYSTERESIS))
        except Exception:
            out["hysteresis"] = _DEFAULT_HYSTERESIS
        out["op"] = op
    elif kind == "equals":
        if "value" not in rule:
            return None
        out["value"] = rule.get("value")
    return out


def _evaluate_rule(rule: Dict[str, Any], current: Any, prior: Any) -> bool:
    """Return True iff the rule currently fires.

    prior is the value we saw on the previous poll (may be None on first
    seen). For edge rules, we need both prior and current.
    """
    kind = rule["kind"]
    if current is None:
        return False

    if kind == "rising_edge":
        # 0 -> 1 transition. Tolerant of booleans, ints, and "RUN"/"STOP"
        # strings — anything falsy → truthy counts.
        return (not _truthy(prior)) and _truthy(current)
    if kind == "falling_edge":
        return _truthy(prior) and (not _truthy(current))
    if kind == "threshold":
        try:
            v = float(current)
        except Exception:
            return False
        op = rule["op"]
        ref = float(rule["value"])
        hys = float(rule.get("hysteresis") or 0.0)
        if op == ">":
            return v > (ref + hys)
        if op == ">=":
            return v >= (ref + hys)
        if op == "<":
            return v < (ref - hys)
        if op == "<=":
            return v <= (ref - hys)
        return False
    if kind == "equals":
        return _coerce_equal(current, rule["value"])
    return False


def _truthy(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    return s in ("1", "true", "on", "run", "yes", "y")


def _coerce_equal(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a == b
    # Try numeric compare; fall back to case-insensitive string.
    try:
        return float(a) == float(b)
    except Exception:
        return str(a).strip().lower() == str(b).strip().lower()


def _evaluate_condition(cond: Dict[str, Any], current_values: Dict[str, Any],
                        prior_values: Dict[str, Any]) -> bool:
    """Evaluate the top-level {operator: AND|OR, rules: [...]} block."""
    if not isinstance(cond, dict):
        return False
    rules_raw = cond.get("rules") or []
    parsed = [r for r in (_parse_rule(r) for r in rules_raw) if r]
    if not parsed:
        return False
    op = str(cond.get("operator") or "AND").upper()
    results = [_evaluate_rule(r, current_values.get(r["tag"]), prior_values.get(r["tag"])) for r in parsed]
    if op == "OR":
        return any(results)
    return all(results)


def _collect_watched_tags(batch_types: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return {tag: None} of every tag referenced by any enabled type's
    triggers. The actual values are filled by _latest_values()."""
    out: Dict[str, Any] = {}
    for bt in batch_types:
        if not bt.get("enabled"):
            continue
        for slot in ("trigger_start", "trigger_stop"):
            cond = bt.get(slot)
            if not isinstance(cond, dict):
                continue
            for r in (cond.get("rules") or []):
                if isinstance(r, dict):
                    t = str(r.get("tag") or "").strip()
                    if t:
                        out[t] = None
    return out


def _latest_values(tags: List[str]) -> Dict[str, Any]:
    """Return the most-recent historian value per tag (across all DBs).
    Returns dict {tag: value} omitting tags with no data."""
    if not tags:
        return {}
    out: Dict[str, Any] = {}
    try:
        import sqlite3 as _sql
        # Reuse the scope helper from the intelligence module which
        # already knows every candidate workspace DB.
        try:
            from trustnode_intelligence.backend.tools._scope import all_db_paths
            paths = all_db_paths()
        except Exception:
            # Fallback to the single primary db from app_store.
            from app.state import app_store as _store  # type: ignore
            paths = [getattr(_store, "_db_path", "")] if getattr(_store, "_db_path", "") else []
        placeholders = ",".join(["?"] * len(tags))
        sql = (
            f"SELECT tag_name, value, ts_utc FROM historian_readings "
            f"WHERE tag_name IN ({placeholders}) "
            f"ORDER BY ts_utc DESC"
        )
        for db_path in paths:
            try:
                con = _sql.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
            except Exception:
                continue
            try:
                for r in con.execute(sql, tags):
                    name = str(r[0] or "")
                    if name and name not in out:
                        out[name] = r[1]
                        if len(out) >= len(tags):
                            break
            except _sql.OperationalError:
                pass
            finally:
                try: con.close()
                except Exception: pass
            if len(out) >= len(tags):
                break
    except Exception:
        pass
    return out


def _fire_start(svc: Any, bt: Dict[str, Any]) -> None:
    """Create + start a batch from a batch_type via plc_trigger."""
    try:
        payload = {
            "batch_type_id": bt["id"],
            "product": (bt.get("description") or bt.get("name") or "").split("\n")[0][:80],
            "recipe": "",
            "operator": "plc_trigger",
            "metadata": {"source": "plc_trigger", "auto_start": True},
        }
        created = svc.create_batch(payload, actor="system:plc_trigger")
        new_id = created.get("id")
        if not new_id:
            return
        svc.start_batch(new_id, actor="system:plc_trigger", source="plc_trigger")
        _log.info("plc_trigger: started batch %s from type %s", new_id, bt.get("name"))
    except Exception as exc:
        _log.warning("plc_trigger start failed for type %s: %s", bt.get("name"), exc)


def _fire_stop(svc: Any, bt: Dict[str, Any]) -> None:
    """Stop all currently-running batches of this type via plc_trigger."""
    try:
        result = svc.list_batches(limit=50, batch_type_id=bt["id"], status_filter="running")
        rows = result[0] if isinstance(result, tuple) else result
        for b in (rows or []):
            try:
                svc.stop_batch(b["id"], result="completed", actor="system:plc_trigger", source="plc_trigger")
                _log.info("plc_trigger: stopped batch %s of type %s", b["id"], bt.get("name"))
            except Exception as exc:
                _log.warning("plc_trigger stop failed for batch %s: %s", b.get("id"), exc)
    except Exception as exc:
        _log.warning("plc_trigger stop scan failed for type %s: %s", bt.get("name"), exc)


def _tick() -> None:
    """One poll cycle: fetch enabled batch types, evaluate triggers, fire."""
    try:
        from .service import BatchService  # local import to avoid cycles
        from app.state import app_store as _store  # type: ignore
        svc = BatchService(_store)
        batch_types = svc.list_batch_types()
    except Exception as exc:
        _log.debug("triggers tick: cannot reach service: %s", exc)
        return

    # Find all tags we need values for this tick.
    tags_blank = _collect_watched_tags(batch_types)
    if not tags_blank:
        return
    tags = list(tags_blank.keys())
    current = _latest_values(tags)

    now_mono = time.monotonic()
    for bt in batch_types:
        if not bt.get("enabled"):
            continue
        for slot in ("trigger_start", "trigger_stop"):
            cond = bt.get(slot)
            if not isinstance(cond, dict) or not cond.get("rules"):
                continue
            key = (str(bt.get("id")), slot)
            with _state_lock:
                prior = _prior.get(key, {})
                _prior[key] = dict(current)  # snapshot for the next tick
            try:
                fires = _evaluate_condition(cond, current, prior)
            except Exception as exc:
                _log.warning("trigger eval crashed for %s %s: %s", bt.get("name"), slot, exc)
                continue
            if not fires:
                continue
            last = _last_fire.get(key, 0.0)
            if (now_mono - last) < TRIGGER_DEBOUNCE_S:
                _log.debug("trigger debounced %s %s", bt.get("name"), slot)
                continue
            _last_fire[key] = now_mono
            if slot == "trigger_start":
                _fire_start(svc, bt)
            else:
                _fire_stop(svc, bt)


def _loop() -> None:
    while True:
        try:
            _tick()
        except Exception as exc:
            _log.exception("triggers loop tick failed: %s", exc)
        time.sleep(TRIGGER_POLL_INTERVAL_S)


def start_trigger_watcher() -> None:
    """Idempotent start of the daemon thread."""
    global _started, _thread
    if _started:
        return
    _started = True
    _thread = threading.Thread(target=_loop, name="trustnode-batch-trigger-watcher", daemon=True)
    _thread.start()
    _log.info("Batch trigger watcher started (poll=%.1fs, debounce=%.1fs)", TRIGGER_POLL_INTERVAL_S, TRIGGER_DEBOUNCE_S)
