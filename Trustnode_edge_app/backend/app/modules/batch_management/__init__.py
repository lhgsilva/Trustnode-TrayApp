"""TrustNode Batch Management & Traceability module.

License-gated feature. Files:

  - models.py    pydantic schemas for the REST surface
  - service.py   BatchService — all DB access lives here
  - router.py    FastAPI router, every endpoint gated by
                 require_batch_management_license()
  - triggers.py  PLC-trigger evaluator (reuses the existing
                 collection_triggers logic, READ-ONLY)
  - reports.py   PDF report generators built on reportlab
"""

from .router import router as batch_router  # noqa: F401
# v2 clean-rebuild router (spec-named endpoints). Mounted alongside the legacy
# router; the legacy endpoints remain but are no longer used by the new UI.
try:
    from .router_v2 import router as batch_router_v2  # noqa: F401
except Exception:  # pragma: no cover - keep module importable if v2 router absent
    batch_router_v2 = None  # type: ignore

# 2026-07-14 CLEAN REBUILD: the batch module now runs a SINGLE trigger daemon —
# the v2 watcher (triggers_v2). The LEGACY triggers.py daemon is deliberately NOT
# started so two loops never both create batches. The legacy daemon's evaluation
# helpers are still imported/reused by triggers_v2; only its auto-start is retired.
# Guide: docs/BATCH_MANAGEMENT_REDESIGN_2026-07-14.md
# 2026-10-30, measured: this daemon started at IMPORT time with no licence
# check at all, on an edge whose licence does not list batch_management.
# Every endpoint in this module is gated by
# require_batch_management_license() - the daemon behind them was not, so an
# unlicensed edge still ran a polling loop against the historian for a
# feature nobody could open.
#
# has_module() deliberately returns True when the licence cannot be
# evaluated, so a licensing hiccup starts the watcher rather than silently
# stopping batches on a customer who pays for them. Not-licensed means not
# started; cannot-tell means started.
def _batch_watcher_should_start() -> bool:
    try:
        from app.services.access_policy import has_module
        return bool(has_module("batch_management"))
    except Exception:
        return True


try:
    if _batch_watcher_should_start():
        from .triggers_v2 import start_trigger_watcher_v2 as _start_v2
        _start_v2()
    else:
        print("[trustnode][boot] batch management is not licensed - "
              "trigger watcher not started", flush=True)
except Exception:
    pass
