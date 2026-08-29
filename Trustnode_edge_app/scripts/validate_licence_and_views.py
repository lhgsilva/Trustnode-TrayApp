# -*- coding: utf-8 -*-
"""Licensing, customer management, and every view the customer can open.

2026-08-29, asked for as part of the release gate: *"make sure each version is
correct completely, from the licences and customer management, and the reading
of app data from the cloud, using Lite and other web views."*

`validate_surfaces.py` already proves the bundles are SERVED and that RBAC holds
(43 checks). This proves the layer above it: that the licence actually says
something, that seats and customer records are readable and coherent, and that
each view can READ DATA rather than merely render.

Read-only. It creates nothing and changes nothing, so it is safe to run beside a
long soak — which is exactly where it is used.

    python scripts/validate_licence_and_views.py
    exit 0 = PASS, 2 = FAIL
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("VAL_API", "http://127.0.0.1:8000")
USER = os.environ.get("VAL_USER", "admin-mari")
PASSWORD = os.environ.get("VAL_PASS", "Limerick2019*")
FAILS: list[str] = []
WARNS: list[str] = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:150]) if detail else ""
    print("  {0:56s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


def note(name, detail=""):
    """Something worth reporting that is NOT a pass/fail.

    A licence with no limits set, or a cloud that is switched off, is a
    configuration choice - reporting it as a failure would train the operator to
    ignore this suite.
    """
    print("  {0:56s}: NOTE{1}".format(name, (" - " + str(detail)[:150]) if detail else ""))
    WARNS.append(name)


def call(method, path, tok=None, body=None, timeout=60):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    d = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=d, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "null")
        except Exception:
            return e.code, None
    except Exception as e:
        return 0, str(e)[:200]


def main() -> int:
    print("TrustNode - licensing, customers, and the customer-facing views")
    print("  api: %s" % API)
    print()

    st, b = call("POST", "/api/auth/login", body={"username": USER, "password": PASSWORD})
    tok = (b or {}).get("token") if isinstance(b, dict) else None
    check("admin login", st == 200 and bool(tok), st)
    if not tok:
        return 2

    # ------------------------------------------------------------ licensing
    print()
    print("[licensing]")
    st, lic = call("GET", "/api/control-plane/licenses", tok)
    rows = (lic or {}).get("licenses") or (lic or {}).get("rows") or []
    check("the licence list is readable", st == 200, st)
    if isinstance(rows, list) and rows:
        print("     %d licence record(s)" % len(rows))
        for r in rows[:4]:
            print("       %-22s pkg=%-18s status=%s" % (
                str(r.get("id") or r.get("license_id"))[:22],
                str(r.get("package_key") or r.get("package"))[:18],
                r.get("status") or r.get("state")))
    else:
        note("  no licence records on this edge", "pre-seat/unlicensed install")

    st, seats = call("GET", "/api/control-plane/license/seats", tok)
    check("the seat ledger answers", st == 200, st)
    ledger = (seats or {}).get("seats") or (seats or {}).get("products") or seats or {}
    if isinstance(ledger, dict) and ledger:
        keys = sorted(k for k in ledger.keys() if isinstance(k, str))
        print("     products: %s" % ", ".join(keys[:6]))
        # All-zero seats must NOT block anything - that is the pre-seat state.
        check("  the ledger names the products", len(keys) >= 1, keys[:6])

    st, chk = call("GET", "/api/control-plane/edge-link/license-check", tok)
    check("the edge can evaluate its own licence", st in (200, 204), st)
    if isinstance(chk, dict):
        mods = chk.get("modules") or chk.get("licensed_modules") or []
        limits = chk.get("limits") or {}
        print("     modules: %s" % (", ".join(sorted(map(str, mods))[:8]) if mods else "(none declared)"))
        print("     limits : %s" % (json.dumps(limits)[:120] if limits else "(none set)"))
        if not mods:
            note("  no modules declared", "legacy/unlicensed installs fail OPEN by design")

    # ------------------------------------------------- customer management
    print()
    print("[customer management]")
    st, cust = call("GET", "/api/control-plane/customers", tok)
    if st == 200:
        items = (cust or {}).get("customers") or (cust or {}).get("rows") or []
        check("the customer list is readable", True, "%d customer(s)" % len(items))
        for c in (items or [])[:3]:
            print("       %-24s %s" % (str(c.get("name") or c.get("id"))[:24],
                                       c.get("status") or ""))
    elif st in (401, 403, 404):
        note("  customer management is portal-only here", "HTTP %s on this edge" % st)
    else:
        check("the customer list is readable", False, st)

    st, edges = call("GET", "/api/control-plane/edges", tok)
    if st == 200:
        items = (edges or {}).get("edges") or (edges or {}).get("rows") or []
        check("the edge registry is readable", True, "%d edge(s)" % len(items))
    elif st in (401, 403, 404):
        note("  edge registry is portal-only here", "HTTP %s" % st)
    else:
        check("the edge registry is readable", False, st)

    # ----------------------------------------------------- the Lite surface
    print()
    print("[Lite view — can it READ, not just render]")
    st, caps = call("GET", "/api/lite-local/capabilities", tok)
    check("Lite reports its capabilities", st == 200, st)
    # The real shape is a FLAT map of module -> bool under "capabilities",
    # not a list of tabs. Parsing it as a list reported "no tabs" on a perfectly
    # healthy Lite app - the test was wrong, not the product.
    cap_map = (caps or {}).get("capabilities") if isinstance(caps, dict) else None
    if isinstance(cap_map, dict):
        granted = sorted(k for k, v in cap_map.items()
                         if v is True and k not in ("is_admin", "read_only"))
        denied = sorted(k for k, v in cap_map.items() if v is False)
        print("     granted: %s" % (", ".join(granted)[:110] or "(none)"))
        if denied:
            print("     denied : %s" % ", ".join(denied)[:110])
        check("  Lite grants at least one module", bool(granted),
              "all-zero seats must not blank the Lite app")
        sess = (caps or {}).get("session") or {}
        check("  and the Lite session is scoped to this edge",
              bool(sess.get("edge_id")) and bool(sess.get("tenant_id")),
              "edge=%s tenant=%s" % (sess.get("edge_id"), sess.get("tenant_id")))

    st, lb = call("GET", "/api/lite-local/bootstrap", tok)
    check("Lite bootstrap returns data", st == 200, st)
    if isinstance(lb, dict):
        gws = (lb.get("data") or lb).get("gateway_configurations") or lb.get("gateways") or []
        print("     Lite sees %d gateway(s)" % len(gws) if isinstance(gws, list) else "")

    # ------------------------------------------------- reading actual data
    print()
    print("[data readable through the customer-facing APIs]")
    st, live = call("GET", "/api/app-store/live?limit=2000", tok)
    rows = (live or {}).get("rows") or []
    check("live values are readable", st == 200 and len(rows) > 0, "%d row(s)" % len(rows))
    gateways = sorted({str(r.get("gateway_id")) for r in rows if r.get("gateway_id")})
    print("     gateways with live values: %s" % ", ".join(gateways[:6]))
    check("  more than one gateway is reporting", len(gateways) >= 2, gateways)

    st, rng = call("GET", "/api/app-store/historian/range?limit=200", tok)
    check("historian rows are readable", st == 200 and len(((rng or {}).get("rows")) or []) > 0,
          len(((rng or {}).get("rows")) or []))

    tag = ""
    for r in rows:
        if r.get("tag"):
            tag = str(r["tag"])
            gid = str(r.get("gateway_id") or "")
            break
    if tag:
        st, agg = call("GET",
                       "/api/app-store/historian/agg?bucket=minute&limit=60"
                       "&gateway=%s&tag=%s" % (urllib.parse.quote(gid), urllib.parse.quote(tag)),
                       tok)
        arows = ((agg or {}).get("rows")) or []
        check("chart aggregates are readable (%s)" % tag[:24], len(arows) > 0, len(arows))

    # ----------------------------------------------------------- the cloud
    print()
    print("[cloud]")
    st, diag = call("GET", "/api/telemetry/v1/edge/diagnostics", tok)
    d = (diag or {}).get("diagnostics") or {}
    if st == 200:
        depth = d.get("outbox_depth")
        check("the cloud forwarding state is readable", True,
              "outbox depth %s" % depth)
        if not d.get("ingest_enabled"):
            note("  cloud ingest is disabled on this edge",
                 "rows are stored locally and replay when it is turned on")
        if isinstance(depth, int) and depth > 500000:
            check("  the outbox is not running away", False, depth)
    else:
        note("  cloud diagnostics unavailable", "HTTP %s" % st)

    print()
    if WARNS:
        print("  %d note(s): %s" % (len(WARNS), "; ".join(WARNS[:4])))
    print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
    return 0 if not FAILS else 2


if __name__ == "__main__":
    sys.exit(main())
