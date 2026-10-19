# -*- coding: utf-8 -*-
"""The signing secret must live beside the app store that is actually live.

2026-10-19, reported: "Your session has ended" on every start, with
"Configuration not loaded ... Session expired. Please sign in again." The app
kept running and collecting - only the SCREEN lost its session, which is the
tell that this is about the token, not the data.

The JWT secret lives in trustnode_auth.db. auth_store._resolve_db_path used
to default straight to %ProgramData%\\TrustNode\\edge on Windows, while
app_store defaults there and then calls _adopt_legacy_store(), which moves it
to the workspace the operator is really on. The two could therefore land in
DIFFERENT directories, each holding its OWN secret. Measured on the reporting
machine:

    ~/.trustnode_edge/data/trustnode_auth.db        8dc55a10305fa266
    %ProgramData%/TrustNode/edge/trustnode_auth.db  9db5478113af8789

A token signed by one is rejected by the other, so any restart that resolved
differently invalidated every session.

Both candidates routinely exist - a re-activated install has both - so
"whichever exists" is not enough: it has to be the one in USE.

Runs entirely on temp directories. Touches no live store.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "backend", "app", "services", "auth_store.py")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("TrustNode - the auth secret follows the live app store")
print()

# Load just the resolver, with no app imports and no live environment.
for var in ("TRUSTNODE_DATA_DIR", "TRUSTNODE_AUTH_DB_PATH"):
    os.environ.pop(var, None)
src = io.open(SRC, encoding="utf-8").read()
head = src.split("def _utc_now")[0]
ns: dict = {"__name__": "probe"}
exec(compile(head, "probe", "exec"), ns)          # noqa: S102
resolve = ns["_resolve_db_path"]
pick = ns["_data_dir_of_the_live_app_store"]

print("[1. an explicit path still wins]")
tmp = tempfile.mkdtemp(prefix="tn-auth-")
explicit = os.path.join(tmp, "custom", "my_auth.db")
os.environ["TRUSTNODE_AUTH_DB_PATH"] = explicit
check("TRUSTNODE_AUTH_DB_PATH is honoured", str(resolve()) == explicit, resolve())
del os.environ["TRUSTNODE_AUTH_DB_PATH"]

print()
print("[2. TRUSTNODE_DATA_DIR still wins over the search]")
ws = os.path.join(tmp, "workspace")
os.makedirs(ws, exist_ok=True)
os.environ["TRUSTNODE_DATA_DIR"] = ws
check("the workspace env var is honoured",
      os.path.dirname(str(resolve())) == ws, resolve())
del os.environ["TRUSTNODE_DATA_DIR"]

print()
print("[3. with TWO app stores, it picks the LIVE one]")
# This is the case that broke: a re-activated install has both, and the
# stale one is often the FIRST candidate on Windows.
old_dir = os.path.join(tmp, "programdata", "TrustNode", "edge")
new_dir = os.path.join(tmp, "userhome", ".trustnode_edge", "data")
os.makedirs(old_dir, exist_ok=True)
os.makedirs(new_dir, exist_ok=True)
io.open(os.path.join(old_dir, "trustnode_app_store.db"), "wb").write(b"old")
time.sleep(1.1)                      # a filesystem-visible age difference
io.open(os.path.join(new_dir, "trustnode_app_store.db"), "wb").write(b"new")

ns["sys"].platform = "win32"
os.environ["PROGRAMDATA"] = os.path.join(tmp, "programdata")
ns["Path"] = ns["Path"]
_real_home = ns["Path"].home
ns["Path"].home = staticmethod(lambda: ns["Path"](os.path.join(tmp, "userhome")))
try:
    # Call the REAL entry point, not the helper. The first version of this
    # test asserted on _data_dir_of_the_live_app_store() directly, so it
    # still passed when _resolve_db_path was changed to ignore it - a test
    # that cannot fail proves nothing.
    chosen = os.path.dirname(str(resolve()))
    check("the NEWER app store's directory is chosen",
          os.path.normcase(chosen) == os.path.normcase(new_dir),
          "chose %s; the stale ProgramData copy is listed first, so "
          "'first that exists' would have taken it" % chosen)

    # And the reverse, so the test is measuring recency and not an ordering
    # that happens to be right.
    time.sleep(1.1)
    io.open(os.path.join(old_dir, "trustnode_app_store.db"), "wb").write(b"older-now-newer")
    chosen2 = os.path.dirname(str(resolve()))
    check("  and it follows the store when the OTHER becomes newer",
          os.path.normcase(chosen2) == os.path.normcase(old_dir),
          "chose %s - if this still said the user dir, the check would be "
          "passing by luck of ordering" % chosen2)

    print()
    print("[4. a genuinely fresh install is unchanged]")
    empty = os.path.join(tmp, "fresh")
    os.makedirs(os.path.join(empty, "programdata", "TrustNode", "edge"), exist_ok=True)
    os.environ["PROGRAMDATA"] = os.path.join(empty, "programdata")
    ns["Path"].home = staticmethod(lambda: ns["Path"](os.path.join(empty, "userhome")))
    fresh = os.path.dirname(str(resolve()))
    check("with no app store anywhere it keeps the platform default",
          fresh.endswith(os.path.join("TrustNode", "edge")),
          "%s - first-run behaviour must not change" % fresh)
finally:
    ns["Path"].home = _real_home

print()
print("[5. the rule is stated where the next person will look]")
check("the resolver explains why it follows the store",
      "signed by one is rejected by the other" in src,
      "a future edit that 'simplifies' this back to a constant path "
      "reintroduces a silent logout on every restart")
check("  and the two stores are named",
      "trustnode_app_store.db" in src and "_AUTH_DB_NAME" in src)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
