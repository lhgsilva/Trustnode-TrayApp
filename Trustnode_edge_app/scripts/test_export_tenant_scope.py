# -*- coding: utf-8 -*-
"""The export pickers and the export itself must agree on the tenant.

2026-10-08, reported: the Data Export page is "not loading the data to be
filters, gateways and tags to be selected".

The page was not slow and nothing errored - it was reading the wrong tenant.
Every historian row on the reporting machine is written under
`tenant-cust-e5916328`; `/api/data-export/sources` hand-rolled
`WHERE tenant_id = ?` from `_current_tenant_id()`, and its "widen" fallback
kept the same predicate, so both queries returned nothing and the pickers
came back empty. Measured read-only against that 5 GB store: with the right
tenant the very same query answers in 0.4 s with 3 gateways, 2 devices and
119 tags.

A second copy of a scoping rule that already exists is the recurring shape
here (see the hand-rolled scope key in meter_export_and_scope_reads). Both
the pickers and the export now go through the one rule.

Runs against a scratch store; the live one is never opened.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
_TMP = tempfile.mkdtemp(prefix="tn-exptenant-")
os.environ.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=_TMP,
                  TRUSTNODE_APP_STORE_PATH=os.path.join(_TMP, "s.db"),
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


from app.services import data_export as _q                  # noqa: E402

print("TrustNode - the export reads the tenant the data is under")
print()

print("[the query builder accepts the tenants the picker resolved]")
sql_one, params_one, _cols = _q.build_query({}, "tenant-cust-e5916328")
check("a single tenant still uses the indexed equality",
      "tenant_id = ?" in sql_one and params_one[0] == "tenant-cust-e5916328",
      sql_one.split("WHERE", 1)[-1].strip()[:70])

sql_many, params_many, _c2 = _q.build_query(
    {}, ["tenant-cust-e5916328", "tenant-old-1"])
check("several tenants become an IN list",
      "tenant_id IN (?,?)" in sql_many
      and params_many[:2] == ["tenant-cust-e5916328", "tenant-old-1"],
      sql_many.split("WHERE", 1)[-1].strip()[:70])
check("  a bridged prior tenant is therefore exportable",
      len(params_many) >= 2,
      "the hand-rolled version silently excluded it")

print()
print("[one rule, used by the pickers AND the export]")
router = io.open(os.path.join(ROOT, "backend", "app", "routers",
                              "data_export.py"),
                 encoding="utf-8", errors="replace").read()
check("the pickers use the canonical tenant clause",
      "app_store._tenant_filter_clause(\"tenant_id\")" in router,
      "that helper is what every working historian read uses")
check("  preview and run share it", router.count("_export_tenants()") >= 3,
      "the picker offering a tag the export cannot return is the bug")
check("  no hand-rolled tenant equality is left in the SQL",
      'WHERE tenant_id = ? ")' not in router,
      "a second copy of a scoping rule is how the two drifted apart")

print()
print("[an empty page must say WHY it is empty]")
check("a tenant with no rows is reported, not just blank",
      "rows_outside_tenant" in router and "note" in router,
      "an empty picker and a tenant mismatch looked identical")
check("  without naming another customer's tenant",
      "SELECT COUNT(*) FROM historian_readings " in router
      and "different one" in router,
      "a count is enough to explain it; an id would be a scope leak")

print()
print("[the widen fallback still exists for a quiet historian]")
check("an old-but-real historian still fills the pickers",
      'out["widened"] = bool(rows)' in router,
      "a store last written weeks ago is still exportable")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
