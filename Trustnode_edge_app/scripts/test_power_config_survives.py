# -*- coding: utf-8 -*-
"""A configured power meter must survive a restart. Always.

2026-08-26, reported: "after reloaded the new app, the meter configured did not
load, some how we lost it."

It was not lost by the operator. `_ensure_config_loaded` fell back to
DEFAULT_POWER_CONFIG whenever the store could not be read - app_store locked at
boot, a slow read, a partial bootstrap - and then WROTE THAT FALLBACK BACK a few
lines later to apply the force-stopped policy. A transient read failure at boot
therefore destroyed the configuration permanently. Three separate paths reached
it: the read-miss seed, the parse failure, and the exception fallback.

A read that did not succeed is not a configuration. These tests hold the line:

  * a normal restart keeps the meter;
  * a restart where the store read FAILS keeps the meter and writes nothing;
  * a partial save (tariffs only) does not blank the meters;
  * a system/background write can never remove the last meter;
  * an operator deleting their own meter still works.
"""
import io
import os
import sys
import time
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

TMP = tempfile.mkdtemp(prefix="tn-pcfg-")
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_DATA_DIR"] = TMP
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(TMP, "s.db")
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"

FAILS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:110]) if detail else ""
    print("  {0:58s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


from app.state import app_store  # noqa: E402
from app.services.power_manager import PowerManager  # noqa: E402

METER = {
    "id": "PM001", "name": "Glass Innovation", "enabled": True,
    "type": "modbus_tcp", "protocol": "modbus_tcp",
    "ip": "192.168.1.117", "port": 502, "unit_id": 1,
    "poll_interval_ms": 1000, "electrical_mode": "three_phase",
}


_LIVE = []


def fresh_manager():
    """A new PowerManager over the same store - i.e. an app restart.

    PowerManager.__init__ STARTS its poll loop and writer thread, and those
    loops load and re-save configuration. The product runs exactly one manager;
    leaving previous ones alive here would have them writing the store
    underneath the manager being tested, so each restart stops the last.
    """
    for prev in _LIVE:
        try:
            prev._stop.set()
            prev._writer_stop.set()
        except Exception:
            pass
    _LIVE.clear()
    m = PowerManager.__new__(PowerManager)
    PowerManager.__init__(m, app_store)
    _LIVE.append(m)
    time.sleep(0.35)          # let the previous loops notice and settle
    return m


# --- the operator configures a meter --------------------------------------
print("[the meter is configured and saved]")
mgr = fresh_manager()
mgr.update_config({"enabled": True, "devices": [METER],
                   "selected_device_id": "PM001"}, actor="admin")
cfg = mgr.get_config()
check("the meter is saved", len(cfg.get("devices") or []) == 1,
      [d.get("name") for d in cfg.get("devices") or []])

# --- restart ---------------------------------------------------------------
print("\n[the app restarts]")
mgr2 = fresh_manager()
cfg2 = mgr2.get_config()
devs = cfg2.get("devices") or []
check("THE METER SURVIVES A RESTART", len(devs) == 1,
      [d.get("name") for d in devs])
check("  with its address intact",
      devs and str(devs[0].get("ip")) == "192.168.1.117",
      devs[0].get("ip") if devs else None)
check("  and its id", devs and str(devs[0].get("id")) == "PM001",
      devs[0].get("id") if devs else None)

# --- restart while the store cannot be read -------------------------------
# The exact failure: bootstrap unavailable at boot. It must NOT be treated as
# "there is no configuration", and it must NOT be written back.
print("\n[the app restarts while the store cannot be read]")
real_bootstrap = app_store.get_bootstrap
real_fast = app_store.get_domain_fast


def broken_bootstrap(*a, **kw):
    raise RuntimeError("app_store is locked")


# Break BOTH read paths before the manager starts, so its very first load
# genuinely fails - that is the boot this incident happened on. There are two
# now: get_domain_fast (the single-row fast path added 2026-08-26) and the full
# bootstrap it falls back to. Breaking only one proves nothing.
app_store.get_bootstrap = broken_bootstrap
app_store.get_domain_fast = broken_bootstrap
try:
    mgr3 = fresh_manager()
    served = mgr3.get_config()
    check("a failed read still serves a usable dict", isinstance(served, dict))
    check("  and does NOT latch the fallback as loaded",
          getattr(mgr3, "_config_loaded", True) is False,
          getattr(mgr3, "_config_loaded", None))
    check("  and serves no phantom meters",
          len(served.get("devices") or []) == 0, served.get("devices"))
finally:
    app_store.get_bootstrap = real_bootstrap
    app_store.get_domain_fast = real_fast

# the store must be untouched by that failed boot
mgr4 = fresh_manager()
devs4 = mgr4.get_config().get("devices") or []
check("  THE STORED METER IS STILL THERE", len(devs4) == 1,
      [d.get("name") for d in devs4])

# and once the store reads again, the manager recovers on its own
recovered = mgr3.get_config()
check("  the same manager recovers once the store answers",
      len(recovered.get("devices") or []) == 1,
      [d.get("name") for d in recovered.get("devices") or []])

# --- a partial save --------------------------------------------------------
print("\n[a partial save must not blank the meters]")
mgr5 = fresh_manager()
mgr5.update_config({"energy_price_eur_kwh": 0.34}, actor="admin")
after = mgr5.get_config()
check("saving only a price keeps the meters",
      len(after.get("devices") or []) == 1,
      [d.get("name") for d in after.get("devices") or []])
check("  and the price was applied",
      abs(float(after.get("energy_price_eur_kwh") or 0) - 0.34) < 1e-6,
      after.get("energy_price_eur_kwh"))

# --- a system write can never delete the last meter ------------------------
print("\n[a background write can never delete the last meter]")
mgr6 = fresh_manager()
mgr6.update_config({"devices": []}, actor="system")
check("a system write with no devices is REFUSED",
      len(mgr6.get_config().get("devices") or []) == 1,
      [d.get("name") for d in mgr6.get_config().get("devices") or []])
mgr7 = fresh_manager()
check("  and the store still holds it", len(mgr7.get_config().get("devices") or []) == 1)

# --- but the operator is still in charge -----------------------------------
print("\n[the operator can still delete their own meter]")
mgr8 = fresh_manager()
mgr8.update_config({"devices": [], "selected_device_id": ""}, actor="admin")
check("an operator CAN delete the last meter",
      len(mgr8.get_config().get("devices") or []) == 0,
      mgr8.get_config().get("devices"))
mgr9 = fresh_manager()
check("  and that deletion persists", len(mgr9.get_config().get("devices") or []) == 0)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
