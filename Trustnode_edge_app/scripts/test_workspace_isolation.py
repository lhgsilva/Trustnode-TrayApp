# -*- coding: utf-8 -*-
"""A workspace carries what the operator built, never who the machine is.

2026-10-30, from a real sequence: a machine was wiped, reinstalled, activated
as a NEW customer, and then the OLD machine's workspace was imported. The
import wrote app_settings back verbatim - including edge_profile - plus the
previous licence receipt and every user's old tenant_id, and re-mirrored that
activation into the registry. The fresh edge then:

  * wrote configuration under a scope built from the OLD customer and edge id,
    split from the scope it read from;
  * showed the OLD tenant's licensed modules;
  * said "pushed to the cloud" with no cloud configured;
  * ran an OPC UA outbound connection it does not license - which deadlocked
    and dropped collection from 1 s to 8.02 s per cycle.

The operator's rule, in their words: "we should not import workspaces with
different tenants information to not have conflicts, and when it does it
should be able to self-heal to leave the app functional without fails."

So: content travels, identity does not, and a foreign file is cleaned rather
than refused - the operator still gets their gateways, dashboards and reports.

    python scripts/test_workspace_isolation.py
"""
from __future__ import annotations

import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="tn_ws_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-62s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if detail else ""))


def main() -> int:
    print("workspace import/export isolation")
    from app.routers.workspace import _strip_machine_identity, _IDENTITY_KEYS, _INFRASTRUCTURE_KEYS

    # A workspace exactly like the one that caused the outage.
    old_machine = {
        "app_settings": {
            # identity of the OLD machine
            "edge_profile": {"edge_id": "edge-74d903ffcd",
                             "linked_customer_id": "cust-e5916328",
                             "linked_tenant": "tenant-cust-e5916328"},
            "edge_id": "edge-74d903ffcd",
            "customer_id": "cust-e5916328",
            "tenant_id": "tenant-cust-e5916328",
            # infrastructure of the OLD machine
            "cloud_url": "https://trustnode.lsapps.app",
            "cloud_auto_sync_enabled": True,
            "connections": {"opcua": {"enabled": True, "runtime": "python"}},
            "lan_sharing_enabled": True,
            # things the operator actually BUILT - these must survive
            "theme": "dark",
            "report_columns": ["work_order", "result"],
        },
        "gateway_configurations": [{"id": "gw-1", "name": "PLC", "interval_ms": 1000}],
        "dashboard_configurations": [{"id": "d1", "widgets": [{"id": "w1"}]}],
        "alarms_setup": [{"id": "a1"}],
    }

    clean, stripped = _strip_machine_identity(old_machine)
    s = clean["app_settings"]

    for k in _IDENTITY_KEYS:
        check("identity removed: app_settings.%s" % k, k not in s)
    for k in _INFRASTRUCTURE_KEYS:
        check("infrastructure removed: app_settings.%s" % k, k not in s)

    check("the operator's own settings survive",
          s.get("theme") == "dark" and s.get("report_columns") == ["work_order", "result"])
    check("gateways survive", clean.get("gateway_configurations") == old_machine["gateway_configurations"])
    check("dashboards survive", clean.get("dashboard_configurations") == old_machine["dashboard_configurations"])
    check("alarms survive", clean.get("alarms_setup") == old_machine["alarms_setup"])
    check("the caller is told what was removed", len(stripped) >= 8,
          "%d item(s)" % len(stripped))

    # Self-healing, not refusing: a foreign file still yields a usable app.
    check("a foreign workspace is cleaned, not rejected",
          isinstance(clean, dict) and bool(clean.get("gateway_configurations")))

    # And the source file is never mutated.
    check("the original payload is not modified in place",
          old_machine["app_settings"].get("edge_id") == "edge-74d903ffcd")

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
