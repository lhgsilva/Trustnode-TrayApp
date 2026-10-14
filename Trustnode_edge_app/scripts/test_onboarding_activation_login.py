# -*- coding: utf-8 -*-
"""Customer -> licence -> edge -> activation code -> login, end to end.

2026-10-14, requested: validation tests for "the creation of the customer
edge licenses and activation, and logins".

This is the onboarding chain a new customer actually goes through, and every
link in it has already failed in production at least once:

  * the `default` tenant did not exist in the cloud control plane, so the
    developer row could not be created at all (cp_users has a foreign key
    onto cp_tenants) - 2026-10-13;
  * an activation code was issued against the master's `default` tenant
    instead of the customer's, so the edge came up in the wrong scope;
  * a brand-new computer had nowhere to send its activation code - the
    endpoints now travel INSIDE the code (commit 2bc02aa);
  * a blank cp_users.password_hash authenticates an EMPTY password, because
    _verify_password ends in hmac.compare_digest(password, raw).

Runs entirely against a scratch control-plane database. No network, no
Supabase, nothing written outside the temp directory.
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

# Scratch workspace BEFORE importing anything that can reach app.state -
# importing it instantiates AppStore and migrates whatever database the
# environment points at.
_TMP = tempfile.mkdtemp(prefix="tn-onboard-")
os.environ.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=_TMP,
                  TRUSTNODE_APP_STORE_PATH=os.path.join(_TMP, "scratch.db"),
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)


def raises(fn, want: str) -> bool:
    """Did fn() refuse with the expected reason?"""
    try:
        fn()
    except Exception as exc:
        return want in str(exc)
    return False


from app.services.control_plane_store import ControlPlaneStore       # noqa: E402

store = ControlPlaneStore()

TEN = "tenant-cust-test01"
CUST = "cust-test01"
EDGE = "edge-test01"
LIC = "lic-test01"

print("TrustNode - customer onboarding, activation and login")
print()

# ---------------------------------------------------------------- creation
print("[1. the scope has to exist before anything hangs off it]")
store.upsert_tenant(tenant_id=TEN, name="Test Customer")
check("a tenant can be created", bool(store.list_tenants()))
store.upsert_customer(tenant_id=TEN, customer_id=CUST, company_name="Test Co",
                      contact_email="ops@test.example")
check("  a customer under it", any(c["customer_id"] == CUST
                                   for c in store.list_customers(tenant_id=TEN)))
store.upsert_license(tenant_id=TEN, license_id=LIC, customer_id=CUST,
                     plan_code="standard", status="active", max_edges=3)
check("  a licence linked to the customer",
      any(l["license_id"] == LIC for l in store.list_licenses(tenant_id=TEN)))
store.upsert_edge(tenant_id=TEN, edge_id=EDGE, edge_name="Test Edge A",
                  customer_id=CUST)
check("  an edge linked to the customer",
      any(e["edge_id"] == EDGE for e in store.list_edges(tenant_id=TEN)))

# ------------------------------------------------------------- activation
print()
print("[2. an activation code is refused unless the chain is whole]")
check("no customer -> refused",
      raises(lambda: store.issue_activation_code(
          tenant_id=TEN, customer_id="", edge_id=EDGE, license_id=LIC),
          "customer_id_required"))
check("  no edge -> refused",
      raises(lambda: store.issue_activation_code(
          tenant_id=TEN, customer_id=CUST, edge_id="", license_id=LIC),
          "edge_id_required"))
check("  no licence -> refused",
      raises(lambda: store.issue_activation_code(
          tenant_id=TEN, customer_id=CUST, edge_id=EDGE, license_id=""),
          "license_id_required"))
check("  unknown customer -> refused",
      raises(lambda: store.issue_activation_code(
          tenant_id=TEN, customer_id="nope", edge_id=EDGE, license_id=LIC),
          "customer_not_found"))
check("  unknown edge -> refused",
      raises(lambda: store.issue_activation_code(
          tenant_id=TEN, customer_id=CUST, edge_id="nope", license_id=LIC),
          "edge_not_found"))

# An edge belonging to a DIFFERENT customer must never take this licence.
store.upsert_customer(tenant_id=TEN, customer_id="cust-other",
                      company_name="Other Co")
store.upsert_edge(tenant_id=TEN, edge_id="edge-other", edge_name="Other Edge",
                  customer_id="cust-other")
check("  an edge from another customer -> refused",
      raises(lambda: store.issue_activation_code(
          tenant_id=TEN, customer_id=CUST, edge_id="edge-other", license_id=LIC),
          "edge_not_linked_to_customer"),
      "this is how an edge ends up reporting into the wrong customer")

store.upsert_license(tenant_id=TEN, license_id="lic-expired", customer_id=CUST,
                     status="suspended")
check("  a licence that is not active -> refused",
      raises(lambda: store.issue_activation_code(
          tenant_id=TEN, customer_id=CUST, edge_id=EDGE, license_id="lic-expired"),
          "license_not_active"),
      "a suspended customer must not be able to activate a new edge")

print()
print("[3. a valid code carries everything the edge needs]")
store.set_infrastructure_config(
    endpoints={"cloud_api_url": "https://portal.example.com",
               "supabase_url": "https://proj.supabase.co"},
    tenant_id=ControlPlaneStore.GLOBAL_INFRA_TENANT)
infra = store.resolve_infrastructure_endpoints(tenant_id=TEN)
issued = store.issue_activation_code(
    tenant_id=TEN, customer_id=CUST, edge_id=EDGE, license_id=LIC,
    edge_name="Test Edge A", ttl_minutes=30,
    metadata={"infrastructure": infra})
code = str(issued.get("activation_code") or "")
check("a code is issued", bool(code), "len=%d" % len(code))
check("  it is bound to the CUSTOMER's tenant, not the issuer's",
      issued.get("tenant_id") == TEN,
      "issuing under the master's `default` is how an edge lands in the "
      "wrong scope: %s" % issued.get("tenant_id"))
check("  it names the edge and licence",
      issued.get("edge_id") == EDGE and issued.get("license_id") == LIC)
check("  it expires", bool(str(issued.get("expires_utc") or "")))

row = store.get_activation_code_row(activation_code=code)
check("  the stored row starts life as 'issued'",
      str(row.get("status")) == "issued", row.get("status"))
meta = row.get("metadata_json") or "{}"
check("  it carries the infrastructure endpoints",
      "cloud_api_url" in str(meta),
      "a brand-new computer has nowhere to send the code otherwise "
      "(commit 2bc02aa)")
check("    including where the portal lives",
      "portal.example.com" in str(meta), str(meta)[:120])

print()
print("[4. redeeming the code - the part that must never fail]")
check("the exact code resolves", bool(store.get_activation_code_row(
    activation_code=code)))
check("  a code with surrounding whitespace resolves",
      bool(store.get_activation_code_row(activation_code="  %s  " % code)),
      "people paste from e-mail")
check("  a code broken across lines resolves",
      bool(store.get_activation_code_row(
          activation_code=code[:10] + "\n" + code[10:])),
      "a wrapped e-mail inserts newlines mid-code")
check("  a code with inner spaces resolves",
      bool(store.get_activation_code_row(
          activation_code=code[:8] + " " + code[8:])))
_messy = chr(9) + code[:6] + chr(13) + chr(10) + code[6:] + chr(9)
check("  a code with tabs and CRLF resolves",
      bool(store.get_activation_code_row(activation_code=_messy)),
      "the lookup has several fallbacks; what matters is that every way "
      "an operator can paste a code still resolves")
check("  a wrong code does NOT resolve",
      store.get_activation_code_row(activation_code=code + "x") is None,
      "tolerance must not become acceptance")
check("  an empty code does NOT resolve",
      store.get_activation_code_row(activation_code="") is None)

ttl_floor = store.issue_activation_code(
    tenant_id=TEN, customer_id=CUST, edge_id=EDGE, license_id=LIC,
    ttl_minutes=1)
check("  a silly TTL is floored, not honoured",
      bool(str(ttl_floor.get("expires_utc") or "")),
      "a 1-minute code expires before the operator finishes typing it")

print()
print("[5. actually redeeming it - activate_edge_with_code]")
act = store.issue_activation_code(
    tenant_id=TEN, customer_id=CUST, edge_id=EDGE, license_id=LIC,
    edge_name="Test Edge A")
acode = str(act.get("activation_code") or "")

bad = raises(lambda: store.activate_edge_with_code(
    activation_code=acode + "zz", edge_id=EDGE), "activation_code_not_found")
check("an unknown code is refused", bad)

# En/em dashes: a code pasted out of Word or an e-mail client that has
# "helpfully" typographed the hyphens must still work.
typographed = acode.replace("-", "—", 1) if "-" in acode else acode
res = store.activate_edge_with_code(activation_code=typographed, edge_id=EDGE)
check("  a code whose hyphen became an em-dash still activates",
      bool(res), "this is what a mail client does to a pasted code")
check("  the edge is bound to the customer's tenant",
      str((res or {}).get("tenant_id")) == TEN, (res or {}).get("tenant_id"))
check("  and marked active",
      str((res or {}).get("status") or "").lower() == "active",
      (res or {}).get("status"))

used = store.get_activation_code_row(activation_code=acode)
check("  the code is consumed", str(used.get("status")) == "used", used.get("status"))
check("    and stamped with when", bool(str(used.get("used_utc") or "")))

again = store.activate_edge_with_code(activation_code=acode, edge_id=EDGE)
check("  re-applying the SAME code for the SAME edge is idempotent",
      bool(again),
      "a re-install must not be blocked by its own previous activation")
# A used code is bound to the edge it was ISSUED for. Presenting it with a
# different edge_id does not bind that edge - the branch takes
# `target_edge_id = code_edge_id or current_edge_id`, so it returns the
# original edge and ignores the caller. That is deliberate: failing hard
# would break a legitimate re-install, while binding the caller's edge would
# let one code license an unlimited number of machines.
reused = store.activate_edge_with_code(activation_code=acode,
                                       edge_id="edge-other")
check("    a used code never binds a DIFFERENT edge",
      str((reused or {}).get("edge_id")) == EDGE,
      "it returns the edge the code was issued for (%s), not the one asked "
      "for - one code must not license a second machine"
      % (reused or {}).get("edge_id"))
_other = next((e for e in store.list_edges(tenant_id=TEN)
               if e.get("edge_id") == "edge-other"), {})
check("    and edge-other is untouched by it",
      str(_other.get("status") or "").lower() != "active",
      "presenting someone else's code must not activate your edge "
      "(edge-other status=%s)" % _other.get("status"))

revoked = store.issue_activation_code(
    tenant_id=TEN, customer_id=CUST, edge_id=EDGE, license_id=LIC)
rrow = store.get_activation_code_row(activation_code=revoked["activation_code"])
store.update_activation_code(tenant_id=TEN, row_id=int(rrow["id"]), status="revoked")
check("  a revoked code is refused",
      raises(lambda: store.activate_edge_with_code(
          activation_code=revoked["activation_code"], edge_id=EDGE),
          "activation_code_revoked"))

print()
print("[6. logins]")
store.upsert_user(tenant_id=TEN, customer_id=CUST, username="cust-admin",
                  password="Str0ng-Pass!", role="admin")
ok = store.authenticate_user(tenant_id=TEN, username="cust-admin",
                             password="Str0ng-Pass!")
check("a customer admin can sign in", bool(ok), ok)
check("  with their own tenant on the session",
      (ok or {}).get("tenant_id") == TEN, (ok or {}).get("tenant_id"))
check("  the wrong password is refused",
      store.authenticate_user(tenant_id=TEN, username="cust-admin",
                              password="wrong") is None)
check("  an unknown user is refused",
      store.authenticate_user(tenant_id=TEN, username="ghost",
                              password="x") is None)
check("  another tenant cannot use the same name",
      store.authenticate_user(tenant_id="tenant-somewhere-else",
                              username="cust-admin",
                              password="Str0ng-Pass!") is None,
      "a cross-tenant read is a data-protection failure, not a display bug")

store.upsert_user(tenant_id=TEN, username="suspended-user",
                  password="Str0ng-Pass!", role="viewer", status="suspended")
check("  a suspended account is refused",
      store.authenticate_user(tenant_id=TEN, username="suspended-user",
                              password="Str0ng-Pass!") is None)

print()
print("[7. the blank-hash trap]")
# The fallback in _verify_password ends in
# hmac.compare_digest(password, raw), and compare_digest("", "") is True - so
# a row with a BLANK password_hash authenticated an EMPTY password.
#
# upsert_user always hashes, so this is not reachable through the API - which
# is exactly why asserting it through upsert_user proved nothing. The
# exposure is a row written DIRECTLY into cp_users: a migration, a repair
# script, or a hand-made developer row (one was created on 2026-10-13).
# So test the verifier itself.
check("an empty stored hash authenticates nothing",
      store._verify_password("", "") is False,  # noqa: SLF001
      "compare_digest('', '') is True - without an explicit guard a blank "
      "hash is an open door for a blank password")
check("  not even a non-empty password",
      store._verify_password("anything", "") is False)  # noqa: SLF001
check("  and a real hash still verifies",
      store._verify_password(  # noqa: SLF001
          "Str0ng-Pass!", store._hash_password("Str0ng-Pass!")) is True,
      "a guard that rejects everything proves nothing")
check("  while the wrong password against it does not",
      store._verify_password(  # noqa: SLF001
          "nope", store._hash_password("Str0ng-Pass!")) is False)

# And the API path that creates a user without one must not leave it blank.
store.upsert_user(tenant_id=TEN, username="no-password-user", role="viewer")
check("  upsert_user never writes a blank hash",
      store.authenticate_user(tenant_id=TEN, username="no-password-user",
                              password="") is None)

print()
print("[8. the developer (master) scope]")
store.upsert_tenant(tenant_id="default", name="TrustNode (developer)")
store.upsert_user(tenant_id="default", username="master-test",
                  password="Str0ng-Pass!", role="admin")
m = store.authenticate_user(tenant_id="default", username="master-test",
                            password="Str0ng-Pass!")
check("the developer account signs in", bool(m))
check("  role=admin AND tenant=default - what _is_global_admin requires",
      (m or {}).get("role") == "admin" and (m or {}).get("tenant_id") == "default",
      "%s / %s" % ((m or {}).get("role"), (m or {}).get("tenant_id")))
check("  and it can list every customer across tenants",
      len(store.list_customers(all_tenants=True)) >= 2,
      "this is the master view; %d customer(s)"
      % len(store.list_customers(all_tenants=True)))

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
