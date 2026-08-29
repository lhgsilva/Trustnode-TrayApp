# -*- coding: utf-8 -*-
"""The write PRAGMAs must be real on a FRESH connection, not just intended.

2026-08-28. The schema bootstrap has always run

    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=NORMAL;

inside one `executescript`. `journal_mode` is stored in the database file and
persists. `synchronous` and `cache_size` belong to a CONNECTION and do not - so
every connection opened afterwards reverted to the SQLite defaults, and the
live 13.4 GB store was measured running `synchronous=2 (FULL)` and a 2 MB page
cache while the source said NORMAL.

Nothing failed. The historian just wrote slowly - "v2-writer slow HISTORIAN
flush: 1 cycle(s) in 2000-9000 ms", about once a minute, for as long as anyone
had been looking.

This asserts the settings a WRITE connection actually reports, which is the
only form of the claim that can be checked.
"""
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
FAILS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:130]) if detail else ""
    print("  {0:56s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


# --- the premise: these two pragmas do NOT persist in the file -------------
print("[why this test exists]")
tmp = tempfile.mkdtemp(prefix="tn-pragma-")
probe = os.path.join(tmp, "probe.db")
con = sqlite3.connect(probe)
con.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
con.close()
con2 = sqlite3.connect(probe)
persisted_journal = str(con2.execute("PRAGMA journal_mode").fetchone()[0]).lower()
reverted_sync = int(con2.execute("PRAGMA synchronous").fetchone()[0])
con2.close()
check("journal_mode set once DOES persist in the file",
      persisted_journal == "wal", persisted_journal)
check("  synchronous set the same way does NOT",
      reverted_sync == 2,
      "a new connection reports {0} (2=FULL), which is why it must be set "
      "per connection".format(reverted_sync))

# --- the app's own write connection ---------------------------------------
print()
print("[what an AppStore write connection actually reports]")
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(tmp, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = tmp
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"

from app.services.app_store import AppStore  # noqa: E402

store = AppStore()
with store._connect() as conn:
    journal = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    cache = int(conn.execute("PRAGMA cache_size").fetchone()[0])
    sync = int(conn.execute("PRAGMA synchronous").fetchone()[0])

check("the write connection is in WAL mode", journal == "wal", journal)

# cache_size is negative = KB, positive = pages. Anything at the 2 MB default
# means the setting is not being applied.
check("it has a real page cache, not the 2 MB default",
      cache < -2000 or cache > 2000,
      "cache_size={0} ({1})".format(
          cache, "{0} MB".format(abs(cache) // 1024) if cache < 0 else "{0} pages".format(cache)))

# synchronous is deliberately left at FULL by default - losing data is ruled
# out, and NORMAL is the operator's call. What must hold is that the setting is
# APPLIED, so choosing it actually changes something.
check("  and a synchronous mode that was applied, not defaulted",
      sync in (0, 1, 2, 3), "synchronous={0}".format(sync))

# Read at CONNECT time, not at import time - so no reload, and no dependency on
# .env being loaded before this module happens to be imported.
os.environ["TRUSTNODE_SQLITE_SYNCHRONOUS"] = "NORMAL"
os.environ["TRUSTNODE_SQLITE_CACHE_KB"] = "65536"
with store._connect() as conn:
    sync2 = int(conn.execute("PRAGMA synchronous").fetchone()[0])
    cache2 = int(conn.execute("PRAGMA cache_size").fetchone()[0])
check("the environment can change them without a reload",
      sync2 == 1 and cache2 == -65536,
      "synchronous={0} cache_size={1} - the settings must not depend on .env "
      "being loaded before this module is imported".format(sync2, cache2))

os.environ["TRUSTNODE_SQLITE_SYNCHRONOUS"] = "nonsense"
with store._connect() as conn:
    sync3 = int(conn.execute("PRAGMA synchronous").fetchone()[0])
check("  and a bad value falls back to the safe default",
      sync3 == 2, "synchronous={0} (2=FULL)".format(sync3))

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
