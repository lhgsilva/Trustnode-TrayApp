# -*- coding: utf-8 -*-
"""The Lite surface's target is a PORTAL setting, not a file pushed by SSH.

2026-10-13, owner's design: "webclient / lite version also in the future can
be moved migrated to different provider", and more generally "the whole system
should be able of migration".

Most of that was already built - cp_infrastructure_config holds the endpoints,
global with per-tenant overrides, and they ride into the edge inside the
activation code. Two things were not:

  * `lite_url` was not one of the keys, so where Lite lives could not be
    recorded in the portal at all;
  * /lite/config.json is a STATIC file written to the web root over SSH by
    scripts/write_lite_config_to_vps.py, so moving the Lite backend meant
    hand-editing a file on every host that serves it.

This covers the endpoint that resolves that config from the control plane, and
the precedence it must follow. Writes into a scratch workspace; never the live
store, and it reaches no network.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
_TMP = tempfile.mkdtemp(prefix="tn-portable-")
os.environ.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=_TMP,
                  TRUSTNODE_APP_STORE_PATH=os.path.join(_TMP, "s.db"),
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("TrustNode - the portal owns where things live")
print()

from app.services.control_plane_store import ControlPlaneStore      # noqa: E402

print("[every migratable surface has a key in the portal]")
KEYS = ControlPlaneStore.INFRA_KEYS
for want, why in (
    ("cloud_api_url", "the portal/control-plane API can move host"),
    ("supabase_url", "the portal database can move project"),
    ("web_client_url", "the web client can move"),
    ("lite_url", "Lite/mobile can move - added 2026-10-13"),
    ("ai_endpoint_url", "the AI endpoint can move"),
):
    check("  %-16s %s" % (want, why), want in KEYS, KEYS)

print()
print("[per-tenant overrides the global default, key by key]")
store = ControlPlaneStore(db_path=os.path.join(_TMP, "cp.db")) \
    if "db_path" in ControlPlaneStore.__init__.__code__.co_varnames \
    else ControlPlaneStore()
try:
    store.set_infrastructure_config(
        endpoints={"cloud_api_url": "https://portal.example.com",
                   "lite_url": "https://lite.example.com",
                   "supabase_url": "https://global.supabase.co"},
        tenant_id=ControlPlaneStore.GLOBAL_INFRA_TENANT)
    store.set_infrastructure_config(
        endpoints={"lite_url": "https://lite.customer-a.example.com"},
        tenant_id="tenant-cust-a")
    glob = store.resolve_infrastructure_endpoints()
    cust = store.resolve_infrastructure_endpoints(tenant_id="tenant-cust-a")
    check("the global default resolves",
          glob.get("lite_url") == "https://lite.example.com", glob)
    check("  a tenant overrides just its own key",
          cust.get("lite_url") == "https://lite.customer-a.example.com", cust)
    check("  and INHERITS the keys it did not set",
          cust.get("cloud_api_url") == "https://portal.example.com",
          "a partial override must not blank the rest: %s" % (cust,))
    store.set_infrastructure_config(endpoints={"lite_url": ""},
                                    tenant_id="tenant-cust-a")
    back = store.resolve_infrastructure_endpoints(tenant_id="tenant-cust-a")
    check("  clearing an override falls back to global",
          back.get("lite_url") == "https://lite.example.com", back)
except Exception as exc:                                            # noqa: BLE001
    check("per-tenant endpoint resolution", False, repr(exc)[:160])

print()
print("[the Lite config endpoint resolves from the control plane]")
from app.routers.lite_local import get_lite_config                  # noqa: E402


class _Req:
    headers: dict = {}


os.environ["TRUSTNODE_SUPABASE_ANON_KEY"] = "anon-key-for-test"
os.environ["TRUSTNODE_SUPABASE_URL"] = "https://env-fallback.supabase.co"

cfg = get_lite_config(_Req(), tenant_id="")
check("it answers at all", cfg.get("ok") is True, cfg)
check("  it reports WHERE the answer came from",
      cfg.get("source") in ("control_plane", "environment"), cfg.get("source"))
check("  the anon key is served (it is public by design)",
      cfg.get("supabase_anon_key") == "anon-key-for-test",
      "Lite cannot talk to Supabase without it; RLS is what protects the data")
check("  the SERVICE key is never exposed",
      not any("service" in str(k).lower() for k in cfg),
      "the service key bypasses RLS: %s" % sorted(cfg))
check("  a supabase_url is always produced",
      str(cfg.get("supabase_url") or "").startswith("https://"),
      cfg.get("supabase_url"))

print()
print("[precedence: the portal wins, the environment is the fallback]")
# A standalone edge, or one whose control plane is unreachable, must still
# serve a usable Lite config. Force the control-plane read to fail - the `or`
# version of this assertion passed without ever exercising the fallback.
import app.state as _state                                          # noqa: E402


class _Exploding:
    def resolve_infrastructure_endpoints(self, **_kw):
        raise RuntimeError("control plane unreachable")


_saved = _state.control_plane_store
_state.control_plane_store = _Exploding()
try:
    off = get_lite_config(_Req(), tenant_id="")
finally:
    _state.control_plane_store = _saved

check("an unreachable control plane degrades to the environment",
      off.get("source") == "environment"
      and off.get("supabase_url") == "https://env-fallback.supabase.co",
      "%s via %s" % (off.get("supabase_url"), off.get("source")))
check("  and still serves the anon key, so Lite keeps working",
      off.get("supabase_anon_key") == "anon-key-for-test", off)
check("  the portal still wins when it IS reachable",
      cfg.get("source") == "control_plane"
      and cfg.get("supabase_url") == "https://global.supabase.co",
      "%s via %s" % (cfg.get("supabase_url"), cfg.get("source")))

print()
print("[nothing that already worked has changed]")
SRC = io.open(os.path.join(ROOT, "backend", "app", "routers", "lite_local.py"),
              encoding="utf-8").read()
check("the new route is additive",
      SRC.count('@router.get("/config")') == 1
      and '@router.get("/capabilities")' in SRC
      and '@router.get("/bootstrap")' in SRC,
      "the existing Lite endpoints must be untouched")
check("  it cannot take the Lite API down if the portal is unreachable",
      "except Exception:" in SRC.split('@router.get("/config")')[1][:1600],
      "a control-plane outage must degrade to the environment, not 500")
check("  the static file remains the documented fallback",
      os.path.exists(os.path.join(ROOT, "scripts", "write_lite_config_to_vps.py")),
      "an edge with no control plane still needs a config.json on disk")

UI = io.open(os.path.join(ROOT, "frontend", "src", "components", "ControlPlane",
                          "InfrastructureEndpoints.jsx"), encoding="utf-8").read()
check("the portal offers the new field", '"lite_url"' in UI,
      "a setting with no UI is a setting nobody changes")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
