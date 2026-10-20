# -*- coding: utf-8 -*-
"""Every retention dial the page offers must actually delete something.

2026-10-20. Four keys sat in DEFAULT_POLICY, were rendered on the Backup and
Retention page, and were read by NOTHING:

    telemetry_cycles_keep   outbox_sent_keep
    reports_files_keep      reports_metadata_keep

The cost was not theoretical. `telemetry_cycles_keep` said 7 days while
trustnode_telemetry.db held 1,082,929 cycle rows spanning THREE MONTHS -
6.08 GB with zero free pages, meaning not one row had ever been deleted. It
was invisible because the engine only ever addressed its own database and
telemetry lives in a separate file.

A dial that changes nothing is worse than no dial: the operator believes
their data is bounded, sizes a disk for it, and is wrong.

What this pins:

  * each key deletes rows older than its cutoff, in the right table,
  * "forever" and an unset key delete NOTHING - a cleanup that ignores its
    own off switch is how an archive disappears,
  * a PENDING outbox row survives. `outbox_sent_keep` means delivered work,
    not queued work; deleting a pending row drops a write nobody will retry.

Scratch databases only. The live workspace is never opened.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ.setdefault("TRUSTNODE_SKIP_DOTENV", "1")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:190]) if detail else ""))
    if not ok:
        FAILS.append(name)


TMP = tempfile.mkdtemp(prefix="tn-prune-")
STORE = os.path.join(TMP, "trustnode_app_store.db")
TELEM = os.path.join(TMP, "trustnode_telemetry.db")

OLD = (dt.datetime.utcnow() - dt.timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
NEW = (dt.datetime.utcnow() - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

con = sqlite3.connect(STORE)
con.executescript("""
CREATE TABLE app_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL,
  level TEXT, category TEXT, message TEXT, created_utc TEXT,
  tenant_id TEXT NOT NULL DEFAULT 'default');
CREATE TABLE config_audit (rowid_ INTEGER, created_utc TEXT);
CREATE TABLE cp_security_audit_log (rowid_ INTEGER, created_utc TEXT);
CREATE TABLE retention_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, run_utc TEXT,
  dry_run INTEGER, status TEXT, details_json TEXT);
CREATE TABLE sync_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT,
  entity_key TEXT, payload_json TEXT, status TEXT NOT NULL DEFAULT 'pending',
  retries INTEGER DEFAULT 0, last_error TEXT, created_utc TEXT, updated_utc TEXT);
CREATE TABLE generated_reports (id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT, name TEXT, created_utc TEXT);
""")
for ts in (OLD, NEW):
    con.execute("INSERT INTO app_logs (ts_utc,level,category,message,created_utc) "
                "VALUES (?,'INFO','t','m',?)", (ts, ts))
    con.execute("INSERT INTO retention_runs (run_utc,dry_run,status,details_json) "
                "VALUES (?,0,'ok','{}')", (ts,))
    con.execute("INSERT INTO generated_reports (tenant_id,name,created_utc) "
                "VALUES ('t','r',?)", (ts,))
    con.execute("INSERT INTO sync_outbox (domain,entity_key,payload_json,status,"
                "created_utc) VALUES ('d','k','{}','sent',?)", (ts,))
# The one that must survive: old, but NOT delivered.
con.execute("INSERT INTO sync_outbox (domain,entity_key,payload_json,status,"
            "created_utc) VALUES ('d','pending-key','{}','pending',?)", (OLD,))
con.commit()
con.close()

tc = sqlite3.connect(TELEM)
tc.executescript("""
CREATE TABLE telemetry_samples_raw (edge_record_id TEXT PRIMARY KEY,
  tenant_id TEXT, sample_ts_utc TEXT, tags_json TEXT);
CREATE TABLE ingest_audit_log_local (id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL, action TEXT);
""")
for i, ts in enumerate((OLD, NEW)):
    tc.execute("INSERT INTO telemetry_samples_raw VALUES (?,?,?,?)",
               ("rec-%d" % i, "t", ts, "{}"))
    tc.execute("INSERT INTO ingest_audit_log_local (ts_utc,action) VALUES (?,'c')",
               (ts,))
tc.commit()
tc.close()

print("TrustNode - every retention dial actually prunes")
print()

from app.services.retention_engine import RetentionEngine  # noqa: E402

eng = RetentionEngine(STORE, backup_dir_fn=lambda: TMP)


def counts():
    a = sqlite3.connect(STORE)
    b = sqlite3.connect(TELEM)
    out = {}
    for t in ("app_logs", "retention_runs", "generated_reports", "sync_outbox"):
        out[t] = a.execute("SELECT COUNT(*) FROM [%s]" % t).fetchone()[0]
    out["sync_outbox_pending"] = a.execute(
        "SELECT COUNT(*) FROM sync_outbox WHERE status='pending'").fetchone()[0]
    for t in ("telemetry_samples_raw", "ingest_audit_log_local"):
        out[t] = b.execute("SELECT COUNT(*) FROM [%s]" % t).fetchone()[0]
    a.close(); b.close()
    return out


print("[1. an 'off' policy deletes nothing]")
before = counts()
eng._prune_other_data({"other_data": {
    "app_logs_keep": "forever", "retention_runs_keep": "forever",
    "outbox_sent_keep": "forever", "reports_metadata_keep": "forever",
    "telemetry_cycles_keep": "forever"}})
after_off = counts()
check("'forever' leaves every table untouched", before == after_off,
      "before=%s after=%s" % (before, after_off))
eng._prune_other_data({"other_data": {}})
check("  an unset key leaves every table untouched", counts() == before,
      "a missing dial must not mean 'delete'")

print()
print("[2. a 7-day policy prunes the old row in every table]")
res = eng._prune_other_data({"other_data": {
    "app_logs_keep": "7d", "retention_runs_keep": "7d", "audit_keep": "7d",
    "outbox_sent_keep": "7d", "reports_metadata_keep": "7d",
    "telemetry_cycles_keep": "7d"}})
after = counts()
print("      engine reported: %s" % res)
for t in ("app_logs", "retention_runs", "generated_reports",
          "telemetry_samples_raw", "ingest_audit_log_local"):
    check("  %-24s old row gone, new row kept" % t,
          after[t] == 1, "%d row(s) left (expected 1)" % after[t])

print()
print("[3. the telemetry file is a DIFFERENT database - the whole bug]")
check("the engine addresses it by path",
      "trustnode_telemetry.db" in open(
          os.path.join(ROOT, "backend", "app", "services", "retention_engine.py"),
          encoding="utf-8").read(),
      "the engine only ever addressed its own store, which is why 6.08 GB "
      "of cycle data was never pruned")
check("  and it reported what it deleted there",
      res.get("telemetry_samples_raw") == 1
      and res.get("ingest_audit_log_local") == 1, res)

print()
print("[4. pending work is never thrown away]")
check("the delivered outbox row is gone",
      after["sync_outbox"] == 2, "%d left" % after["sync_outbox"])
check("  but the PENDING one survived", after["sync_outbox_pending"] == 1,
      "deleting a pending row drops a write nobody will retry")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
