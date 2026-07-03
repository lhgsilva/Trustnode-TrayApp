"""TrustNode Intelligence — AI chat + insights module.

Self-contained module. To install on an existing Edge:
  1. Copy this folder to <edge_root>/trustnode_intelligence/
  2. Add to backend/app/main.py:
       from trustnode_intelligence.backend.router import router as intelligence_router
       app.include_router(intelligence_router)
  3. Register the menu entries in App.jsx (see frontend/IntelligenceMenu.jsx)
  4. Restart backend.

License gate: customer's license must include the `trustnode_intelligence`
module key, plus the AI endpoint URL + model + token configured via portal.
"""

__all__ = ["router"]


# Operator 2026-07-02: kick off the ONE-shot AI Endpoint config pull
# from the VPS portal on module import. Runs in a background thread so
# backend boot is never delayed. Silent on all failures. This is the
# ONLY refresh mechanism — the license-check enrichment and any per-
# request refresh loops were removed after they caused wedges.
try:
    from .refresh import start_boot_refresh as _start_boot_refresh
    _start_boot_refresh()
except Exception:
    pass
