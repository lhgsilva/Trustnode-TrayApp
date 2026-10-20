# -*- coding: utf-8 -*-
"""A user with no address of their own is recoverable via their company.

2026-10-20, requested: "every users locally or in the cloud admin for the
company or not need a email for future recovey. the company when created in
the portal need a email when users doenst have email registerd the recovery
should go the the company email registered."

This is not a nicety. Measured on the portal the day the code feature
shipped, NOT ONE account could receive a code:

    default         master        master@trustnode.local   placeholder
    ...a0d29680     admin         (none)                   no address
    ...b47b1b83     admin-lucas   (none)                   no address
    ...e5916328     admin-mari    admin@trustnode.local    placeholder
    ...e5916328     admin-newpc   (none)                   no address

So "request a code" answered "a code has been sent" for five out of five
accounts for which no code could ever arrive. Two things fix that: the
company address becomes the fallback, and a NEW customer cannot be created
without one.

What this pins, and why each line is here:

  * The user's OWN address still wins. The company address is a fallback,
    never a redirect - mail about one person's account should not land in a
    shared inbox when they have their own.
  * A placeholder is not an address. <user>@trustnode.local exists so the row
    has something in it; falling back past it is the whole point.
  * The lookup stays TENANT SCOPED. A recovery request in one customer
    reaching another customer's inbox is a data-protection failure, not a
    display bug (CLAUDE.md 1b).
  * Creating a customer without a deliverable address is refused - but
    EDITING an existing one is not, or every customer already on the live
    portal becomes unsaveable.
  * The edge does the same thing through app_store.site_scope_key() rather
    than a second hand-rolled copy of the scope rule.

Runs in-process on a scratch workspace. Importing app.state migrates whatever
database the environment points at, so the environment is set BEFORE the
import, not after (CLAUDE.md 5).
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="tn-recmail-")
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "s.db")
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ.pop("TRUSTNODE_SUPABASE_URL", None)
os.environ.pop("TRUSTNODE_SUPABASE_ANON_KEY", None)

sys.path.insert(0, os.path.join(ROOT, "backend"))

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("TrustNode - recovery reaches the company when the user has no address")
print()

import io  # noqa: E402

from app.routers import auth as auth_router          # noqa: E402
from app.routers import control_plane as cp_router   # noqa: E402

# Confirm the scratch workspace really took, before anything else runs.
check("the test is NOT pointed at the live workspace",
      bool(_TMP) and ".trustnode_edge" not in _TMP.lower(), _TMP)


class FakeCP:
    """Only what these two helpers call, and tenant-scoped like the real one."""

    def __init__(self, users, customers):
        self._users, self._customers = users, customers

    def list_users(self, *, tenant_id=None, **_kw):
        return [u for u in self._users
                if not tenant_id or u.get("tenant_id") == tenant_id]

    def list_customers(self, *, tenant_id=None, **_kw):
        return [c for c in self._customers
                if not tenant_id or c.get("tenant_id") == tenant_id]


USERS = [
    {"tenant_id": "t1", "username": "has-own", "email": "real@example.com",
     "customer_id": "c1"},
    {"tenant_id": "t1", "username": "placeholder",
     "email": "placeholder@trustnode.local", "customer_id": "c1"},
    {"tenant_id": "t1", "username": "no-email", "email": "", "customer_id": "c1"},
    {"tenant_id": "t1", "username": "orphan", "email": "", "customer_id": "c-gone"},
    {"tenant_id": "t2", "username": "other-tenant", "email": "",
     "customer_id": "c1"},
]
CUSTOMERS = [
    {"tenant_id": "t1", "customer_id": "c1", "contact_email": "ops@acme.example"},
    {"tenant_id": "t2", "customer_id": "c1", "contact_email": "ops@other.example"},
]

_real_cp = cp_router.control_plane_store
cp_router.control_plane_store = FakeCP(USERS, CUSTOMERS)

print()
print("[1. the portal picks the right inbox]")
check("a user with their own address keeps it",
      cp_router._registered_email("t1", "has-own") == "real@example.com",
      "a company fallback must never override a real personal address")
check("  a user with no address falls back to the company",
      cp_router._registered_email("t1", "no-email") == "ops@acme.example",
      "this is the request: recovery should go to the company email")
check("  a trustnode.local placeholder also falls back",
      cp_router._registered_email("t1", "placeholder") == "ops@acme.example",
      "the placeholder is not a mailbox; treating it as one told five "
      "operators a code was on its way to nowhere")
check("  a user whose customer has no address gets nothing",
      cp_router._registered_email("t1", "orphan") == "",
      "better to fail than to pick an arbitrary other customer inbox")
check("  an unknown username gets nothing",
      cp_router._registered_email("t1", "nobody-at-all") == "")
check("  a blank username gets nothing",
      cp_router._registered_email("t1", "") == "")

print()
print("[2. it never crosses a tenant]")
# "other-tenant" sits in t2 under a customer id that ALSO exists in t1 with a
# different address. Asking t1 for them must not find them at all, and asking
# t2 must return t2's own company address.
check("a t2 user is invisible when asking t1",
      cp_router._registered_email("t1", "other-tenant") == "",
      "a shared customer_id across tenants is normal; resolving a username "
      "globally would leak one customer recovery into another inbox")
check("  and t2 resolves to its own company address",
      cp_router._registered_email("t2", "other-tenant") == "ops@other.example")

print()
print("[3. a new customer needs an address; an existing one still saves]")
check("an existing customer is recognised as existing",
      cp_router._existing_customer("t1", "c1") is True)
check("  an unseen customer id is NEW",
      cp_router._existing_customer("t1", "c-brand-new") is False)
check("  a blank customer id is NEW",
      cp_router._existing_customer("t1", "") is False)


class BoomCP(FakeCP):
    def list_customers(self, **_kw):
        raise RuntimeError("store unreachable")


cp_router.control_plane_store = BoomCP(USERS, CUSTOMERS)
check("  a store it cannot read counts as an EDIT, never a block",
      cp_router._existing_customer("t1", "c1") is True,
      "failing closed here would make every customer unsaveable the moment "
      "the control-plane read hiccups")
check("  and the email lookup degrades to empty, not an exception",
      cp_router._registered_email("t1", "no-email") == "")
cp_router.control_plane_store = FakeCP(USERS, CUSTOMERS)

print()
print("[4. the create path actually enforces it]")
SRC = io.open(os.path.join(ROOT, "backend", "app", "routers",
                           "control_plane.py"), encoding="utf-8").read()
check("create is refused without a deliverable address",
      "_is_new and not recovery_codes.is_deliverable(_email)" in SRC,
      "the check must be is_deliverable, not merely non-empty - a "
      "trustnode.local address is non-empty and unreachable")
check("  the refusal says WHY, so the operator can act on it",
      "where account" in SRC and "recovery goes" in SRC,
      "contact_email required tells nobody why a contact address is "
      "suddenly mandatory")
check("  and it applies on CREATE only",
      "_is_new = not _existing_customer(" in SRC,
      "applying it to edits would block saving every customer that already "
      "exists on the live portal without one")

print()
print("[5. the edge behaves the same way]")
_real_find = auth_router.auth_store.find_user_by_email_or_username
_real_state_cp = sys.modules["app.state"].control_plane_store
_real_scope = sys.modules["app.state"].app_store.site_scope_key

EDGE_USERS = {
    "edge-own": {"username": "edge-own", "email": "me@example.com"},
    "edge-none": {"username": "edge-none", "email": ""},
    "edge-fake": {"username": "edge-fake", "email": "edge-fake@trustnode.local"},
}
auth_router.auth_store.find_user_by_email_or_username = (
    lambda ident: EDGE_USERS.get(str(ident or "")))
sys.modules["app.state"].control_plane_store = FakeCP(USERS, CUSTOMERS)
# tenant|customer|edge, which is what site_scope_key() returns.
sys.modules["app.state"].app_store.site_scope_key = lambda: "t1|c1|edge-abc"

_u, mail = auth_router._lookup_user_email("edge-own")
check("an edge user with an address keeps it", mail == "me@example.com")
_u, mail = auth_router._lookup_user_email("edge-none")
check("  one with none falls back to the company", mail == "ops@acme.example",
      "a local operator on a factory LAN is exactly who has no address of "
      "their own")
_u, mail = auth_router._lookup_user_email("edge-fake")
check("  a placeholder falls back too", mail == "ops@acme.example")
_u, mail = auth_router._lookup_user_email("no-such-edge-user")
check("  an unknown user resolves to nothing", _u is None and mail == "")

# An edge with NO identity has no company to fall back to, and must not
# borrow one. This is the unscoped case CLAUDE.md 1b is about.
sys.modules["app.state"].app_store.site_scope_key = lambda: ""
_u, mail = auth_router._lookup_user_email("edge-none")
check("  an edge with no identity falls back to NOBODY", mail == "",
      "with no scope there is no this-edge company; picking the first "
      "customer in the file would mail another customer inbox")

# A customer-less edge is written as tenant|-|edge, and the "-" is a
# literal, not a customer id.
sys.modules["app.state"].app_store.site_scope_key = lambda: "t1|-|edge-abc"
_u, mail = auth_router._lookup_user_email("edge-none")
check("  a customer-less edge resolves within its tenant",
      mail == "ops@acme.example",
      "the literal - must not be matched against a customer_id")

# /forgot-password used to read u["email"] straight off the row, so it
# skipped the fallback entirely: a user with no address got neither a code
# nor a link. Both paths must resolve through the SAME helper.
_u, mail = auth_router._lookup_user_email("edge-none")
check("  forgot-password resolves through the same helper",
      auth_router._recovery_address({"username": "x", "email": ""})
      == "ops@acme.example",
      "a second copy of this rule is how the gap appeared in the first place")
check("  and it still prefers a real personal address",
      auth_router._recovery_address(
          {"username": "x", "email": "me@example.com"}) == "me@example.com")

AUTHSRC = io.open(os.path.join(ROOT, "backend", "app", "routers", "auth.py"),
                  encoding="utf-8").read()
check("  /forgot-password no longer reads the row directly",
      'email = _recovery_address(u)' in AUTHSRC
      and 'email = str(u.get("email") or "")' not in AUTHSRC,
      "reading the row skips the company fallback")
check("  the edge uses the canonical scope key, not its own copy",
      "site_scope_key()" in AUTHSRC,
      "a second copy of the tenant|customer|edge rule read the wrong "
      "document once already")

auth_router.auth_store.find_user_by_email_or_username = _real_find
sys.modules["app.state"].control_plane_store = _real_state_cp
sys.modules["app.state"].app_store.site_scope_key = _real_scope
cp_router.control_plane_store = _real_cp

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
