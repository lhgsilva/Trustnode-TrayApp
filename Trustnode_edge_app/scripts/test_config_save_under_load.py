# -*- coding: utf-8 -*-
"""Saving a config must not queue behind the historian writer.

2026-10-02, reported on every page: "Database connections could not be saved:
Timed out after 20s: /api/app-store/domain". The live store is 5.3 GB taking
41 rows/s, and PUT /domain did FOUR reads before writing - a version check and
three guards, each calling get_bootstrap_scoped_or_shout(), which reads EVERY
domain and takes the global lock. Behind a continuous writer each of those can
take seconds, so the save passed the UI's 20 s budget and the banner followed
the operator from page to page.

This writes continuously from one thread while saving configs from another,
and fails if a save takes anywhere near the UI's budget.
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ.setdefault("TRUSTNODE_SKIP_DOTENV", "1")
_TMP = tempfile.mkdtemp(prefix="tn-cfgload-")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "s.db")
os.environ.setdefault("TRUSTNODE_BOOT_INTEGRITY_CHECK", "never")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:160]) if detail else ""))
    if not ok:
        FAILS.append(name)


from app.state import app_store                                  # noqa: E402

DB = os.environ["TRUSTNODE_APP_STORE_PATH"]
SCOPE = "tenant-x|cust-x|edge-x"
DOMAIN = "database_configurations"

# Seed a body of history so a full-bootstrap read is not free.
con = sqlite3.connect(DB)
base = datetime.utcnow() - timedelta(hours=2)
rows = []
for i in range(120000):
    ts = (base + timedelta(milliseconds=i * 50)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    rows.append((ts, "gw-load", "tag_%d" % (i % 40), float(i % 100), ts))
con.executemany(
    "INSERT INTO historian_readings (tenant_id, ts_utc, gateway_id, tag_name,"
    " value, quality, quality_label, created_utc)"
    " VALUES ('default',?,?,?,?,192,'GOOD',?)", rows)
con.commit()
con.close()
print("TrustNode - a config save under write load")
print()
print("[%d seeded readings, a writer running throughout]" % len(rows))

stop = threading.Event()
written = {"n": 0, "err": ""}


def writer():
    """Write through the APP STORE, the way the historian actually does.

    An earlier version of this test opened its own sqlite connection, which
    bypasses app_store's global lock entirely - so it measured nothing and
    both read paths looked identical. The contention being fixed is that
    Python-level lock, so the writer has to take it.
    """
    while not stop.is_set():
        try:
            now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            app_store.append_historian_rows([
                {"ts_utc": now, "gateway_id": "gw-load", "tag_name": "tag_%d" % t,
                 "value": float(t), "quality": 192, "quality_label": "GOOD"}
                for t in range(40)])
            written["n"] += 40
        except Exception as exc:                        # noqa: BLE001
            written["err"] = str(exc)[:120]
        time.sleep(0.02)


t = threading.Thread(target=writer, daemon=True)
t.start()
time.sleep(1.0)

# Put a document in place so the guards have something to read.
app_store.upsert_domain_scoped(SCOPE, DOMAIN,
                               [{"id": "db-1", "name": "Local SQLite", "engine": "sqlite"}],
                               actor="test")

print()
print("[the read the save guards depend on]")
worst = 0.0
for i in range(12):
    t0 = time.time()
    got = app_store.get_domain_for_scope_fast(SCOPE, DOMAIN)
    dt = time.time() - t0
    worst = max(worst, dt)
    if got is None:
        check("the lock-free read returned the document", False, "attempt %d" % (i + 1))
        break
check("the lock-free read never waits on the writer", worst < 2.0,
      "worst %.2fs over 12 reads (the full-bootstrap read it replaced took "
      "seconds under this load)" % worst)

print()
print("[the property that matters: the read never takes the global lock]")
# TIMING cannot settle this. On a scratch store both paths return in
# milliseconds; the contention only appears on a multi-gigabyte store under a
# continuous writer, which cannot be reproduced here. So assert the PROPERTY
# instead - the guards reach for a read-only single-document read first, and
# the slow full-bootstrap read is only the fallback on a miss.
import inspect                                                   # noqa: E402
_rd = inspect.getsource(app_store.get_domain_for_scope_fast.__func__)
check("the reader uses a read-only connection",
      "_connect_readonly()" in _rd,
      "a read/write handle can queue behind the historian writer")
check("  and never takes the global lock",
      "self._lock" not in _rd,
      "that lock is held by the historian write path for the whole batch")
check("  it reads ONE document, not every domain",
      "WHERE scope_key = ? AND domain = ?" in _rd)

_router = io.open(os.path.join(ROOT, "backend", "app", "routers", "app_store.py"),
                  encoding="utf-8", errors="replace").read()
check("every save guard tries the lock-free read first",
      _router.count("get_domain_for_scope_fast(scope_key, domain)") == 3,
      "the blank guard, the omitted-key preserver and the secret restorer")
check("  with the vetted full read still there as a fallback",
      _router.count("get_bootstrap_scoped_or_shout(") >= 3,
      "a miss must not be treated as 'empty' - that is how configs got wiped")

print()
print("[the whole save path]")
worst_save = 0.0
for i in range(6):
    t0 = time.time()
    app_store.upsert_domain_scoped(
        SCOPE, DOMAIN,
        [{"id": "db-1", "name": "Local SQLite", "engine": "sqlite", "rev": i}],
        actor="test")
    worst_save = max(worst_save, time.time() - t0)
check("a save completes well inside the UI's 20 s budget", worst_save < 10.0,
      "worst %.2fs over 6 saves" % worst_save)
check("  and comfortably inside, not just barely", worst_save < 5.0,
      "worst %.2fs" % worst_save)

stop.set()
t.join(timeout=5)
print()
print("  writer managed %d rows during the test%s"
      % (written["n"], (" (errors: %s)" % written["err"]) if written["err"] else ""))
check("the writer was genuinely running", written["n"] > 200,
      "%d rows - with no contention this test proves nothing" % written["n"])

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
