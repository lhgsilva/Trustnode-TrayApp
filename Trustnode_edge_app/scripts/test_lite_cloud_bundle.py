# -*- coding: utf-8 -*-
"""The cloud Lite bundle must talk to Supabase, not to the LAN placeholder.

2026-10-13, reported: signing in to https://trustnode.lsapps.app/lite/ showed
"Failed to fetch" under the password field, every time, for master and for
customer accounts. Everything server-side checked out: Supabase Auth
reachable, the anon key in /lite/config.json valid, CORS `*`, no CSP, the
account present and confirmed, the service worker ignoring POSTs.

The cause was in the bundle, not the server.

backend/static/lite_view/ is the LAN fork. It hardcodes its config, because
its shim already knows it talks to the local backend:

    cfg = { supabase_url: "local://lan", supabase_anon_key: "local", ... }
    supabase = createClient(cfg.supabase_url, cfg.supabase_anon_key, {});

build_lite_cloud_bundle.py turned that into the cloud app by swapping ONE
importmap entry - the shim for the real supabase-js - and nothing else. So
the cloud build ran the real Supabase client against "local://lan". Every
call it made went nowhere, and the browser reported it the only way it can:
"Failed to fetch", on the login, before anything else could happen.

The correct config.json was on the VPS the whole time (nginx even has a
location block for it) with nothing reading it. The deployed page's fetch
targets contained /api/health and /api/lite-local/* and no config.json at
all - the evidence was visible from outside the whole time.

Shipped 2026-09-30, found 2026-10-13.

This runs the real builder into a temp directory and asserts the OUTPUT, so
the next person who edits either file finds out immediately. Reaches no
network and writes nothing outside the temp directory.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("TrustNode - the cloud Lite bundle points at Supabase")
print()

LAN = os.path.join(ROOT, "backend", "static", "lite_view", "index.html")
BUILDER = os.path.join(ROOT, "scripts", "build_lite_cloud_bundle.py")
lan_src = io.open(LAN, encoding="utf-8").read()

print("[the LAN fork is still the LAN fork]")
check("it hardcodes the local placeholder", 'supabase_url: "local://lan"' in lan_src,
      "if this moved, the cloud builder's anchor must move with it")
check("  and it does NOT read config.json",
      'fetch("config.json"' not in lan_src,
      "the LAN app has no config.json - its shim knows where the backend is")
check("  its importmap uses the shim",
      '"@supabase/supabase-js": "./local_supabase_shim.js"' in lan_src)

print()
print("[build the cloud bundle for real]")
out_dir = tempfile.mkdtemp(prefix="tn-litecloud-")
env = dict(os.environ, PYTHONIOENCODING="utf-8")
proc = subprocess.run([sys.executable, BUILDER, "--out", out_dir],
                      capture_output=True, text=True, cwd=ROOT, env=env)
if proc.returncode != 0 and "--out" in (proc.stderr or ""):
    # Older builder with no --out flag: fall back to its default location.
    proc = subprocess.run([sys.executable, BUILDER],
                          capture_output=True, text=True, cwd=ROOT, env=env)
    out_dir = os.path.join(ROOT, "web_cloud_readonly", "lite")
check("the builder succeeded", proc.returncode == 0,
      (proc.stdout or proc.stderr or "")[-300:])

built_path = os.path.join(out_dir, "index.html")
if not os.path.exists(built_path):
    built_path = os.path.join(ROOT, "web_cloud_readonly", "lite", "index.html")
check("it produced an index.html", os.path.exists(built_path), built_path)

if os.path.exists(built_path):
    built = io.open(built_path, encoding="utf-8").read()

    print()
    print("[the output is a CLOUD app, not a half-converted LAN one]")
    check("the LAN placeholder URL is gone", "local://lan" not in built,
          "this single string is the whole bug: the real Supabase client "
          "pointed at it and every fetch failed")
    check("  the real supabase-js is in the importmap",
          "esm.sh/@supabase/supabase-js" in built)
    check("  the shim is no longer imported",
          '"@supabase/supabase-js": "./local_supabase_shim.js"' not in built)
    check("  it READS config.json", 'fetch("config.json"' in built,
          "the file was served correctly the whole time and nothing read it")
    check("  and refuses a config with no usable url",
          "has no usable supabase_url" in built,
          "failing loudly beats failing as 'Failed to fetch'")

    print()
    print("[the login path is intact]")
    check("sign-in still goes through supabase-js",
          "supabase.auth.signInWithPassword" in built)
    check("  the magic-link path survives too",
          "signInWithOtp" in built)
    check("  the error is still surfaced to the operator",
          "setErr(e.message" in built)

print()
print("[the builder refuses to ship a broken bundle]")
BLD = io.open(BUILDER, encoding="utf-8").read()
check("it fails when the loadConfig anchor moves",
      "the LAN loadConfig() block was not found" in BLD,
      "a silent no-op replace is what shipped this")
check("  it fails if the placeholder survives",
      "the LAN placeholder URL survived into the cloud bundle" in BLD)
check("  it fails if config.json is not read",
      "the cloud bundle does not read config.json" in BLD)
check("  and it still checks the importmap swap",
      "the importmap still points at the shim" in BLD,
      "the check that already existed - which passed while the bundle was "
      "broken, because it only covered one of the two swaps")

print()
print("[the deploy actually ships it]")
# 2026-10-17: this bundle was built and pushed BY HAND. The VPS was measured
# serving tnlite-v51 while the source was at v54 - three rounds of phone and
# web-client fixes sat in git and reached nobody, because the workflow built
# the frontend and the client view and simply never touched Lite.
_WF = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), ".github", "workflows", "deploy-vps.yml")
if not os.path.isfile(_WF):
    check("the deploy workflow was found", False, _WF)
else:
    wf = io.open(_WF, encoding="utf-8").read()
    check("the workflow BUILDS the cloud Lite bundle",
          "build_lite_cloud_bundle.py" in wf,
          "a bundle nobody builds in CI is a bundle that drifts")
    check("  and rsyncs it to the host",
          "web_cloud_readonly/lite/" in wf and "/var/www/trustnode/lite/" in wf)
    _lite_at = wf.find("Rsync cloud Lite bundle")
    _dist_at = wf.find("Rsync frontend dist")
    check("  AFTER the frontend dist rsync",
          _dist_at > 0 and _lite_at > _dist_at,
          "that step is --delete into /var/www/trustnode/, which removes "
          "lite/ - shipping Lite first would publish it and then wipe it")
    # Only THIS step: the next one (db + scripts) legitimately uses --delete,
    # and a window wide enough to reach it fails for the wrong reason.
    _seg = ""
    if _lite_at > 0:
        _end = wf.find("      - name:", _lite_at + 10)
        _seg = wf[_lite_at:_end if _end > 0 else len(wf)]
    check("  and WITHOUT --delete",
          "--delete" not in _seg,
          "build_lite_cloud_bundle.py leaves files it has no local copy of "
          "to the VPS's own; --delete would remove them")
    check("  the deploy verifies what the host actually serves",
          "Verify the served Lite bundle" in wf and "CACHE_VERSION" in wf,
          "a green deploy said nothing about the bundle - that is how v51 "
          "survived three releases")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
