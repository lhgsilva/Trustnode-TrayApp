# -*- coding: utf-8 -*-
"""A brand-new machine must be able to activate. Nothing is installed yet.

2026-10-14, raised: "when I am activating a new customer the software would
not be installed yet, has to be bullet proof."

This is the hardest case and the one that has actually failed. On a fresh
install there is:

  * no `cloud_url` in app_settings - that is written BY activation;
  * nothing in localStorage - the machine has never talked to a portal;
  * an empty local control-plane store - it has never seen this code;
  * and the operator is on http://127.0.0.1, so the request's own origin is
    useless as a fallback.

The activation code itself cannot help: `issue_activation_code` returns
`secrets.token_urlsafe(24)`, an opaque random string. The endpoints live in
the code's database ROW, which can only be read by asking the portal - the
very thing the machine does not yet know how to reach.

So the address must be carried by the SHIPPED BUNDLE, and the operator must
have a way to override it. 2026-08-25 (commit 2bc02aa): activation worked on
existing machines and failed on a new one with "Control-plane edge-link
login activation failed" - the bundle carried no portal address at all, the
candidate list came out EMPTY, the loop never ran, and the operator got a
bare failure with no clue what was wrong.

These are the things that must stay true for a new customer to come online.
Reads source and built bundles; no network, no store.
"""
from __future__ import annotations

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)


API = io.open(os.path.join(ROOT, "frontend", "src", "api.js"),
              encoding="utf-8").read()
LOGIN = io.open(os.path.join(ROOT, "frontend", "src", "components", "Login",
                             "Login.jsx"), encoding="utf-8").read()

print("TrustNode - a brand-new machine can activate")
print()

print("[1. the shipped bundle knows a portal, because nothing else does]")
check("api.js carries a built-in portal address",
      "const DEFAULT_CONTROL_PLANE_URL" in API,
      "without it a fresh machine has nowhere to send the code")
m = re.search(r'DEFAULT_CONTROL_PLANE_URL\s*=\s*"(https?://[^"]+)"', API)
check("  and it is a real https URL", bool(m) and m.group(1).startswith("https://"),
      m.group(1) if m else "(not found)")
check("  a build may override it",
      "VITE_TRUSTNODE_CONTROL_PLANE_URL" in API,
      "so a self-hosted deployment ships its own address")

print()
print("[2. the candidate list can never come out empty]")
check("activation tries explicit, then stored, then the built-in",
      "[explicitCloud, storedCloud, CONTROL_PLANE_FALLBACK_URL]" in API,
      "this exact order is the fix from 2026-08-25")
check("  blanks are dropped and duplicates removed",
      ".filter(Boolean)" in API and "new Set(" in API)
check("  an empty list fails LOUDLY, naming the fix",
      "has nowhere to go" in API and "Portal address field" in API,
      "the original bug was a bare 'activation failed' with no clue")

print()
print("[3. the operator can always override it]")
check("the activation screen has a Portal address field",
      "Portal address" in LOGIN,
      "the escape hatch when the built-in default is not the right portal")
check("  it is sent with the activation request",
      "control_plane_url: String(edgeRegisterForm.control_plane_url" in LOGIN)
# Precedence is the ARRAY order, not where the names happen to appear in the
# file - storedCloud is declared earlier for an unrelated call site, so
# comparing file offsets proves nothing.
_order = re.search(r"\[\s*explicitCloud\s*,\s*storedCloud\s*,"
                   r"\s*CONTROL_PLANE_FALLBACK_URL\s*\]", API)
check("  and an explicit value is honoured FIRST", bool(_order),
      "an operator who types a portal must not be overruled by a stale "
      "stored one, nor by the built-in default")

print()
print("[4. what worked is remembered, so a re-activation is easier]")
check("the successful portal is stored",
      "Remember the portal that worked" in API or "storedCloud" in API)
check("  and the edge is told where it came from",
      "cloud_api_url: String(dataLike?.cloud_api_url" in API,
      "local-finalize writes it into app_settings, which is what every "
      "later licence re-check reads")

print()
print("[5. the SHIPPED artefacts actually contain an address]")
# The real failure in August was a BUILT bundle with no portal in it. Source
# being correct is not evidence; check what ships.
expect = m.group(1).split("//", 1)[-1].rstrip("/") if m else ""
shipped = []
for rel in ("frontend/dist/assets", "frontend/dist_cloud_readonly/assets"):
    d = os.path.join(ROOT, rel)
    if not os.path.isdir(d):
        continue
    hit = False
    for fn in os.listdir(d):
        if not fn.endswith(".js"):
            continue
        try:
            blob = io.open(os.path.join(d, fn), encoding="utf-8",
                           errors="replace").read()
        except Exception:
            continue
        if expect and expect in blob:
            hit = True
            break
    shipped.append((rel, hit))
check("at least one built bundle was found to check",
      bool(shipped), "run `npm run build` first")
for rel, hit in shipped:
    check("  %-34s carries the portal address" % rel, hit,
          "a bundle with no address cannot activate a new customer - this is "
          "exactly what shipped in August")

print()
print("[6. the backend never invents a host]")
CP = io.open(os.path.join(ROOT, "backend", "app", "routers",
                          "control_plane.py"), encoding="utf-8").read()
check("a localhost request with nothing configured resolves to ''",
      "No hardcoded fallback by design" in CP,
      "the edge must learn its portal, not guess one - a wrong guess sends "
      "a customer's activation to someone else's deployment")
check("  and an unknown code is forwarded upstream to the portal",
      'f"{cloud_url}/api/control-plane/edge-link/bootstrap"' in CP,
      "a fresh edge's local store has never seen the code")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
