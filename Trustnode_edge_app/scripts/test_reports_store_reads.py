# -*- coding: utf-8 -*-
"""Reading report templates must not queue behind the historian's writer.

2026-09-30, reported: "templates are not loading and configuration of the app
when is starting up also not working, need to be reloaded all the time", with
the Reporting page showing

    Failed to load templates: signal is aborted without reason

ReportsStore shares ONE sqlite file with the historian - on the reporting
install that file is gigabytes and under constant write load - and every call
used a READ/WRITE connection with a 10s busy timeout. A template list could
therefore sit behind the writer for longer than the UI's 12s request budget,
so the page gave up. Reloading worked only because it re-raced the writer.

Reads now use a read-only connection capped at 3s, the rule
AppStore._connect_readonly already applies for the same reason.
"""
from __future__ import annotations

import io
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ.setdefault("TRUSTNODE_SKIP_DOTENV", "1")
_TMP = tempfile.mkdtemp(prefix="tn-rstore-")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "s.db")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:160]) if detail else ""))
    if not ok:
        FAILS.append(name)


print("TrustNode - report reads do not wait on the writer")
print()

SRC = io.open(os.path.join(ROOT, "backend", "app", "services", "reports_store.py"),
              encoding="utf-8", errors="replace").read()

print("[every pure read uses the read-only handle]")
READS = ("list_templates", "get_template", "list_schedules",
         "list_enabled_schedules_all_tenants", "get_schedule",
         "list_generated", "get_generated")
bodies = {}
for m in re.finditer(r"\n    def ([a-z_0-9]+)\(", SRC):
    name = m.group(1)
    start = m.end()
    nxt = SRC.find("\n    def ", start)
    bodies[name] = SRC[start:nxt if nxt > 0 else len(SRC)]
for name in READS:
    body = bodies.get(name, "")
    check("  %-34s reads read-only" % name,
          "self._connect_read()" in body and "self._connect() as conn" not in body,
          "a read/write handle can block on the writer for 10s")

print()
print("[writes still use the writable handle]")
for name in ("save_template", "insert_generated", "set_cycle_state"):
    body = bodies.get(name, "")
    if not body:
        continue
    check("  %-34s still writable" % name,
          "self._connect_read()" not in body,
          "a read-only connection cannot write - every save would fail")

print()
print("[the read-only handle is genuinely read-only and short-waiting]")
fn = bodies.get("_connect_read", "")
check("opened with mode=ro", "mode=ro" in fn and "uri=True" in fn,
      "without uri=True sqlite treats the whole string as a filename")
check("  capped well under the UI's 12s budget",
      bool(re.search(r"timeout=3(\.0)?", fn)), fn[:80])
check("  falls back when the file does not exist yet",
      "self._connect()" in fn,
      "mode=ro cannot create a database, so a fresh install must not 500")

print()
print("[it behaves: a real read-only connection refuses to write]")
from app.services.reports_store import ReportsStore            # noqa: E402
store = ReportsStore()
with store._connect() as c:
    c.execute("CREATE TABLE IF NOT EXISTS probe (id TEXT)")
    c.commit()
ro = store._connect_read()
try:
    ro.execute("INSERT INTO probe (id) VALUES ('x')")
    ro.commit()
    refused = False
except sqlite3.OperationalError:
    refused = True
finally:
    ro.close()
check("a write through the read handle is refused", refused,
      "then it is not really read-only and can still take the write lock")

# A writer holding the lock must not stall a reader for the UI's whole budget.
with store._connect() as c:
    c.execute("BEGIN EXCLUSIVE")
    t0 = time.time()
    try:
        r = store._connect_read()
        r.execute("SELECT count(*) FROM probe").fetchone()
        r.close()
        waited = time.time() - t0
        blocked = False
    except sqlite3.OperationalError:
        waited = time.time() - t0
        blocked = True
    c.rollback()
check("under an exclusive writer the reader gives up fast",
      waited < 6.0,
      "waited %.1fs (blocked=%s); the UI aborts at 12s" % (waited, blocked))

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
