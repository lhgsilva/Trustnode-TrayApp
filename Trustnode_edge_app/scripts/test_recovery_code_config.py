# -*- coding: utf-8 -*-
"""A customer's edge must be able to ask the cloud for a recovery code.

2026-10-22, reported from a new customer: "Request Code" did nothing and
no email ever arrived.

recovery_codes.send_code() needs a Supabase URL *and* an ANON key, and it
used to read the ENVIRONMENT ONLY. A customer machine has neither: a
fresh install has no .env (an operator installs that by hand), and the
activation code carried the Supabase URL but never a key. So send_code()
returned "supabase_not_configured" on every customer install while the
screen still answered "if the account exists, a code has been sent" - the
operator waited for an email nobody had requested.

Two halves, both checked here:
  * the portal puts supabase_url AND supabase_anon_key into the
    endpoints an activation code carries, taking them from its own
    environment when they are not configured explicitly;
  * the edge falls back to those stored endpoints when the environment
    does not carry them.

The ANON key is public by design - it ships inside every browser bundle
and is protected by row-level security, not secrecy. The SERVICE key
must never travel this way: it bypasses Auth entirely.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

FAKE_URL = "https://unit-test.supabase.co"
FAKE_ANON = "anon-key-for-this-test-only"


class _AppStore:
    """Stands in for the edge's store, holding what activation saved."""

    def __init__(self):
        self.endpoints = {}

    def get_domain_scoped_fast(self, domain):
        if domain != "app_settings":
            return {}
        return {"infrastructure_endpoints": dict(self.endpoints)}


# Registered BEFORE any app import, and before anything can import the real
# one. `import app.state` constructs the real AppStore, which runs
# _ensure_schema and ALTERs whatever database the environment points at -
# that is how a test migrates a production store. CLAUDE.md is absolute:
# never write to the live workspace from a test. Importing
# app.services.recovery_codes pulls app.state in through its own chain, so
# the stub has to be in place first, not just before the call.
import tempfile  # noqa: E402
import types  # noqa: E402

_FAKE_STORE = _AppStore()
_stub = types.ModuleType("app.state")
_stub.app_store = _FAKE_STORE
sys.modules["app.state"] = _stub

# Belt and braces: if some other path still reaches a real store, point it at
# a throwaway directory rather than anything a customer owns.
_scratch = tempfile.mkdtemp(prefix="tn-recovery-cfg-")
os.environ["TRUSTNODE_DATA_DIR"] = _scratch
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_scratch, "s.db")
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"

FAILS = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(
        name, "PASS" if ok else "FAIL", (" - " + str(detail)[:120]) if not ok else ""))
    if not ok:
        FAILS.append(name)


print("A customer edge can ask the cloud for a recovery code")
print()

# ---- half 1: the portal puts both into what the activation code carries
from app.services.control_plane_store import ControlPlaneStore  # noqa: E402

os.environ["TRUSTNODE_SUPABASE_URL"] = FAKE_URL
os.environ["TRUSTNODE_SUPABASE_ANON_KEY"] = FAKE_ANON


class _Store(ControlPlaneStore):
    """Only the two reads resolve_infrastructure_endpoints makes."""

    def __init__(self):  # no database: this method touches nothing else
        pass

    def get_infrastructure_config(self, *, tenant_id=None):
        return {"endpoints": {"cloud_api_url": "https://portal.example"}}


resolved = _Store().resolve_infrastructure_endpoints(tenant_id="tenant-cust-x")
check("the activation endpoints carry the Supabase URL",
      resolved.get("supabase_url") == FAKE_URL, resolved.get("supabase_url"))
check("  and the ANON key, so the edge can reach Auth",
      resolved.get("supabase_anon_key") == FAKE_ANON,
      "without it send_code() returns supabase_not_configured")
check("  the SERVICE key is never shipped",
      not any("service" in str(k).lower() for k in resolved),
      sorted(resolved))
check("  an explicitly configured value still wins",
      _Store().resolve_infrastructure_endpoints(tenant_id=None).get("cloud_api_url")
      == "https://portal.example")

# ---- half 2: the edge reads them when the environment is empty
from app.services import recovery_codes  # noqa: E402

os.environ.pop("TRUSTNODE_SUPABASE_URL", None)
os.environ.pop("TRUSTNODE_SUPABASE_ANON_KEY", None)

url, anon = recovery_codes.supabase_config()
check("with no env and no stored endpoints: not configured",
      (url, anon) == ("", ""), "{0!r} {1!r}".format(url, anon))


_FAKE_STORE.endpoints = {"supabase_url": FAKE_URL, "supabase_anon_key": FAKE_ANON}

url, anon = recovery_codes.supabase_config()
check("THE EDGE USES WHAT ACTIVATION DELIVERED",
      url == FAKE_URL and anon == FAKE_ANON,
      "{0!r} {1!r} - this is what makes Request Code work on a customer PC"
      .format(url, anon))

os.environ["TRUSTNODE_SUPABASE_URL"] = "https://env-wins.supabase.co"
os.environ["TRUSTNODE_SUPABASE_ANON_KEY"] = "env-anon"
url, anon = recovery_codes.supabase_config()
check("  an explicit environment still overrides it",
      url == "https://env-wins.supabase.co" and anon == "env-anon",
      "{0!r} {1!r}".format(url, anon))

check("a placeholder address is still not deliverable",
      not recovery_codes.is_deliverable("admin@trustnode.local"),
      "telling an operator a code is coming when nobody reads that mailbox")
check("  a real one is", recovery_codes.is_deliverable("someone@example.com"))

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
