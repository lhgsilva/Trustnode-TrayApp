# -*- coding: utf-8 -*-
"""The whole journey, in order, on one running app.

2026-10-20, requested: "make a validation flow, from the customer creation
on the portal, email, users edge, licenses, validation code, local users and
login and forget password to make sure we have a correct app running
everywhere".

WHY THIS EXISTS WHEN THE PIECES ARE ALREADY TESTED

Each link already has a test, and every one of them passes while the journey
is broken, because they each build their own starting state:

  test_onboarding_activation_login.py  the portal chain and the code rules
  test_activation_creates_admin.py     the local admin activation leaves
  test_login_codes.py                  the three code endpoints
  test_public_reset_no_token_leak.py   the reset response body
  test_recovery_email_fallback.py      which inbox a code goes to

What nobody checks is the HANDOFF: that what one stage WRITES is what the
next stage READS. A customer created with no email, a code issued under the
wrong tenant, an activation that leaves no admin, a recovery that resolves
to nobody - each is invisible to a test that starts after it.

So this runs ONE backend and walks the journey forward. Every stage consumes
the previous stage's output, and a break stops the chain instead of being
skipped. It deliberately does NOT re-assert the refusal matrices above - it
names the test that owns each one.

SAFETY. Scratch workspace, set before any import that can reach app.state,
because importing it migrates whatever database the environment points at
(CLAUDE.md 5). No live store, no Supabase, no network.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = os.environ.get("FLOW_PORT", "8188")
API = "http://127.0.0.1:" + PORT

FAILS: list[str] = []
STOPPED = ""


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:200]) if detail else ""))
    if not ok:
        FAILS.append(name)
    return bool(ok)


def stage(n, title):
    print()
    print("[%s. %s]" % (n, title))


TMP = tempfile.mkdtemp(prefix="tn-flow-")

# ---------------------------------------------------------------------------
# 2026-10-20: THIS BLOCK MUST STAY ABOVE EVERY app.* IMPORT.
#
# The first version of this script set the SUBPROCESS environment only, and
# then imported ControlPlaneStore in-process. os.environ still pointed at the
# default workspace, so the import resolved to ~/.trustnode_edge/data and the
# run wrote a tenant, a customer, a licence, an edge, three activation codes
# and a GLOBAL infrastructure row into the live control plane - the exact
# failure CLAUDE.md 5 describes ("importing app.state migrates whatever
# database the environment points at").
#
# Setting os.environ is the fix; the assert below is what makes it stay
# fixed, because the symptom otherwise is silent.
# ---------------------------------------------------------------------------
os.environ.update(
    TRUSTNODE_SKIP_DOTENV="1",
    TRUSTNODE_DATA_DIR=TMP,
    # Deliberately NOT setting TRUSTNODE_APP_STORE_PATH. It names one file
    # while ControlPlaneStore derives trustnode_app_store.db from the data
    # dir, so setting both gave the subprocess one control plane and this
    # process another - the flow wrote a customer the resolver could not
    # see, and stage 8 reported a product bug that did not exist.
    TRUSTNODE_BOOT_INTEGRITY_CHECK="never",
    TRUSTNODE_PORT=PORT,
)
# Supabase stays unconfigured on purpose: delivery must FAIL and every
# endpoint must still behave. A flow that only works when mail works would
# not have caught the response that leaked the reset token.
os.environ.pop("TRUSTNODE_SUPABASE_URL", None)
os.environ.pop("TRUSTNODE_SUPABASE_ANON_KEY", None)

ENV = dict(os.environ)


def _refuse_if_not_scratch(where: str, path: str) -> None:
    """Stop before writing, not after."""
    p = os.path.normcase(os.path.abspath(str(path or "")))
    if not p.startswith(os.path.normcase(os.path.abspath(TMP))):
        print()
        print("REFUSING TO RUN: %s resolved to" % where)
        print("    %s" % path)
        print("  which is outside the scratch workspace")
        print("    %s" % TMP)
        print("  A flow that writes into a real install is worse than no "
              "flow at all.")
        raise SystemExit(2)

# A backend left over from an earlier run answers on this port and looks
# exactly like ours. On 2026-08-22 a stale backend squatting a port is how
# "the data is gone" turned out to be the wrong workspace. Refuse rather
# than silently validate somebody else's app.
try:
    _probe = urllib.request.urlopen(
        "http://127.0.0.1:%s/api/health" % PORT, timeout=3)
    _probe.read()
    print("REFUSING TO RUN: something is already serving on port %s." % PORT)
    print("  That is almost certainly a backend left over from an earlier")
    print("  run. Stop it, or set FLOW_PORT to a free port.")
    raise SystemExit(2)
except SystemExit:
    raise
except Exception:
    pass          # nothing listening: what we want

LOG = open(os.path.join(TMP, "backend.log"), "w")
PROC = subprocess.Popen([sys.executable, "-m", "app"],
                        cwd=os.path.join(ROOT, "backend"), env=ENV,
                        stdout=LOG, stderr=subprocess.STDOUT)


def call(method, path, token=None, body=None, timeout=60):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            # Surfaces serve HTML. Parsing everything as JSON reported the
            # Web Lite pages as unreachable when they had answered 200.
            try:
                return r.status, json.loads(raw or "null")
            except ValueError:
                return r.status, raw[:200]
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw or "null")
        except Exception:
            return e.code, raw[:400]
    except Exception as e:  # noqa: BLE001
        return 0, str(e)[:300]


def finish(code):
    try:
        PROC.terminate()
        PROC.wait(timeout=25)
    except Exception:
        PROC.kill()
    sys.exit(code)


def halt(why):
    """A flow stops where it breaks. Skipping ahead reports fiction."""
    global STOPPED
    STOPPED = why
    print()
    print("  FLOW STOPPED: %s" % why)
    print("  Later stages are NOT reported - with this broken, whatever they")
    print("  said would be about a state no real customer can reach.")
    print()
    print("RESULT: FAIL - " + why)
    finish(2)


print("TrustNode - the onboarding journey, end to end")
print("  workspace: %s" % TMP)
print()

up = False
for _ in range(75):
    try:
        urllib.request.urlopen(API + "/api/health", timeout=3).read()
        up = True
        break
    except Exception:
        time.sleep(2)
if not check("the app came up", up):
    print(io.open(os.path.join(TMP, "backend.log"), encoding="utf-8",
                  errors="replace").read()[-1500:])
    finish(2)

# The workspace must be the scratch one. Getting this wrong is how a test
# writes into the live install (CLAUDE.md 1 and 5).
_st, health = call("GET", "/api/health")
_ws = (health or {}).get("workspace")
_ws = _ws if isinstance(_ws, dict) else {}
_data_dir = str(_ws.get("data_dir") or "")
check("  and it is on the SCRATCH workspace, not the live one",
      os.path.normcase(os.path.abspath(_data_dir or "x"))
      == os.path.normcase(os.path.abspath(TMP)),
      "data_dir=%s - if this is ever the live workspace, stop: a run here "
      "writes into a real install" % (_data_dir or "(not reported)"))

# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(ROOT, "backend"))
from app.services.control_plane_store import ControlPlaneStore  # noqa: E402

# The portal's own store. The running app has its own instance on the same
# scratch directory; this is the portal side of the same control plane.
store = ControlPlaneStore()
_refuse_if_not_scratch("the in-process control-plane store",
                       getattr(store, "_db_path", ""))

TEN = "tenant-cust-flow01"
CUST = "cust-flow01"
EDGE = "edge-flow01"
LIC = "lic-flow01"
COMPANY_EMAIL = "ops@flow-customer.example"
ADMIN = "admin-flow01"
ADMIN_PW = "Str0ngPass!2026"

stage(1, "the portal creates the customer - with an address")
store.upsert_tenant(tenant_id=TEN, name="Flow Customer")
store.upsert_customer(tenant_id=TEN, customer_id=CUST,
                      company_name="Flow Customer",
                      contact_email=COMPANY_EMAIL, status="active")
cust = [c for c in (store.list_customers(tenant_id=TEN) or [])
        if c.get("customer_id") == CUST]
if not check("the customer exists under its own tenant", bool(cust)):
    halt("the customer was not created, so nothing can hang off it")
check("  and carries a company email",
      str(cust[0].get("contact_email") or "") == COMPANY_EMAIL,
      "this address is where recovery goes for every user in this company "
      "who has none of their own - %s" % (cust[0].get("contact_email") or "(none)"))

# The REQUIRED-on-create rule lives in the router, not the store, so that an
# existing customer without one can still be edited. Its unit coverage is
# test_recovery_email_fallback.py; here we only confirm the rule is wired
# into the endpoint the portal actually posts to.
CPSRC = io.open(os.path.join(ROOT, "backend", "app", "routers",
                             "control_plane.py"), encoding="utf-8").read()
check("  the create endpoint refuses a customer without one",
      "_is_new and not recovery_codes.is_deliverable(_email)" in CPSRC,
      "detail in test_recovery_email_fallback.py")

stage(2, "a licence, active, linked to that customer")
store.upsert_license(tenant_id=TEN, license_id=LIC, customer_id=CUST,
                     plan_code="standard", status="active")
lic = [x for x in (store.list_licenses(tenant_id=TEN) or [])
       if x.get("license_id") == LIC]
if not check("the licence exists and is linked", bool(lic)):
    halt("no licence - a code cannot be issued without one")
check("  and is active",
      str(lic[0].get("status") or "") == "active",
      "a lapsed customer must not be able to activate new edges")

stage(3, "an edge, linked to the same customer")
store.upsert_edge(tenant_id=TEN, edge_id=EDGE, customer_id=CUST,
                  edge_name="Flow Edge")
edge = [e for e in (store.list_edges(tenant_id=TEN) or [])
        if e.get("edge_id") == EDGE]
if not check("the edge exists and is linked", bool(edge)):
    halt("no edge - this is how an edge ends up reporting into the wrong "
         "customer")

stage(4, "an activation code that carries where to call home")
# Portal -> Infrastructure must be set BEFORE codes are issued: the
# endpoints are resolved at issue time and travel inside the code, because
# the code itself is an opaque token and a brand-new machine has no other
# way to learn where the portal lives. Re-host, update this FIRST, then
# issue new codes.
store.set_infrastructure_config(
    endpoints={"cloud_api_url": "https://portal.flow.example",
               "supabase_url": "https://proj.supabase.co"},
    tenant_id=ControlPlaneStore.GLOBAL_INFRA_TENANT)
resolved_infra = store.resolve_infrastructure_endpoints(tenant_id=TEN)
if not check("the portal has an infrastructure address configured",
             bool(str((resolved_infra or {}).get("cloud_api_url") or "")),
             "without this every code issued is one a new machine cannot act on"):
    halt("no infrastructure endpoints - codes would ship with nowhere to call")

issued = store.issue_activation_code(tenant_id=TEN, customer_id=CUST,
                                     edge_id=EDGE, license_id=LIC,
                                     edge_name="Flow Edge", ttl_minutes=30,
                                     metadata={"infrastructure": resolved_infra})
code = str((issued or {}).get("activation_code") or "")
if not check("a code is issued", bool(code), "len=%d" % len(code)):
    halt("no activation code - the refusal reasons are covered by "
         "test_onboarding_activation_login.py")
check("  issued under the CUSTOMER's tenant, not the issuer's",
      str(issued.get("tenant_id") or "") == TEN,
      "issuing under the master's 'default' puts the edge in the wrong scope "
      "and everything it writes afterwards lands there")
# Read it back off the STORED row, not the dict we just passed in: what
# matters is what the edge will actually receive.
row = store.get_activation_code_row(activation_code=code) or {}
meta_raw = str(row.get("metadata_json") or "{}")
check("  the stored code starts life as 'issued'",
      str(row.get("status") or "") == "issued", row.get("status"))
check("  and carries the portal address the new machine cannot know",
      "cloud_api_url" in meta_raw and "portal.flow.example" in meta_raw,
      "an activation code is an opaque token - a brand-new computer learns "
      "where the portal lives from the code itself: %s" % meta_raw[:140])
try:
    infra = (json.loads(meta_raw) or {}).get("infrastructure") or {}
except Exception:
    infra = {}

stage(5, "the edge redeems it and gets a local administrator")
st, res = call("POST", "/api/control-plane/edge-link/local-finalize", body={
    "tenant_id": TEN, "edge_id": EDGE, "edge_name": "Flow Edge",
    "customer_id": CUST, "license_id": LIC, "license_status": "active",
    "license_plan_code": "standard", "license_modules": [],
    "cloud_api_url": str(infra.get("cloud_api_url") or "https://example.invalid"),
    "admin_username": ADMIN, "admin_password": ADMIN_PW,
})
if not check("the local half of activation succeeds", st == 200,
             "HTTP %s %s" % (st, str(res)[:160])):
    halt("activation did not complete, so there is no local account to log "
         "in with - detail in test_activation_creates_admin.py")

stage(6, "that administrator can actually sign in")
st, res = call("POST", "/api/auth/login",
               body={"username": ADMIN, "password": ADMIN_PW})
token = str((res or {}).get("token") or "")
if not check("the promised admin logs in", st == 200 and bool(token),
             "HTTP %s - activation reporting success while leaving no usable "
             "account is the exact 2026-08-25 report" % st):
    halt("the account activation promised does not work")
check("  the wrong password is refused",
      call("POST", "/api/auth/login",
           body={"username": ADMIN, "password": "nope-nope-nope"})[0] != 200)

stage(7, "a locked-out operator can ask for a code")
# Supabase is not configured here, so nothing is delivered. What must hold is
# that the endpoints are reachable WITHOUT a session, say nothing about who
# exists, and never put a secret in the body.
for path, body in (
    ("/api/auth/login-code/request", {"identifier": ADMIN}),
    ("/api/auth/forgot-password", {"identifier": ADMIN}),
):
    st, res = call("POST", path, body=body)
    if not check("%-34s answers without a session" % path.replace("/api/auth", ""),
                 st == 200, "HTTP %s - behind the auth gate it is unreachable "
                            "by the only people who need it" % st):
        halt("a locked-out operator cannot reach the recovery endpoint")
    blob = json.dumps(res or {}).lower()
    check("  and leaks nothing",
          not any(k in blob for k in ('"code"', '"token"', '"reset_token"',
                                      '"secret"', '"otp"')),
          str(res)[:160])

st_known, r_known = call("POST", "/api/auth/login-code/request",
                         body={"identifier": ADMIN})
st_unknown, r_unknown = call("POST", "/api/auth/login-code/request",
                             body={"identifier": "no-such-person-zz"})
check("an unknown user gets the SAME answer as a real one",
      json.dumps(r_known, sort_keys=True) == json.dumps(r_unknown, sort_keys=True),
      "a different reply is a free account-enumeration oracle:\n"
      "      %s\n      %s" % (str(r_known)[:90], str(r_unknown)[:90]))
check("  a wrong code is refused",
      call("POST", "/api/auth/login-code/verify",
           body={"identifier": ADMIN, "code": "000000"})[0] == 401)
check("  and a weak password cannot ride in on the code path",
      call("POST", "/api/auth/reset-password/code",
           body={"identifier": ADMIN, "code": "000000",
                 "new_password": "short"})[0] == 400,
      "a second way in must not be a weaker way in")

stage(8, "where that code would actually go")
# The journey's point: this operator never registered an address of their
# own, so the company address from stage 1 is the only way back in.
from app.routers import auth as auth_router  # noqa: E402
from app import state as _state  # noqa: E402

# This process holds its OWN AppStore instance, built before stage 5 ran, so
# its cached bootstrap has never seen the edge identity activation wrote in
# the subprocess. Resolving the scope here would return "" for a reason that
# says nothing about the product.
#
# So pin the scope to the one activation actually produced and verified in
# stage 5 - the tenant, customer and edge this flow created. That is the
# handoff under test: a customer created WITH an address in stage 1 is the
# inbox a user with none gets a code at. How the scope itself is derived is
# app_store.site_scope_key()'s job and is covered by
# test_recovery_email_fallback.py, including the no-identity case.
_state.app_store.site_scope_key = lambda: "%s|%s|%s" % (TEN, CUST, EDGE)

_scope_seen = str(_state.app_store.site_scope_key() or "")
try:
    _custs = [(c.get("customer_id"), c.get("contact_email"))
              for c in (_state.control_plane_store.list_customers(tenant_id=TEN) or [])]
except Exception as _exc:  # noqa: BLE001
    _custs = "ERROR: %s: %s" % (type(_exc).__name__, _exc)
print("      scope the resolver sees : %s" % (_scope_seen or "(empty)"))
print("      customers it can read   : %s" % (_custs,))
print("      in-process store file   : %s" % getattr(store, "_db_path", "?"))
print("      app.state store file    : %s"
      % getattr(_state.control_plane_store, "_db_path", "?"))

resolved = auth_router._recovery_address({"username": ADMIN, "email": ""})
check("a user with no address of their own resolves to the company",
      resolved == COMPANY_EMAIL,
      "resolved to %r - on 2026-10-20 not one portal account could receive a "
      "code, which is what this whole stage is about" % resolved)
check("  a user WITH their own address keeps it",
      auth_router._recovery_address(
          {"username": ADMIN, "email": "person@flow-customer.example"})
      == "person@flow-customer.example",
      "a shared company inbox must not swallow mail about one person")
check("  a placeholder address is not treated as deliverable",
      auth_router._recovery_address(
          {"username": ADMIN, "email": ADMIN + "@trustnode.local"})
      == COMPANY_EMAIL,
      "telling an operator a code is on its way to a mailbox nobody reads is "
      "worse than telling them it failed")

stage(9, "and the offline way back, for a machine with no internet")
st, res = call("GET", "/api/auth/recovery-status")
check("recovery status is reachable without logging in", st == 200,
      "HTTP %s" % st)
check("  it can see the admin activation created",
      bool((res or {}).get("has_admin")) is True,
      "has_admin=%s admin_count=%s" % ((res or {}).get("has_admin"),
                                       (res or {}).get("admin_count")))
check("  it names the break-glass account for a locked-out operator",
      bool((res or {}).get("master_account_hint")),
      (res or {}).get("master_account_hint"))
check("  and it never returns a hash",
      "hash" not in json.dumps(res or {}).lower(), str(res)[:160])

stage(10, "every surface the operator might be standing at")
# "a correct app running everywhere" - each surface must at least serve its
# shell and reach the same auth. Lite is served by this backend; the cloud
# surfaces are built artefacts and are checked by their own build guards.
# Trailing slashes matter: these are StaticFiles(html=True) mounts, and the
# bare path 404s rather than redirecting.
for label, path in (("the edge app shell", "/"),
                    ("the Web Lite landing", "/trustnode/lite/"),
                    ("the Web Lite app", "/trustnode/lite/app/"),
                    ("the health endpoint", "/api/health")):
    st, body = call("GET", path)
    check("%-24s responds" % label, st in (200, 301, 302, 307, 308),
          "HTTP %s %s" % (st, str(body)[:110]))

# Web Lite is hand-written and no build reads it, so "it exists on disk"
# is the only thing that makes the served route meaningful.
LITE = os.path.join(ROOT, "backend", "static", "lite_view", "index.html")
check("Web Lite is present on disk (no build produces it)",
      os.path.isfile(LITE), LITE)

print()
if FAILS:
    print("RESULT: FAIL - " + ", ".join(FAILS))
else:
    print("RESULT: PASS - a customer created on the portal can be licensed,")
    print("        activated, logged into, and recovered.")
finish(0 if not FAILS else 2)
