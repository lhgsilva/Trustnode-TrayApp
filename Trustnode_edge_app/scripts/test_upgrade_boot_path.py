# -*- coding: utf-8 -*-
"""Upgrading over an older install must not stall or corrupt the boot.

2026-10-07, reported: the new exe "fail to start, backend did not started"
on a computer that had an older version.

Cause: _resolve_db_path() copied the whole legacy app store into
%ProgramData% at IMPORT time, before anything could answer /api/health.
Measured on a real 4.96 GB store with the packaged binary: 36 s to first
health, against 5 s once migrated and 3.5 s on a fresh machine. The desktop
shell gives up at 120 s with "Backend service did not respond"; on slower
storage, with Defender scanning a multi-gigabyte write, that copy runs for
minutes.

It only ever happened on an upgrade - a fresh machine has no legacy store -
and the packaged --boot-probe sets TRUSTNODE_APP_STORE_PATH, which takes an
earlier branch, so the build gate could not see it.

Everything here runs against temp directories; the real store is never read
or written.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ.setdefault("TRUSTNODE_SKIP_DOTENV", "1")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


from app.services.app_store import AppStore                  # noqa: E402


class _Probe(AppStore):
    """Only the path decision - no database is opened."""

    def __init__(self):            # noqa: D107 - deliberately not super()
        pass


def make_store(path: str, mb: float) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "wb") as fh:
        fh.write(b"SQLite format 3\x00")
        fh.truncate(int(mb * 1048576))


print("TrustNode - upgrading over an older install")
print()

probe = _Probe()

print("[a big legacy store is adopted where it is, not copied]")
tmp = tempfile.mkdtemp(prefix="tn-upg-big-")
home = os.path.join(tmp, "home")
legacy = os.path.join(home, ".trustnode_edge", "data", "trustnode_app_store.db")
new = os.path.join(tmp, "programdata", "TrustNode", "edge", "trustnode_app_store.db")
os.makedirs(os.path.dirname(new), exist_ok=True)
make_store(legacy, 700)          # over the 512 MB cap
_real_expand = os.path.expanduser
os.path.expanduser = lambda p: p.replace("~", home) if p.startswith("~") else p
try:
    got = probe._adopt_legacy_store(new)
    check("it opens the legacy file in place", got == legacy, got)
    check("  nothing was copied", not os.path.exists(new),
          "a 4.96 GB copy is what pushed boot past the 120 s budget")
    check("  and no staging file is left behind",
          not os.path.exists(new + ".migrating"))

    print()
    print("[a small one is still migrated, atomically]")
    tmp2 = tempfile.mkdtemp(prefix="tn-upg-small-")
    home2 = os.path.join(tmp2, "home")
    legacy2 = os.path.join(home2, ".trustnode_edge", "data", "trustnode_app_store.db")
    new2 = os.path.join(tmp2, "programdata", "TrustNode", "edge", "trustnode_app_store.db")
    os.makedirs(os.path.dirname(new2), exist_ok=True)
    make_store(legacy2, 4)
    os.path.expanduser = lambda p: p.replace("~", home2) if p.startswith("~") else p
    got2 = probe._adopt_legacy_store(new2)
    check("it migrates into %ProgramData%", got2 == new2, got2)
    check("  the copy is complete",
          os.path.exists(new2)
          and os.path.getsize(new2) == os.path.getsize(legacy2),
          "%s bytes" % (os.path.getsize(new2) if os.path.exists(new2) else -1))
    check("  no .migrating file survives",
          not os.path.exists(new2 + ".migrating"),
          "a half-written file that EXISTS is treated as migrated by the "
          "next boot - that is silent corruption")

    print()
    print("[an interrupted copy can never be mistaken for a finished one]")
    src = io.open(os.path.join(ROOT, "backend", "app", "services",
                               "app_store.py"), encoding="utf-8").read()
    fn = src.split("def _adopt_legacy_store", 1)[1].split("\n    def ", 1)[0]
    check("the copy lands on a staging name first",
          'staging = new_path + ".migrating"' in fn
          and "shutil.copy2(legacy, staging)" in fn,
          "copy2 straight onto the real name is the corruption path")
    check("  and is published with an atomic replace",
          "os.replace(staging, new_path)" in fn)
    check("  a failed copy falls back to the legacy file",
          fn.count("return legacy") >= 3,
          "booting on a partial copy is worse than not migrating")

    print()
    print("[free space is checked before committing to a copy]")
    check("the space check is there",
          "shutil.disk_usage(" in fn and "free < size * 1.2" in fn,
          "there was none; a full disk produced a truncated store")

    print()
    print("[an explicit workspace never adopts someone else's data]")
    rp = src.split("def _resolve_db_path", 1)[1].split("\n    def ", 1)[0]
    check("only a DEFAULTED location migrates",
          "defaulted = not data_dir" in rp
          and "if sys.platform == \"win32\" and defaulted:" in rp,
          "a scratch TRUSTNODE_DATA_DIR used to pull in the production store")
    check("  TRUSTNODE_APP_STORE_PATH still wins outright",
          "if env_path:" in rp and "base = env_path" in rp)
finally:
    os.path.expanduser = _real_expand

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
