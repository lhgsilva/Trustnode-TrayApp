"""TrustNode OEE module.

Consumes the EXISTING collection system - gateways, devices, tags, historian -
and adds only what OEE itself needs. Nothing here polls a device or writes a
tag; the gateways keep doing that, and OEE reads what they already store.

  schema.py        the 16 oee_* tables (called from AppStore._ensure_schema)
  store.py         all DB access
  state_engine.py  machine state from signals, power rules, or both
  calc.py          the OEE arithmetic, pure functions
  service.py       joins configuration to collected data
  seed.py          default downtime/quality reasons
  router.py        REST surface at /api/oee

See docs/OEE_MODULE_PLAN.md for the architecture notes, in particular WHY the
links to gateways/devices/tags are soft references and not foreign keys.
"""

from .router import router as oee_router  # noqa: F401

__all__ = ["oee_router"]
