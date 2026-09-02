# -*- coding: utf-8 -*-
"""One customer's configuration must never be served to another.

2026-08-31, live install. Saved configuration:

    tenant-cust-e5916328|cust-e5916328|edge-74d903ffcd  ->  PLC/point_io@192.168.10.105

What /api/app-store/bootstrap actually returned:

    PLC / allen_bradley @ 192.168.10.240, tags ["SimREAL[3]"]

sourced from a legacy smoke-test scope under a DIFFERENT customer,
`default|cust-09ab9941|smk-persist-edge-23f26da`. The operator saw a gateway
row carrying one device's protocol and another's address and reported that
"gateways configuration and names got mixed up".

Cause: the read-fallback chain's last-resort net probes `{tenant}|%|%`. The
resolved shared scope began with the placeholder segment "default", so the
pattern became `default|%|%` and matched every legacy default-scoped document
on the machine regardless of customer; the richest was overlaid as the
operator's own.

The rescue paths this net exists for are all anchored to a real identity -
legacy 2-segment, same customer + edge, same customer any edge, same edge id -
and those must keep working. Only matching on a placeholder tenant is wrong.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:56s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:140]) if detail else ""))
    if not ok:
        FAILS.append(name)


from app.services.app_store import AppStore  # noqa: E402

store = AppStore.__new__(AppStore)

tmp = tempfile.mkdtemp(prefix="tn-scope-")
db = os.path.join(tmp, "s.db")
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
conn.execute("""CREATE TABLE config_documents_scoped (
                  scope_key TEXT NOT NULL, domain TEXT NOT NULL,
                  payload_json TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                  updated_utc TEXT, PRIMARY KEY (scope_key, domain))""")

FOREIGN = "default|cust-09ab9941|smk-persist-edge-23f26da"   # another customer
MINE_T = "tenant-cust-e5916328|cust-e5916328|edge-74d903ffcd"
MINE_E = "default|cust-e5916328|edge-99999999"               # same customer, other edge
SAME_EDGE = "tenant-other|cust-other|edge-74d903ffcd"        # same edge id
rows = [
    # Deliberately the RICHEST document, so a score-ranked net would pick it.
    (FOREIGN, "gateway_configurations", "[" + ("{\"id\":\"foreign\"}," * 60).rstrip(",") + "]"),
    (MINE_T, "gateway_configurations", '[{"id":"mine","plc_ip":"192.168.10.105"}]'),
    (MINE_E, "gateway_configurations", '[{"id":"mine-other-edge"}]'),
    (SAME_EDGE, "gateway_configurations", '[{"id":"same-edge"}]'),
]
conn.executemany("INSERT INTO config_documents_scoped (scope_key, domain, payload_json) "
                 "VALUES (?,?,?)", rows)
conn.commit()

print("[a placeholder tenant must not match every scope]")
keys = store._build_read_fallback_scope_keys(conn, "default|cust-e5916328|edge-74d903ffcd")
check("another customer's scope is NOT a candidate", FOREIGN not in keys,
      "candidates: %s" % ", ".join(k[:38] for k in keys))
check("the same customer's other edge still is", MINE_E in keys,
      "the rescue this net exists for must keep working")
check("the same edge id under another tenant still is", SAME_EDGE in keys,
      "an edge id does identify one edge")
check("the requested scope is still probed", "default|cust-e5916328|edge-74d903ffcd" in keys)

print()
print("[a REAL tenant id may still match by tenant]")
keys2 = store._build_read_fallback_scope_keys(conn, "tenant-cust-e5916328|cust-e5916328|edge-x")
check("a real tenant still reaches its own scopes", MINE_T in keys2,
      "candidates: %s" % ", ".join(k[:38] for k in keys2))
check("  and still not the other customer's", FOREIGN not in keys2)

conn.close()
print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
