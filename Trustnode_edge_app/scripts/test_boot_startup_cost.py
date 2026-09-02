# -*- coding: utf-8 -*-
"""Start-up must not pay for the historian.

2026-08-31, after installing the fixed build: "still a lot of problems on the
startup, taking too long to startup, not correctly and broken and did not
started for minutes, this should not happen when we restart the app."

Two costs, both measured on the live install (15.9 GB store, 16.0 M rows,
41 hours of data, ~108 rows/s):

1. THE SCAN AT BOOT. Moving the Data Continuity scan off the request path made
   the steady state fast - four consecutive bootstraps took 0.16-0.97 s where
   they used to take 16-23 s. But the first bootstrap still SCHEDULED that 17 s
   full scan, and it ran while the app was opening everything else: a bootstrap
   issued during it measured 20.57 s. The scan now waits until the process has
   been up a while.

2. THE WAL. trustnode_app_store.db-wal had reached 1.66 GB. Checkpoints were
   running, but only PASSIVE ones, which backfill and never truncate; with ~22
   pollers and continuous writes there is always an overlapping reader, so the
   file could never reset. Every fresh start then pays to index it. TRUNCATE at
   shutdown - the one moment nothing is reading - keeps the next start cheap.

The clock detail matters too: time.monotonic() counts from SYSTEM boot, so it
cannot answer "how long has this process been up". A module-load stamp can.
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:54s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:140]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("[the Data Continuity scan stays away from start-up]")
src = io.open(os.path.join(ROOT, "backend", "app", "services", "app_store.py"),
              encoding="utf-8", errors="replace").read()
check("process age comes from a module-load stamp",
      "_MODULE_LOAD_MONO = time.monotonic()" in src
      and "_time.monotonic() - _MODULE_LOAD_MONO" in src,
      "monotonic() alone counts from SYSTEM boot, not process start")
check("bootstrap's peek refuses to scan while booting",
      "if not fresh and not booting and not self._tenant_inventory_refreshing:" in src)
check("the dedicated inventory endpoint still scans for real",
      src.count("def list_historian_tenant_inventory") == 1
      and "peek_historian_tenant_inventory" in src,
      "the Data Continuity page must show true numbers on demand")

from app.services.app_store import AppStore  # noqa: E402

store = AppStore.__new__(AppStore)           # no side effects; we only need the methods
check("a fresh process peeks without scheduling a scan",
      store.peek_historian_tenant_inventory() == []
      and store._tenant_inventory_refreshing is False,
      "the first bootstrap must not kick off a 17 s scan")

print()
print("[the WAL does not grow forever]")
tmp = tempfile.mkdtemp(prefix="tn-wal-")
db = os.path.join(tmp, "w.db")
conn = sqlite3.connect(db)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA wal_autocheckpoint=0")   # force the WAL to accumulate
conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, blob TEXT)")
conn.executemany("INSERT INTO t (blob) VALUES (?)",
                 (("x" * 400,) for _ in range(60_000)))
conn.commit()
wal = db + "-wal"
grew = os.path.getsize(wal) if os.path.exists(wal) else 0
check("the WAL grew as expected for the test", grew > 2_000_000,
      "%.1f MB" % (grew / 1e6))

# A PASSIVE checkpoint backfills but leaves the file - that is the behaviour
# that let the live install reach 1.66 GB.
conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
after_passive = os.path.getsize(wal) if os.path.exists(wal) else 0
check("  PASSIVE alone does not shrink the file", after_passive >= grew * 0.9,
      "%.1f MB -> %.1f MB" % (grew / 1e6, after_passive / 1e6))
conn.close()

store._db_path = db
t = time.time()
store._checkpoint_wal_truncate()
elapsed = time.time() - t
after = os.path.getsize(wal) if os.path.exists(wal) else 0
check("shutdown's TRUNCATE empties it", after < 100_000,
      "%.1f MB -> %.3f MB in %.2f s" % (after_passive / 1e6, after / 1e6, elapsed))

# The data must still be there - a checkpoint moves frames, it never drops them.
conn = sqlite3.connect(db)
n = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
conn.close()
check("  and every row survived it", n == 60_000, "%s rows" % f"{n:,}")

check("TRUNCATE is called from shutdown and nowhere else",
      src.count("_checkpoint_wal_truncate()") == 1
      and src.index("_checkpoint_wal_truncate()") > src.index("def shutdown(self)"),
      "during operation there is always a reader, so it would block")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
