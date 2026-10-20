# -*- coding: utf-8 -*-
"""Cloud retention stays off until asked, and never deletes unscoped.

2026-10-20, requested: "we need a way to delete data from the cloud also
using the retention set locally ... so we dont consume the storage".

The policy already had a `cloud` block. `_normalise_policy` validated it and
NOTHING read it, so the cloud grew without bound. Measured that day:

    historian_readings   57 GB    92,863,184 rows
    plc_readings         51 GB   151,786,976 rows
    ------------------------------------------- 116 GB, 55 days of data

Both tables carry the same readings - one is the mirror every surface reads,
the other is the operator-configurable sink - so the cloud stored every
sample twice and bounded neither.

This deletes from a database the customer pays for and cannot undo, so the
gates matter more than the deleting:

  * OFF unless the active policy says otherwise. A default that deletes is
    not a default.
  * A WINDOW IS REQUIRED. `cloud.enabled` with no keep window must refuse,
    not treat "unset" as "everything".
  * dry_run must not delete. The dry run is what an operator looks at before
    agreeing, so it must never be the thing that acts.
  * TENANT SCOPED. The cloud database is shared between customers and the
    edge connects with a role that BYPASSES RLS, so the WHERE clause is the
    only thing protecting the others (CLAUDE.md 1b).

The SQL is Postgres and is exercised against the real cloud by hand; what is
asserted here is the gating and the shape of the statements, which is where
an accident would come from. No cloud connection is made - `_prune_cloud`
returns before touching one whenever these gates hold.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

TMP = tempfile.mkdtemp(prefix="tn-cloudret-")
os.environ.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=TMP,
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:190]) if detail else ""))
    if not ok:
        FAILS.append(name)
    return bool(ok)


print("TrustNode - cloud retention is off until asked, and always scoped")
print()

from app.services.retention_engine import RetentionEngine  # noqa: E402

eng = RetentionEngine(os.path.join(TMP, "store.db"), backup_dir_fn=lambda: TMP)

print("[1. it does nothing unless the policy turns it on]")
r = eng._prune_cloud({}, dry_run=False)
check("no cloud block at all -> skipped", bool(r.get("skipped")), r)
r = eng._prune_cloud({"cloud": {"enabled": False, "keep_s": 86400}}, dry_run=False)
check("  enabled=False -> skipped", bool(r.get("skipped")), r)
check("    and it says so in words", "off" in str(r.get("skipped")).lower(), r)

print()
print("[2. 'on' with no window must refuse, not delete everything]")
r = eng._prune_cloud({"cloud": {"enabled": True, "keep_s": 0}}, dry_run=False)
check("enabled with keep_s=0 -> skipped", bool(r.get("skipped")), r)
check("  and nothing was attempted", not r.get("tables"), r)
r = eng._prune_cloud({"cloud": {"enabled": True}}, dry_run=False)
check("  enabled with no keep at all -> skipped", bool(r.get("skipped")), r)

print()
print("[3. the SQL is scoped and bounded]")
SRC = io.open(os.path.join(ROOT, "backend", "app", "services",
                           "retention_engine.py"), encoding="utf-8").read()
seg = SRC[SRC.index("def _prune_cloud"):SRC.index("# -- other data classes")]
check("every delete filters by tenant_id",
      "WHERE tenant_id = :t AND ts_utc < :c" in seg,
      "unscoped, this takes another customer's history with it")
check("  deletes are batched with a LIMIT",
      "ORDER BY id LIMIT :n" in seg,
      "one DELETE over 60M rows holds locks and bloats WAL")
check("  and capped per run",
      "budget" in seg and "max_batches" in seg,
      "an uncapped pass would run until the statement timeout kills it "
      "mid-way, every time")
check("  there is a wall-clock deadline too",
      "deadline" in seg)
check("  dry_run counts and returns before any DELETE",
      seg.index("if dry_run:") < seg.index("DELETE FROM"),
      "the dry run is what the operator reads before agreeing; it must "
      "never be the thing that acts")

print()
print("[4. the dry run cannot silently under-report]")
check("the count is bounded by a cap",
      "LIMIT :cap" in seg and "capped" in seg,
      "a plain COUNT(*) over 60M rows hits the statement timeout and "
      "returns ZERO - a dry run that says 'nothing to do' about 60 "
      "million rows, which is exactly what the first version did")

print()
print("[5. it is honest about what deleting does NOT do]")
check("it says rows are freed, not disk",
      "VACUUM FULL" in seg,
      "Postgres returns deleted space to the TABLE, not the disk; an "
      "operator told 'reclaimed 60M rows' will expect the bill to drop")

print()
print("[6. both duplicate tables are managed]")
check("historian_readings and plc_readings are both named",
      'CLOUD_PRUNE_TABLES = ("historian_readings", "plc_readings")' in SRC,
      "they hold the same readings; pruning one leaves the other growing")
check("  and each is checked to exist first",
      "information_schema.tables" in SRC,
      "DELETE against a missing table aborts the whole pass")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
