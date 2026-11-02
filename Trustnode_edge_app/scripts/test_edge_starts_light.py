# -*- coding: utf-8 -*-
"""The edge starts what it is licensed for, and names every thread.

2026-10-30, asked for: "do not load modules that are not enabled or
licensed... priority with collection of data, gateways, triggers and
reports... check if we have better names of each service... check if we have
duplications or redundant processes".

Measured first, with py-spy against the running backend - 37 threads. The
licence on that edge (`/api/health` -> license_summary.modules) lists 24
modules and does NOT list power_management, batch_management, oee or
trustnode_intelligence. Four threads were running for them anyway:

    tn-power-manager, tn-power-writer          (power_management)
    tn-batch-v2-triggers                       (batch_management)

batch_management starts its watcher at IMPORT time in __init__.py, with no
licence check, although every endpoint in that module is gated. The power
manager starts two threads inside __init__, and state.py constructs it
unconditionally.

Four more threads had no name at all and showed as "Thread-1
(_retention_scheduler_loop)" .. "Thread-4 (_config_sync_loop)".

One theory did NOT survive measurement and is recorded here so nobody
re-opens it: _retention_scheduler_loop looked like a duplicate of
tn-retention-engine and is not one. It has run no retention since
2026-08-21; it only keeps the WAL checkpointed. It was misnamed, not
redundant - it is now tn-wal-checkpoint.

The gates fail OPEN. has_module() returns True when the licence cannot be
evaluated, so a licensing hiccup starts the feature rather than silently
stopping a customer's batches. Not-licensed means not started;
cannot-tell means started. That asymmetry is the point and is asserted
below.

    python scripts/test_edge_starts_light.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="tn_light_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-62s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def src(*parts):
    with open(os.path.join(ROOT, "backend", "app", *parts), encoding="utf-8") as fh:
        return fh.read()


def main() -> int:
    print("the edge starts only what it is licensed for")

    # ---------------------------------------------------------- gating
    batch = src("modules", "batch_management", "__init__.py")
    check("the batch watcher asks the licence before starting",
          "_batch_watcher_should_start" in batch and 'has_module("batch_management")' in batch,
          "it started at import with no check at all")
    check("  and still starts when the licence cannot be read",
          re.search(r"except Exception:\s*\n\s*return True", batch) is not None,
          "a licensing hiccup must not stop a paying customer's batches")

    power = src("services", "power_manager.py")
    check("the power manager does not start two threads unasked",
          "_should_autostart" in power and "def ensure_started" in power)
    check("  and starts the moment the feature is used",
          "ensure_started" in src("routers", "power.py"))
    check("  via a real function, not a Depends(lambda)",
          "def _ensure_power_running" in src("routers", "power.py"),
          "a lambda dependency turns every route into a 422")

    # 2026-10-31: the first version of this gate broke the power meters.
    # Meters are configured through /api/app-store/domain - a workspace
    # import and the UI's bootstrap save both land there, never on
    # /api/power - so a configured meter collected NOTHING until somebody
    # opened the Power page. test_export_files_e2e caught it:
    # "the meter writes a CSV: 0 row(s)". The rule is licensed OR
    # configured, and both halves have to be wired.
    store_router = src("routers", "app_store.py")
    check("saving a meter config starts the power manager",
          "power_management_config" in store_router
          and "ensure_started" in store_router,
          "a meter configured through the config API would never collect")

    main_py = src("main.py")
    check("boot starts the power manager when meters are configured",
          "power_management_config" in main_py and "_pm_boot" in main_py,
          "an unlicensed edge with real meters must still collect from them")
    check("the Lite poller asks the licence too",
          'has_module as _has_mod' in main_py and '"cloud_lite_access"' in main_py)

    # ---------------------------------------------------------- naming
    store = src("services", "app_store.py")
    for want in ("tn-wal-checkpoint", "tn-cloud-live-sync",
                 "tn-cloud-cache", "tn-cloud-config-sync"):
        check("thread is named: %s" % want, '"%s"' % want in store,
              "an unnamed thread cannot be diagnosed from a dump")
    check("the WAL thread is no longer called a retention scheduler",
          "tn-wal-checkpoint" in store,
          "the name said the opposite of what it does")

    # ------------------------------------------- the gates behave correctly
    from app.services import access_policy
    real = access_policy.has_module
    try:
        access_policy.has_module = lambda k: False
        from app.modules.batch_management import _batch_watcher_should_start as bw
        check("an unlicensed module does NOT start", bw() is False)

        def boom(_k):
            raise RuntimeError("licence server down")
        access_policy.has_module = boom
        check("  but an unreadable licence DOES start it", bw() is True,
              "fail-closed here would take the feature away on a hiccup")
    finally:
        access_policy.has_module = real

    # --------------------------------------- nothing on the hot path moved
    # The operator's stated priority: collection, gateways, triggers,
    # reports, database. None of these may be gated or renamed.
    plc = src("services", "plc_manager.py")
    for must in ("tn-collect", "tn-persist"):
        check("collection threads are untouched: %s" % must, must in plc)
    check("the report scheduler still starts unconditionally",
          "report_scheduler.start()" in main_py
          and "has_module" not in main_py.split("report_scheduler.start()")[0][-400:],
          "reports are a priority surface and must not be gated")

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
