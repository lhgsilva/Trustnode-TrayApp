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
try:
    from .triggers_v2 import start_trigger_watcher_v2 as _start_v2
    _start_v2()
except Exception:
    pass
