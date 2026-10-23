# -*- coding: utf-8 -*-
"""Start-up must not read the whole database to decide nothing needs doing.

2026-10-23, reported as "app not starting": the splash gave up after 120
seconds. The app was not hung - it was reading a 7.3 GB database to work
out whether it could delete files that did not exist.

`_drop_proven_safety_copies()` runs at boot, before anything opens the
store, and removes `.pre_<kind>_<stamp>` copies left behind by a restore
or a compaction. Deleting those is only safe if the live database is
healthy, so it ran `PRAGMA quick_check(1)` first - on the live file,
unconditionally, on EVERY boot.

Measured on the operator's install, which had no such copies at all:

    _resolve_db_path   15.8s
    AppStore()         15.9s      <- effectively the whole of start-up
    after the fix       0.1s

The check now runs only when there is something to reclaim. That is not a
weakening: the check exists to make a DELETION safe, and when nothing will
be deleted there is nothing to make safe. The order is the fix, not the
rule.

This file pins the cost AND the safety property, because the tempting
version of this change - dropping the integrity check - would be a way to
delete the one copy that could undo a bad swap.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"

from app.services.retention_engine import _drop_proven_safety_copies  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print("  {0:60s}: {1}{2}".format(
        name, "PASS" if ok else "FAIL", (" - " + str(detail)[:140]) if not ok else ""))
    if not ok:
        FAILS.append(name)


def make_db(path, rows=60000):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, blob TEXT)")
    con.executemany("INSERT INTO t(blob) VALUES(?)",
                    [("y" * 400,) for _ in range(rows)])
    con.commit()
    con.close()


print("Boot must not verify a database it is not about to change")
print()

tmp = tempfile.mkdtemp(prefix="tn-reclaim-")
db = os.path.join(tmp, "store.db")
make_db(db)
size_mb = os.path.getsize(db) / 1048576.0
print("  test database: %.1f MB" % size_mb)

# ------------------------------------------------ nothing to reclaim
#
# Timing alone cannot prove this - on a small test database the check is
# fast either way, and a test that passes with the bug present proves
# nothing. What matters is whether the live store is OPENED at all, so
# that is what is counted. On the operator's 7.3 GB file that one open
# was 15.8 seconds.
import app.services.retention_engine as _re                      # noqa: E402

opens = {"n": 0}
_real_connect = sqlite3.connect


def _counting_connect(*a, **k):
    if a and str(a[0]).replace("\\", "/").find(os.path.basename(db)) >= 0:
        opens["n"] += 1
    return _real_connect(*a, **k)


_re.sqlite3.connect = _counting_connect
try:
    opens["n"] = 0
    t0 = time.time()
    _drop_proven_safety_copies(db)
    cold = time.time() - t0
    check("with nothing to reclaim it never opens the database",
          opens["n"] == 0,
          "opened it %d time(s) - that open is the whole of the boot cost "
          "on a large store" % opens["n"])
    check("  and the database is untouched", os.path.exists(db))

finally:
    _re.sqlite3.connect = _real_connect

# ------------------------------------------------ a copy to reclaim
copy = db + ".pre_compaction_20261023_120000"
open(copy, "wb").write(b"superseded copy")
_drop_proven_safety_copies(db)
check("a superseded copy IS removed when the database is healthy",
      not os.path.exists(copy),
      "the reclaim must still happen - this is what frees the disk")

# ------------------------------- the guard still protects a bad database
bad = os.path.join(tmp, "bad.db")
make_db(bad, rows=4000)
with open(bad, "r+b") as fh:          # corrupt the middle of the file
    fh.seek(os.path.getsize(bad) // 2)
    fh.write(b"\x00" * 65536)
bad_copy = bad + ".pre_restore_20261023_120000"
open(bad_copy, "wb").write(b"the only way back")
_drop_proven_safety_copies(bad)
check("a copy is KEPT when the live database fails its check",
      os.path.exists(bad_copy),
      "with a corrupt store that copy is the only way back - deleting it "
      "would turn a recoverable fault into data loss")

# --------------------------------------- the order is what makes it cheap
src = open(os.path.join(ROOT, "backend", "app", "services",
                        "retention_engine.py"), encoding="utf-8").read()
body = src[src.index("def _drop_proven_safety_copies"):]
body = body[:body.index("\ndef ", 10)]
check("candidates are looked for BEFORE the integrity check",
      body.index('startswith(base + ".pre_")') < body.index("PRAGMA quick_check"),
      "checking first is what cost 15.8s of every boot")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
