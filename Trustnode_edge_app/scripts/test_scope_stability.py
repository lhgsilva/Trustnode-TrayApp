# -*- coding: utf-8 -*-
"""One edge, one configuration - whoever is logged in.

2026-10-30, reported: "when change from one user to other users dashboard are
lost or get messy not holding layout set by the admin", and "when the app is
restarted configurations of dashboard and charts including interpolation are
being lost".

Both are the same fault. The shared-edge scope key is

    tenant | customer | edge

and the first two segments are not stable:

  * tenant comes from the logged-in user's token. Measured on the operator's
    machine, 20 of 28 local accounts carry `default` while the edge's data
    lives under `tenant-cust-e5916328` - so `admin` and `admin-mari` compute
    DIFFERENT keys on the same PC.
  * customer comes from the bootstrap and is sometimes not known yet, giving
    the placeholder `-`. The operator's own store holds both
    `tenant-cust-e5916328|cust-e5916328|edge-74d903ffcd` and
    `tenant-cust-e5916328|-|edge-74d903ffcd`.

canonical_scope_key exists to collapse that, but it only adopted siblings
with the SAME customer - so the `-` form could never find the real one. One
login wrote one document while another read the other.

Runs against a scratch database. Never touches a live workspace.

    python scripts/test_scope_stability.py
"""
from __future__ import annotations

import os
import sys
import tempfile

# A scratch workspace BEFORE app.state is imported anywhere: importing it
# constructs AppStore against whatever the environment points at, and that is
# how a test has migrated a live store before.
_TMP = tempfile.mkdtemp(prefix="tn_scope_test_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.services.app_store import AppStore   # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-62s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if detail else ""))


def main() -> int:
    print("scope stability (one edge, one configuration)")
    store = AppStore()

    EDGE = "edge-74d903ffcd"
    CUST = "cust-e5916328"
    REAL = "tenant-cust-e5916328|%s|%s" % (CUST, EDGE)

    # The admin saves a dashboard while logged in with the real tenant.
    store.save_bootstrap_scoped(REAL, {"dashboard_configurations": [
        {"id": "dash1", "name": "Line 1", "widgets": [{"id": "w1", "x": 0, "y": 0}]}]},
        actor="admin-mari")
    saved = store.get_scoped_domain_payload(REAL, "dashboard_configurations")
    check("the admin's dashboard is stored", bool(saved), "%d item(s)" % len(saved or []))

    # Now a second operator logs in. Their account carries tenant `default`,
    # and the bootstrap has not told them the customer yet, so they compute a
    # different key for the SAME machine.
    for other in ("default|%s|%s" % (CUST, EDGE),      # tenant differs
                  "tenant-cust-e5916328|-|%s" % EDGE,  # customer unknown
                  "default|-|%s" % EDGE):              # both differ
        resolved = store.canonical_scope_key(other)
        got = store.get_scoped_domain_payload(resolved, "dashboard_configurations")
        check("a login computing %s sees the same dashboard" % other,
              bool(got) and len(got) == len(saved or []),
              "resolved to %s -> %d item(s)" % (resolved, len(got or [])))

    # And the guard that matters for data protection: a DIFFERENT edge must
    # never adopt this one's configuration.
    other_edge = store.canonical_scope_key("default|-|edge-someone-else")
    got = store.get_scoped_domain_payload(other_edge, "dashboard_configurations")
    check("a different edge does NOT inherit it", not got,
          "resolved to %s" % other_edge)

    # Nor may a known, different customer on a same-named edge.
    foreign = store.canonical_scope_key("default|cust-somebody-else|%s" % EDGE)
    got = store.get_scoped_domain_payload(foreign, "dashboard_configurations")
    check("a different KNOWN customer does NOT inherit it", not got,
          "resolved to %s" % foreign)

    # --- A NEW CUSTOMER, A NEW COMPUTER -------------------------------
    # 2026-10-30, corrected by the operator: "I am talking about a new
    # installation in a new customer, so I am not sure how the tenant is
    # relevant in new installation in new computers".
    #
    # Fair. On a fresh machine there are no old keys to collide with - but
    # there IS a moment when the key changes under the app's feet:
    # ACTIVATION. Before it the edge has no tenant and no customer, so its
    # configuration is written under `default|-|edge-x`. Activation gives it
    # both, and from the next read the key is `tenant-A|cust-B|edge-x`.
    #
    # If that new key cannot find the old documents, everything the customer
    # set up before activating - dashboards, charts, interpolation - is
    # simply gone. That is the new-install shape of "configurations are lost".
    FRESH_EDGE = "edge-newpc-7a1c"
    store.save_bootstrap_scoped("default|-|%s" % FRESH_EDGE, {
        "dashboard_configurations": [
            {"id": "d1", "name": "Before activation",
             "widgets": [{"id": "w1", "x": 0, "y": 0}, {"id": "w2", "x": 3, "y": 0}]}]},
        actor="installer")
    after_activation = store.canonical_scope_key(
        "tenant-newcust|cust-newcust|%s" % FRESH_EDGE)
    got = store.get_scoped_domain_payload(after_activation, "dashboard_configurations")
    check("activation keeps what the customer set up before it",
          bool(got), "resolved to %s -> %d item(s)" % (after_activation, len(got or [])))

    # The same guard, from the other direction: an activated edge must not
    # pick up a DIFFERENT customer's pre-activation documents.
    store.save_bootstrap_scoped("default|cust-someoneelse|edge-theirs", {
        "dashboard_configurations": [{"id": "x", "name": "Theirs", "widgets": []}]},
        actor="other")
    mine = store.canonical_scope_key("tenant-newcust|cust-newcust|edge-mine-only")
    got = store.get_scoped_domain_payload(mine, "dashboard_configurations")
    check("and never another customer's", not got, "resolved to %s" % mine)

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
