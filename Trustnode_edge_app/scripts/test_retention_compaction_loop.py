# -*- coding: utf-8 -*-
"""Compaction must not destroy its own staged result, and must give disk back.

2026-10-17, reported: "the app is running and is not collecting". Measured on
the live install:

    app store            14.87 GB   (page_count 3,631,421)
    free pages           13.23 GB   (freelist_count 3,230,237 - 89% of the file)
    actually in use       1.64 GB
    backend CPU           268s in 5.4 minutes of uptime, still >100% of a core
    historian writes      none

The retention engine was compacting correctly - 14.87 GB -> 1.61 GB, 13.26 GB
reclaimed, 44 seconds per run - and then throwing the result away:

    target = db_path + ".compacted"
    for stale in (target, target + ".part"):
        os.remove(stale)              # <- the copy staged by the LAST tick

A staged compaction is applied by apply_pending_db_swap at the next start.
With a 60s tick and a 44s compaction, every tick deleted the copy the
previous tick had staged, so it could only ever be applied if the app
happened to restart inside a sliver of a window. It never did. The file
stayed at 89% free, so the next tick compacted again - for ever - while
collection starved behind a pegged process.

Two more costs found with it: the tick was 60s (retention deletes data hours
old; a minute-by-minute pass buys nothing), and the superseded databases left
behind by past successful swaps were never removed - 11.8 GB and 5.3 GB of
them on one install.

Runs against temp files. Touches no live store and no network.
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

_TMP = tempfile.mkdtemp(prefix="tn-compact-")
os.environ.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=_TMP,
                  TRUSTNODE_APP_STORE_PATH=os.path.join(_TMP, "scratch.db"),
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("TrustNode - compaction keeps its result, and the disk comes back")
print()

SRC = io.open(os.path.join(ROOT, "backend", "app", "services",
                           "retention_engine.py"), encoding="utf-8").read()

print("[1. a staged compaction is never destroyed]")
check("compact() returns early when one is already staged",
      'if os.path.exists(target):' in SRC and '"already_staged": True' in SRC,
      "it used to os.remove(target) first - deleting work already done")
check("  only a half-written .part is cleared",
      'if os.path.exists(target + ".part"):' in SRC)
check("  and the message tells the operator a restart applies it",
      "restart to" in SRC.lower() or "next time TrustNode starts" in SRC)

# The old code deleted `target`; prove that exact line is gone.
_bad = ('for stale in (target, target + ".part"):\n            try:\n'
        '                if os.path.exists(stale):\n                    os.remove(stale)')
check("  the self-destructing loop is gone", _bad not in SRC,
      "this single loop is what made the file never shrink")

print()
print("[2. retention does not run every minute]")
import app.services.retention_engine as re_mod                       # noqa: E402
check("the default tick is at least 10 minutes",
      re_mod.RETENTION_TICK_S >= 600,
      "%ds - a 44s compaction on a 60s tick runs essentially continuously"
      % re_mod.RETENTION_TICK_S)
check("  it is still overridable",
      "TRUSTNODE_RETENTION_TICK_S" in SRC)
check("  and the boot delay still holds it off at startup",
      re_mod.RETENTION_BOOT_DELAY_S >= 60, re_mod.RETENTION_BOOT_DELAY_S)

print()
print("[3. superseded databases are not left on disk]")
check("the swap prunes older pre_<kind>_ copies",
      "superseded" in SRC and "olds[1:]" in SRC,
      "two were found holding 11.8 GB and 5.3 GB")
check("  but keeps the newest as the undo",
      "olds[1:]" in SRC,
      "the copy just displaced is the only way back from a bad swap")

print()
print("[4. the swap really does replace the file]")
from app.services.retention_engine import apply_pending_db_swap      # noqa: E402

live = os.path.join(_TMP, "swaptest.db")
con = sqlite3.connect(live)
con.execute("CREATE TABLE t(a)")
con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(500)])
con.commit(); con.close()

staged = live + ".compacted"
con = sqlite3.connect(staged)
con.execute("CREATE TABLE t(a)")
con.execute("INSERT INTO t VALUES (99)")
con.commit(); con.close()

# A couple of stale predecessors from earlier swaps.
for stamp in ("20260101_000000", "20260202_000000"):
    with io.open(live + ".pre_compaction_" + stamp, "wb") as fh:
        fh.write(b"x" * 2048)

note = apply_pending_db_swap(live)
check("the staged file was applied", bool(note), note)
con = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
n = con.execute("SELECT COUNT(*) FROM t").fetchone()[0]
con.close()
check("  the live database is now the compacted one", n == 1,
      "%d rows - 500 means the swap did not happen" % n)
check("  the staged file is consumed", not os.path.exists(staged))

leftovers = sorted(f for f in os.listdir(_TMP)
                   if f.startswith("swaptest.db.pre_compaction_"))
check("  exactly one predecessor is kept", len(leftovers) == 1,
      "%d kept: %s" % (len(leftovers), leftovers))

print()
print("[5. a corrupt staged file is discarded, not applied]")
live2 = os.path.join(_TMP, "bad.db")
con = sqlite3.connect(live2)
con.execute("CREATE TABLE t(a)")
con.execute("INSERT INTO t VALUES (7)")
con.commit(); con.close()
with io.open(live2 + ".compacted", "wb") as fh:
    fh.write(b"this is not a database")
apply_pending_db_swap(live2)
con = sqlite3.connect(f"file:{live2}?mode=ro", uri=True)
ok = con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
con.close()
check("the live database survived", ok,
      "a failed integrity check must never replace a working database")
check("  and the bad candidate was removed",
      not os.path.exists(live2 + ".compacted"))

print()
print("[6. the compacted file uses incremental auto-vacuum]")
check("compaction switches the new file to INCREMENTAL",
      'PRAGMA auto_vacuum=INCREMENTAL' in SRC,
      "so future deletes return pages without another full compaction")
check("  and the engine runs incremental_vacuum after a prune",
      "PRAGMA incremental_vacuum" in SRC,
      "this is what stops the freelist growing back")

print()
print("[7. a NEW database can give its pages back]")
# 2026-10-17, asked: "check if with a new install the database is sanitized
# and lighter". It was not: nothing set auto_vacuum, so a fresh install was
# created with auto_vacuum=NONE and would bloat exactly like the one that
# reached 14.87 GB. auto_vacuum can only be set BEFORE the first table
# exists, so it has to happen at creation.
import tempfile as _tf                                               # noqa: E402
_fresh = os.path.join(_tf.mkdtemp(prefix="tn-fresh-"), "new_store.db")
os.environ["TRUSTNODE_APP_STORE_PATH"] = _fresh
from app.services.app_store import AppStore                          # noqa: E402

_store = AppStore()
_c = sqlite3.connect(f"file:{_fresh}?mode=ro", uri=True)
_av = int(_c.execute("PRAGMA auto_vacuum").fetchone()[0] or 0)
_c.close()
check("a newly created store is auto_vacuum=INCREMENTAL", _av == 2,
      "got %d (0=NONE, 1=FULL, 2=INCREMENTAL) - NONE means deleted pages sit "
      "on a freelist for ever and only a full rewrite reclaims them" % _av)

# A POPULATED database must be left alone: VACUUMing a 14 GB store at boot
# would be far worse than the bloat. Asserted behaviourally, not by grepping
# for an implementation detail.
_pop = os.path.join(_tf.mkdtemp(prefix="tn-pop-"), "existing.db")
_pc = sqlite3.connect(_pop)
_pc.execute("CREATE TABLE already_here(x)")
_pc.executemany("INSERT INTO already_here VALUES (?)", [(i,) for i in range(200)])
_pc.commit(); _pc.close()
_before = os.path.getsize(_pop)
_s2 = AppStore.__new__(AppStore)
_s2._db_path = _pop
AppStore._ensure_incremental_autovacuum(_s2)
_pc = sqlite3.connect(f"file:{_pop}?mode=ro", uri=True)
_av2 = int(_pc.execute("PRAGMA auto_vacuum").fetchone()[0] or 0)
_rows = _pc.execute("SELECT COUNT(*) FROM already_here").fetchone()[0]
_pc.close()
# The case the VACUUM exists for: the file was already created and put into
# WAL by an aborted start, but has no tables yet. page_count is then 1, and
# SQLite ACCEPTS the auto_vacuum pragma while silently IGNORING it - so the
# pragma alone is not enough and only the VACUUM makes it stick.
_wal = os.path.join(_tf.mkdtemp(prefix="tn-wal-"), "halfborn.db")
_wc = sqlite3.connect(_wal)
_wc.execute("PRAGMA journal_mode=WAL")
_wc.commit(); _wc.close()
_s3 = AppStore.__new__(AppStore)
_s3._db_path = _wal
AppStore._ensure_incremental_autovacuum(_s3)
_wc = sqlite3.connect(f"file:{_wal}?mode=ro", uri=True)
_av3 = int(_wc.execute("PRAGMA auto_vacuum").fetchone()[0] or 0)
_wc.close()
check("  a WAL-touched but tableless file still converts", _av3 == 2,
      "page_count is already 1 there, so the pragma alone is ignored - this "
      "is what the VACUUM is for (auto_vacuum=%d)" % _av3)

check("  a POPULATED database is left untouched", _av2 == 0,
      "rewriting a 14 GB store at boot would be worse than the bloat "
      "(auto_vacuum=%d)" % _av2)
check("    and its rows are intact", _rows == 200, _rows)
check("  and the compaction sets it on the copy it builds",
      "PRAGMA auto_vacuum=INCREMENTAL" in SRC,
      "that is how an EXISTING install gets it")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
