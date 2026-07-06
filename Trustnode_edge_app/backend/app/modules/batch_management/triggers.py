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


# --------------------------------------------------------------------------
# Time-based SCHEDULES (operator 2026-07-06)
# Each batch_type may carry start_schedule / stop_schedule / report_schedule,
# a simple preset dict the operator picks in the UI:
#   {enabled: bool, freq: "daily"|"weekly"|"hourly"|"every_minutes",
#    time: "HH:MM" (local clock, for daily/weekly),
#    weekday: 0-6 (Mon=0, for weekly), every_minutes: int}
# We evaluate against LOCAL wall-clock (the edge runs on the operator's machine,
# so "daily at 06:00" means their 06:00). Dedupe is per due-minute using the
# batch_type's last_scheduled_*_utc cursor so a schedule fires at most once per
# occurrence even across restarts or multiple ticks in the same minute.
# --------------------------------------------------------------------------

def _due_now(sched: Any, now_local: datetime) -> bool:
    """True if this schedule should fire at the current local minute."""
    if not isinstance(sched, dict) or not sched.get("enabled"):
        return False
    freq = str(sched.get("freq") or "").strip().lower()
    if freq == "every_minutes":
        try:
            n = max(1, int(sched.get("every_minutes") or 0))
        except Exception:
            return False
        # Fire when minutes-since-midnight is a multiple of n (at :00 seconds).
        mins = now_local.hour * 60 + now_local.minute
        return (mins % n) == 0
    if freq == "hourly":
        try:
            mm = int(sched.get("minute", 0))
        except Exception:
            mm = 0
        return now_local.minute == mm
    # daily / weekly need a HH:MM
    tstr = str(sched.get("time") or "").strip()
    try:
        hh, mm = tstr.split(":")
        hh, mm = int(hh), int(mm)
    except Exception:
        return False
    if now_local.hour != hh or now_local.minute != mm:
        return False
    if freq == "weekly":
        try:
            wd = int(sched.get("weekday", 0))
        except Exception:
            wd = 0
        return now_local.weekday() == wd
    if freq == "daily":
        return True
    return False


def _already_fired_this_minute(last_utc: Any, now_utc: datetime) -> bool:
    """True if the cursor is within the same wall-clock minute as now (UTC)."""
    if not last_utc:
        return False
    try:
        s = str(last_utc)[:16]  # 'YYYY-MM-DD HH:MM'
        return s == now_utc.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return False


def _tick_schedules() -> None:
    """One schedule-evaluation cycle for start/stop/report schedules."""
    try:
        from .service import BatchService
        from app.state import app_store as _store  # type: ignore
        svc = BatchService(_store)
        batch_types = svc.list_batch_types()
    except Exception as exc:
        _log.debug("schedule tick: cannot reach service: %s", exc)
        return

    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now()  # machine-local wall clock
    now_utc_txt = now_utc.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    for bt in batch_types:
        if not bt.get("enabled"):
            continue
        bt_id = str(bt.get("id"))

        # ---- scheduled START ----
        ss = bt.get("start_schedule")
        if _due_now(ss, now_local) and not _already_fired_this_minute(bt.get("last_scheduled_start_utc"), now_utc):
            try:
                svc.mark_schedule_ran(bt_id, "start", now_utc_txt)
                _fire_start(svc, bt)
                _log.info("schedule: started batch from type %s", bt.get("name"))
            except Exception as exc:
                _log.warning("schedule start failed for %s: %s", bt.get("name"), exc)

        # ---- scheduled STOP ----
        st = bt.get("stop_schedule")
        if _due_now(st, now_local) and not _already_fired_this_minute(bt.get("last_scheduled_stop_utc"), now_utc):
            try:
                svc.mark_schedule_ran(bt_id, "stop", now_utc_txt)
                _fire_stop(svc, bt)
                _log.info("schedule: stopped batches of type %s", bt.get("name"))
            except Exception as exc:
                _log.warning("schedule stop failed for %s: %s", bt.get("name"), exc)

        # ---- scheduled REPORT (email latest batch's PDF/CSV) ----
        rs = bt.get("report_schedule")
        if _due_now(rs, now_local) and not _already_fired_this_minute(bt.get("last_report_utc"), now_utc):
            try:
                svc.mark_schedule_ran(bt_id, "report", now_utc_txt)
                recipients = [s.strip() for s in str(bt.get("email_recipients") or "").replace(";", ",").split(",") if s.strip()]
                latest = svc.latest_batch_for_type(bt_id)
                if recipients and latest:
                    attach_csv = bool((rs or {}).get("attach_csv", True))
                    attach_pdf = bool((rs or {}).get("attach_pdf", True))
                    ok = svc.send_batch_report_email(
                        latest["id"], recipients,
                        attach_pdf=attach_pdf, attach_csv=attach_csv,
                        subject=f"Scheduled batch report — {bt.get('name')}",
                    )
                    _log.info("schedule: report for type %s -> %s (ok=%s)", bt.get("name"), recipients, ok)
            except Exception as exc:
                _log.warning("schedule report failed for %s: %s", bt.get("name"), exc)


def _loop() -> None:
    while True:
        try:
            _tick()
        except Exception as exc:
            _log.exception("triggers loop tick failed: %s", exc)
        try:
            _tick_schedules()
        except Exception as exc:
            _log.exception("schedule loop tick failed: %s", exc)
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
