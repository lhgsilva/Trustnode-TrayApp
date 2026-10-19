# -*- coding: utf-8 -*-
"""A store the app cannot write must not kill the backend.

2026-10-19. The backend exited with code 1 on start, five times in one day,
writing no traceback the operator could see. Run directly it said:

    sqlite3.OperationalError: attempt to write a readonly database
      app/services/app_store.py, _ensure_schema
    [PYI-3632:ERROR] Failed to execute script '__main__'

The default Windows store path is %ProgramData%/TrustNode/edge, and the
installer creates that folder with "Authenticated Users:(OI)(CI)RX" so a
customer's regular user cannot edit the SQLite and flip licence flags. The
tray app RUNS as that user. So the default path is one it can read and never
write, and any start without TRUSTNODE_DATA_DIR died before serving anything.

Measured there, and this is why no file check caught it:

    create a file in the dir : ok
    open the .db   r+b       : ok
    open -wal/-shm r+b       : PermissionError [Errno 13]
    sqlite write             : attempt to write a readonly database

In WAL mode SQLite must write the -wal and -shm sidecars; those two carried
the restrictive ACL while the directory and the .db did not. os.access()
reported True for all of it - on Windows it reports the read-only ATTRIBUTE
and ignores the ACL entirely.

Runs on temp directories. Touches no live store.
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "backend", "app", "services", "app_store.py")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("TrustNode - an unwritable store falls back instead of dying")
print()

src = io.open(SRC, encoding="utf-8").read()
seg = src[src.index("def _store_is_writable"):src.index("class AppStore")]
ns: dict = {"os": os, "sqlite3": sqlite3, "__name__": "probe"}
exec(compile(seg, "probe", "exec"), ns)        # noqa: S102
writable = ns["_store_is_writable"]

tmp = tempfile.mkdtemp(prefix="tn-ro-")

print("[1. the probe answers the question SQLite will ask]")
fresh = os.path.join(tmp, "fresh", "store.db")
check("an empty, creatable location is writable", writable(fresh) is True)

good = os.path.join(tmp, "good", "store.db")
os.makedirs(os.path.dirname(good), exist_ok=True)
con = sqlite3.connect(good)
con.execute("CREATE TABLE t(a)")
con.commit()
con.close()
check("  a normal database is writable", writable(good) is True)

missing_dir = os.path.join(tmp, "nope", "deep")
check("  a path that cannot even be created is not",
      writable(os.path.join(os.devnull, "x", "store.db")) is False,
      "a directory that cannot be made must not read as writable")

print()
print("[2. a database SQLite refuses is reported unwritable]")
# Simulate the real failure without needing an ACL: a WAL database whose
# -shm cannot be used. Easiest faithful stand-in is a corrupt header, which
# makes BEGIN IMMEDIATE fail exactly as the ACL did.
bad = os.path.join(tmp, "bad", "store.db")
os.makedirs(os.path.dirname(bad), exist_ok=True)
io.open(bad, "wb").write(b"this is not a database at all")
check("a database SQLite cannot write is False", writable(bad) is False,
      "the probe must fail CLOSED - a false 'writable' puts us back to "
      "dying in _ensure_schema")

print()
print("[3. the probe is a real write lock, not a file stat]")
check("it uses BEGIN IMMEDIATE", "BEGIN IMMEDIATE" in seg,
      "os.access and a plain open() both said yes to the directory that "
      "refused every write")
check("  and it rolls back, changing nothing", "ROLLBACK" in seg)
# Check for a CALL, not a mention: the docstring explains WHY os.access is
# the wrong tool, so a bare substring match fails on the explanation itself.
# (Same trap as earlier today: an assertion tripping over its own comment.)
_code_only = chr(10).join(
    ln for ln in seg.splitlines()
    if ln.strip() and not ln.strip().startswith("#"))
_body = _code_only.split('"""')[0] + _code_only.split('"""')[-1]
check("  os.access is NOT what decides", "os.access(" not in _body,
      "on Windows it reports the read-only attribute and ignores the ACL")

print()
print("[4. the resolver falls back instead of dying]")
check("the defaulted path is checked for writability",
      "_store_is_writable(" in src and "defaulted and not _store_is_writable" in src,
      "only a DEFAULTED location may be swapped - an explicit "
      "TRUSTNODE_DATA_DIR means 'use this workspace'")
check("  it falls back to the user's own data dir",
      '".trustnode_edge", "data")' in src)
# The message is split across source lines, so match its parts.
check("  and says so, loudly",
      "installer ACL" in src and "WARN" in src and "using %s instead" in src,
      "a silent relocation is its own kind of wrong - the operator has to "
      "know which workspace is live")

print()
print("[5. an explicit workspace is still obeyed]")
# The fallback must never override an operator who named a directory: that
# is how a test or a build probe would get pulled onto the production store.
i = src.index("defaulted = not data_dir")
window = src[i:i + 2000]
check("the fallback only runs when defaulted",
      "defaulted and not _store_is_writable" in window,
      "TRUSTNODE_DATA_DIR must win even if the operator pointed at "
      "something read-only - that is their choice to see fail")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
