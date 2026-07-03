"""Daemon thread that runs scheduled insights.

Lightweight cron parser — minute / hour / day-of-month / month / day-of-week
(0=Sunday, 6=Saturday). Special tokens: '*', '*/N', 'a,b,c', 'a-b'.

Starts on import via start_scheduler(); idempotent (calling it twice is
a no-op). Stops when the process exits (daemon=True).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Iterable, Optional

from . import email_sender, service, store


_log = logging.getLogger("trustnode.intelligence.scheduler")
_started = False
_thread: Optional[threading.Thread] = None


# --- cron parsing ---------------------------------------------------------

def _expand_field(expr: str, lo: int, hi: int) -> set:
    expr = (expr or "*").strip()
    if expr == "*":
        return set(range(lo, hi + 1))
    out: set = set()
    for part in expr.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = max(1, int(step_s))
        else:
            base = part
        if base == "*" or base == "":
            rng = range(lo, hi + 1, step)
        elif "-" in base:
            a, b = base.split("-", 1)
            rng = range(int(a), int(b) + 1, step)
        else:
            v = int(base)
            rng = [v]
        out.update(rng)
    return out


def _cron_due(expr: str, now: datetime) -> bool:
    """Return True if `now` matches the cron expression (minute resolution)."""
    parts = expr.strip().split()
    if len(parts) != 5:
        return False
    m, h, dom, mon, dow = parts
    try:
        return (
            now.minute in _expand_field(m, 0, 59) and
            now.hour in _expand_field(h, 0, 23) and
            now.day in _expand_field(dom, 1, 31) and
            now.month in _expand_field(mon, 1, 12) and
            ((now.weekday() + 1) % 7) in _expand_field(dow, 0, 6)  # 0=Sun
        )
    except Exception:
        return False


# --- scheduler loop -------------------------------------------------------

def _run_due_insights() -> None:
    now = datetime.now(timezone.utc)
    for ins in store.all_enabled_insights():
        cron = (ins.get("schedule_cron") or "").strip()
        if not cron:
            continue
        if not _cron_due(cron, now):
            continue
        # Avoid double-firing within the same minute.
        last = (ins.get("last_run_utc") or "").strip()
        if last:
            try:
                last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if (now - last_dt).total_seconds() < 50:
                    continue
            except Exception:
                pass
        _log.info("Running scheduled insight %s (%s)", ins["id"], ins.get("title"))
        started_utc = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        try:
            res = service.run_insight(ins["prompt"], ins["tool_plan"], ins.get("data_source", "local"))
            content = res.get("content") or ""
            err = res.get("error")
            ok = bool(res.get("ok"))
            finished_utc = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            store.update_insight_run(ins["id"], content if ok else None, err if not ok else None)
            # Append to run history (for the Insights page's right column).
            try:
                store.record_insight_run(
                    ins["id"],
                    triggered_by="schedule",
                    started_utc=started_utc,
                    finished_utc=finished_utc,
                    ok=ok,
                    content=content if ok else None,
                    error=err if not ok else None,
                    tool_results=res.get("tool_results") or res.get("tool_log") or [],
                )
            except Exception as _rec_exc:
                _log.warning("record_insight_run failed (scheduled): %s", _rec_exc)
            if ok and ins.get("email_to"):
                send_err = email_sender.send_insight_email(ins["email_to"], ins["title"], content)
                if send_err:
                    _log.warning("Email failed for insight %s: %s", ins["id"], send_err)
        except Exception as exc:
            _log.exception("Insight run failed: %s", exc)
            finished_utc = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            err_msg = f"{type(exc).__name__}: {exc}"
            store.update_insight_run(ins["id"], None, err_msg)
            try:
                store.record_insight_run(
                    ins["id"],
                    triggered_by="schedule",
                    started_utc=started_utc,
                    finished_utc=finished_utc,
                    ok=False,
                    content=None,
                    error=err_msg,
                    tool_results=[],
                )
            except Exception:
                pass


def _loop() -> None:
    # Sleep until next minute boundary, then tick every 60s. Cron has
    # minute resolution so this is plenty.
    #
    # Operator 2026-07-03 (KEEP-IT-SIMPLE): when there are NO scheduled
    # insights, the per-minute wake is a no-op — we still tick (cheap) but
    # `_run_due_insights` returns immediately after one tiny indexed query.
    # The heavy work only runs when a schedule is actually due. This keeps
    # the module quiet: it does NOT poll data or hit the AI on a timer;
    # background activity is one small DB read per minute, and only when the
    # user has created a scheduled insight does it do more.
    while True:
        try:
            now = datetime.now(timezone.utc)
            # Align to next minute.
            sleep_s = 60 - now.second
            time.sleep(max(1, sleep_s))
            _run_due_insights()
        except Exception:
            _log.exception("Scheduler tick failed")
            time.sleep(30)


def start_scheduler() -> None:
    global _started, _thread
    if _started:
        return
    _started = True
    _thread = threading.Thread(target=_loop, name="trustnode-intelligence-scheduler", daemon=True)
    _thread.start()
    _log.info("Insight scheduler started")
