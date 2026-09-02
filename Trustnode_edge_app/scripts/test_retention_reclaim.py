# -*- coding: utf-8 -*-
"""The local file must actually shrink, and the cloud must be prunable.

2026-08-31: "we need to make sure to make the local database small, following
the retention, deletion what is not needed anymore, and also an option on the
database setting for the cloud retention, so the data also from the cloud is
deleted if enabled".

Measured on the live install that prompted this:

    retention policy   raw kept 2 days, 1 m tier kept 1 month
    retention runs     1 106, last one ok
    page_count         4 083 432   ~16.7 GB
    freelist_count     1 743 253   ~7.1 GB ALREADY FREE inside the file

So retention was working and the file still would not shrink: SQLite keeps
deleted pages on a freelist and reuses them. 43% of a 16 GB file was dead
space, and every connection open pays for file size - which is what made those
opens cost 1-3 s.

Two things are checked here.

  * `_compact_waste` measures that dead space, and a maintenance pass compacts
    when it is worth the work. This is the difference between "retention runs"
    and "the disk comes back".
  * Cloud retention exists, is OFF by default, and every delete it issues is
    scoped to one tenant. Several edges can share one Supabase database; a
    prune that forgot the tenant would delete somebody else's history.
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:56s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("[dead space is measured, and reclaimed when it is worth it]")
src = io.open(os.path.join(ROOT, "backend", "app", "services", "retention_engine.py"),
              encoding="utf-8", errors="replace").read()
check("a pass measures how much of the file is free",
      "def _compact_waste" in src and "freelist_count" in src,
      "freelist_count / page_count is exactly the deleted-but-not-returned space")
check("  and compacts when it passes the threshold",
      'summary["compact"] = self.compact()' in src
      and "auto_compact_free_pct" in src)
check("  but never on a dry run",
      'auto_compact", True)) and not dry_run' in src,
      "a dry run must not rewrite a 16 GB file")

# The arithmetic, on a real file with real dead space.
tmp = tempfile.mkdtemp(prefix="tn-reclaim-")
db = os.path.join(tmp, "w.db")
conn = sqlite3.connect(db)
conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, blob TEXT)")
conn.executemany("INSERT INTO t (blob) VALUES (?)", (("x" * 500,) for _ in range(40000)))
conn.commit()
full = os.path.getsize(db)
conn.execute("DELETE FROM t WHERE id % 2 = 0")
conn.commit()
after_delete = os.path.getsize(db)
page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
free_pct = (freelist / float(page_count)) * 100.0
conn.close()
check("deleting rows does NOT shrink the file",
      after_delete >= full * 0.95,
      "%.1f MB -> %.1f MB after deleting half the rows"
      % (full / 1e6, after_delete / 1e6))
conn = sqlite3.connect(db)
conn.execute("VACUUM")
conn.close()
vacuumed = os.path.getsize(db)
check("  compacting is what returns it", vacuumed < after_delete * 0.75,
      "%.1f MB -> %.1f MB" % (after_delete / 1e6, vacuumed / 1e6))

print()
print("[cloud retention: opt-in and tenant-scoped]")
from app.services.retention_engine import DEFAULT_CLOUD, validate_policy  # noqa: E402

check("it is OFF by default", DEFAULT_CLOUD.get("enabled") is False,
      "deleting a customer's cloud copy is not a default")
pol = validate_policy({"name": "p", "raw": {"keep": "2d"},
                        "cloud": {"enabled": True, "keep": "30d"}})
cloud = pol.get("cloud") or {}
check("a policy carries the cloud block",
      bool(cloud.get("enabled")) and int(cloud.get("keep_s") or 0) == 30 * 86400,
      "keep=%s keep_s=%s" % (cloud.get("keep"), cloud.get("keep_s")))
try:
    validate_policy({"name": "p", "raw": {"keep": "2d"},
                      "cloud": {"enabled": True, "keep": "0s"}})
    check("  turning it on without a keep is refused", False, "it was accepted")
except Exception as exc:
    check("  turning it on without a keep is refused", "keep" in str(exc).lower(),
          str(exc)[:90])

store_src = io.open(os.path.join(ROOT, "backend", "app", "services", "app_store.py"),
                    encoding="utf-8", errors="replace").read()
prune = store_src.split("def prune_cloud_historian", 1)[-1][:2600]
check("every cloud delete is scoped to one tenant",
      "tenant_id = :tenant" in prune,
      "edges can share a Supabase database; an unscoped prune deletes "
      "someone else's history")
check("  and is batched, not one long transaction",
      "ctid IN (" in prune and "LIMIT :batch" in prune,
      "a multi-million-row prune must not lock a live table")
check("  and never raises into the maintenance pass",
      "return out" in prune and "except Exception" in prune,
      "an unreachable cloud must not fail local retention")

print()
print("[both are reachable in the settings]")
ui = io.open(os.path.join(ROOT, "frontend", "src", "components", "Retention",
                          "RetentionPanel.jsx"), encoding="utf-8", errors="replace").read()
check("cloud deletion has a switch",
      "Also delete old data from the cloud database" in ui and "d.cloud" in ui)
check("  with a keep field that only matters when it is on",
      'disabled={!draft.cloud?.enabled}' in ui)
# The retention router is mounted under /api/app-store. A call built from
# getApiBase() + /api/retention 404s, and the card would render "failed" with
# a perfectly healthy backend behind it.
api_js = io.open(os.path.join(ROOT, "frontend", "src", "api.js"),
                 encoding="utf-8", errors="replace").read()
block = api_js.split("export async function getStorageStatus", 1)[-1][:600]
check("the size card calls the route that exists",
      "/api/app-store/retention/v2/storage" in block,
      "the retention router's prefix is /api/app-store, like every other call "
      "in that family")

check("reclaiming disk has a switch and a threshold",
      "Reclaim disk automatically after cleanup" in ui
      and "auto_compact_free_pct" in ui)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
