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

# Operator 2026-06-30: kick the PLC-driven auto-trigger watcher on
# import. Idempotent + daemon thread, so safe to call multiple times.
# Wrapped in try/except so a watcher failure never blocks the rest of
# the module from loading.
try:
    from .triggers import start_trigger_watcher as _start_trigger_watcher
    _start_trigger_watcher()
except Exception:
    pass
