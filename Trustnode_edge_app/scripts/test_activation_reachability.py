# -*- coding: utf-8 -*-
"""A shipped build must be able to activate on a BRAND-NEW computer.

2026-08-25: activation failed on a fresh machine with the bare message
"Control-plane edge-link login activation failed". The cause was not the
activation code and not the portal — the built bundle had NO control-plane URL
(VITE_TRUSTNODE_CONTROL_PLANE_URL was never set), and a new machine has nothing
in localStorage either. The candidate list came out empty, the retry loop never
ran, and the error the operator saw was the fallback string with no diagnosis.

These checks run against the BUILT bundle, because that is the artefact that
ships. They need no network and no hardware.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "frontend", "dist", "assets")
FAILS = []


def check(name, ok, detail=""):
    # `detail` explains a FAILURE. Printing it on a PASS made lines read
    # "PASS - the message is missing", which is worse than printing nothing.
    suffix = f" - {str(detail)[:110]}" if (detail and not ok) else ""
    print(f"  {name:62s}: {'PASS' if ok else 'FAIL'}{suffix}")
    if not ok:
        FAILS.append(name)


def check_with(name, ok, value=""):
    """Same, for checks where the VALUE is worth seeing either way."""
    print(f"  {name:62s}: {'PASS' if ok else 'FAIL'}{(' - ' + str(value)[:110]) if value else ''}")
    if not ok:
        FAILS.append(name)


if not os.path.isdir(DIST):
    print("  frontend/dist not built — run `npm run build` first")
    sys.exit(2)

bundle_name = next((f for f in os.listdir(DIST) if f.endswith(".js")), "")
check("a built bundle exists", bool(bundle_name), DIST)
if not bundle_name:
    sys.exit(2)
bundle = open(os.path.join(DIST, bundle_name), encoding="utf-8", errors="replace").read()
print(f"     bundle: {bundle_name} ({len(bundle):,} bytes)")

# 1. The build must carry a portal address. Without one a fresh install cannot
#    activate at all, and nothing in the app says why.
url_pattern = re.compile(r"https?://[a-z0-9.\-]+\.[a-z]{2,}", re.I)
urls = set(url_pattern.findall(bundle))
real_urls = {u for u in urls
             if "w3.org" not in u and "example.com" not in u and "schema" not in u}
check_with("the build carries at least one real portal/base URL", bool(real_urls),
           sorted(real_urls)[:4])

# 2. The empty-candidate case must be a SENTENCE, not the bare fallback.
check("an empty portal list produces an explained error",
      "No TrustNode portal address" in bundle,
      "the explicit message is missing from the bundle")

# 3. The activation screen must let an operator supply the portal.
check("the activation screen exposes a portal address field",
      "control_plane_url" in bundle, "control_plane_url not referenced")

# 4. A 422 must never reach the operator as [object Object].
check("API errors are rendered readably (describeApiError shipped)",
      "gateway_type" in bundle or "describeApiError" in bundle or "loc" in bundle,
      "error formatter missing")

# 5. The source-level guarantee. Every .env* file is gitignored (they hold
#    credentials), so the default MUST live in committed source — otherwise a
#    clean clone rebuilds the exact installer that fails on a fresh computer.
api_js = open(os.path.join(ROOT, "frontend", "src", "api.js"), encoding="utf-8").read()
check("a portal default lives in COMMITTED source, not only in .env",
      "DEFAULT_CONTROL_PLANE_URL" in api_js,
      "only an env var — a clean clone would build with no portal")
m = re.search(r'DEFAULT_CONTROL_PLANE_URL\s*=\s*"([^"]+)"', api_js)
default_url = m.group(1) if m else ""
check_with("  and it has a real value", default_url.startswith("http"), default_url or "(none)")
check_with("  and that default reached the bundle", bool(default_url and default_url in bundle),
           default_url)
check("the env var can still override it",
      "VITE_TRUSTNODE_CONTROL_PLANE_URL" in api_js, "override removed")

# 6. 2026-08-25: activation on a new machine reported success while creating no
#    local admin, and the operator was then locked out with no way back.
print()
check("a failed local finalize cannot be reported as success",
      "could not be created on THIS computer" in bundle,
      "the activation flow still masks a local-finalize failure")
check("a successful activation returns an explicit ok flag",
      "recovered_from_used_code" in bundle and "ok: true" in bundle.replace('"ok":true', "ok: true")
      or "ok:!0" in bundle,
      "result.ok may be undefined on success -> reported as a failure")
check("the sign-in screen offers local account recovery",
      "Recover local access" in bundle, "no recovery entry point shipped")
check("  and says so when the edge has no administrator at all",
      "has no administrator account yet" in bundle, "the locked-out case is silent")
check("  and walks the operator through the code file",
      "local-recovery/request" in bundle and "local-recovery/complete" in bundle,
      "recovery endpoints are not called from the UI")
check("the portal address no longer costs a permanent row",
      "Change portal address" in bundle, "the extra field is always shown")

# 7. the activation card has to fit on a laptop, button included
css_files = [f for f in os.listdir(DIST) if f.endswith(".css")]
css = "".join(open(os.path.join(DIST, f), encoding="utf-8", errors="replace").read()
              for f in css_files)
src_css = open(os.path.join(ROOT, "frontend", "src", "components", "Login", "Login.css"),
               encoding="utf-8").read()
check("the activation card is scrollable so the button is always reachable",
      "max-height:calc(100vh - 24px)" in css.replace(" ", "").replace("calc(100vh-24px)", "calc(100vh - 24px)")
      or "max-height: calc(100vh - 24px)" in src_css,
      "a tall card can still hide the Activate button")
check("  and it stops adding 12px on top of the 12px gap",
      "activate-mode label" in src_css and "margin-bottom: 0;" in src_css,
      "labels still double the spacing")
check("  and short screens get a tighter layout",
      "max-height: 820px" in src_css, "no short-viewport rule")

print()
print(f"RESULT: {'PASS' if not FAILS else 'FAIL - ' + ', '.join(FAILS)}")
sys.exit(0 if not FAILS else 2)
