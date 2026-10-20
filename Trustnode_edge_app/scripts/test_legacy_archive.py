# -*- coding: utf-8 -*-
"""Folding a legacy store into buckets must keep the numbers and the scope.

2026-10-20. Seven point eight GB of the data directory was files nothing
writes any more. Deleting them looked obvious and would have destroyed five
weeks of history: the live store's earliest surviving data of any kind was
2026-08-23, and trustnode_edge.db ended 2026-08-21.

archive_legacy_stores.py folds them into the 1-hour tier the operator's own
policy keeps for a year - 4.28 GB becomes about 15 MB - and only then allows
the file to go. That is only safe if three things hold, so they are asserted
here on data whose every value is known:

  * the arithmetic survives. n, sum, min, max, first and last per bucket must
    match the rows that went in. A bucket that quietly drops readings is
    worse than keeping the file, because it looks like success.
  * a tag that cannot be attributed to exactly ONE gateway is NOT guessed at.
    The legacy table has no gateway_id; 46 of 49 real tags mapped cleanly and
    3 did not. Attributing those 3 to the busiest gateway would fabricate
    history on a series an operator reads as measured.
  * delete refuses unless the manifest says verified. The whole point is that
    the file outlives its own archive until the archive is proven.

Scratch directories only. Never opens the live workspace.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
import calendar
import datetime as dt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:190]) if detail else ""))
    if not ok:
        FAILS.append(name)


def load_tool():
    p = os.path.join(ROOT, "scripts", "archive_legacy_stores.py")
    spec = importlib.util.spec_from_file_location("archive_tool", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


print("TrustNode - legacy stores fold into buckets without losing anything")
print()

TMP = tempfile.mkdtemp(prefix="tn-arch-")
os.environ.setdefault("TRUSTNODE_SKIP_DOTENV", "1")
tool = load_tool()

TENANT = "tenant-cust-test"
GW = "gw-real-0001"
BASE = dt.datetime(2026, 7, 16, 0, 0, 0)

# ---- a target store: rollup table + a tag->gateway source of truth
store = os.path.join(TMP, "trustnode_app_store.db")
con = sqlite3.connect(store)
con.execute("""CREATE TABLE historian_rollup (
 resolution_s INTEGER NOT NULL, tenant_id TEXT NOT NULL, gateway_id TEXT NOT NULL,
 tag_name TEXT NOT NULL, bucket_ms INTEGER NOT NULL, n INTEGER NOT NULL DEFAULT 0,
 sum_v REAL, sumsq_v REAL, min_v REAL, max_v REAL, first_v REAL, first_ms INTEGER,
 last_v REAL, last_ms INTEGER, last_text TEXT, q_min INTEGER, q_max INTEGER,
 q_bad_n INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY (resolution_s, tenant_id, gateway_id, tag_name, bucket_ms)
) WITHOUT ROWID""")
con.execute("""CREATE TABLE historian_readings (
 id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, gateway_id TEXT,
 tag_name TEXT NOT NULL, value REAL, quality INTEGER, value_text TEXT,
 tenant_id TEXT NOT NULL DEFAULT 'default')""")
# "Temp" belongs to exactly one gateway -> mappable.
# "Shared" belongs to two -> must NOT be guessed.
con.execute("INSERT INTO historian_readings (ts_utc,gateway_id,tag_name,value,"
            "quality,tenant_id) VALUES ('2026-09-01 00:00:00',?,'Temp',1,192,?)",
            (GW, TENANT))
for g in (GW, "gw-other-0002"):
    con.execute("INSERT INTO historian_readings (ts_utc,gateway_id,tag_name,value,"
                "quality,tenant_id) VALUES ('2026-09-01 00:00:00',?,'Shared',1,192,?)",
                (g, TENANT))
con.commit()
con.close()

# ---- a legacy file with KNOWN values: 2 hours x 2 tags
legacy = os.path.join(TMP, "trustnode_edge.db")
lc = sqlite3.connect(legacy)
lc.execute("""CREATE TABLE historian_readings (
 id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, tag_name TEXT NOT NULL,
 value REAL, quality INTEGER, source TEXT, value_text TEXT)""")
expect = {}
for hour in (0, 1):
    for tag in ("Temp", "Shared", "Orphan"):
        vals = []
        for minute in range(0, 60, 10):
            v = float(hour * 100 + minute + (0 if tag == "Temp" else 7))
            ts = (BASE + dt.timedelta(hours=hour, minutes=minute)
                  ).strftime("%Y-%m-%d %H:%M:%S")
            lc.execute("INSERT INTO historian_readings (ts_utc,tag_name,value,quality)"
                       " VALUES (?,?,?,?)", (ts, tag, v, 192))
            vals.append(v)
        expect[(tag, hour)] = vals
lc.commit()
lc.close()

src = {"file": "trustnode_edge.db", "path": legacy,
       "readings": sum(len(v) for v in expect.values()),
       "span": ["2026-07-16 00:00:00", "2026-07-16 01:50:00"],
       "archivable": True}

print("[1. it maps tags only where the answer is certain]")
mapped, ambiguous = tool.tag_gateway_map(store)
check("a tag owned by one gateway is mapped", mapped.get("Temp") == GW,
      mapped.get("Temp"))
check("  a tag owned by two gateways is NOT mapped", "Shared" not in mapped,
      "guessing here fabricates history on a real series")
check("  and it is reported as ambiguous", "Shared" in ambiguous, ambiguous)

print()
print("[2. the fold keeps the arithmetic]")
res = tool.archive_file(store, src, TENANT, 3600, apply=True)
check("it ran and verified", res.get("verified") is True,
      "missing=%s short=%s" % (res.get("missing_buckets"), res.get("short_buckets")))
check("  every source bucket is accounted for",
      res.get("source_buckets") == 6, res.get("source_buckets"))

con = sqlite3.connect(store)
bad = []
for (tag, hour), vals in expect.items():
    gw = GW if tag == "Temp" else tool.UNMAPPED_GATEWAY
    # UTC, not local. SQLite's strftime('%s', ts_utc) reads the stored
    # string as UTC; datetime.timestamp() would apply this machine's offset
    # and look for a bucket an hour away on any non-UTC box. Same trap as
    # the Data Export window (CLAUDE.md 8).
    bucket = (calendar.timegm((BASE + dt.timedelta(hours=hour)).timetuple())
              // 3600 * 3600 * 1000)
    row = con.execute(
        "SELECT n, sum_v, min_v, max_v, first_v, last_v FROM historian_rollup "
        "WHERE resolution_s=3600 AND tenant_id=? AND gateway_id=? AND tag_name=? "
        "AND bucket_ms=?", (TENANT, gw, tag, bucket)).fetchone()
    if not row:
        bad.append("%s/h%s missing" % (tag, hour))
        continue
    want = (len(vals), sum(vals), min(vals), max(vals), vals[0], vals[-1])
    got = (int(row[0]), float(row[1]), float(row[2]), float(row[3]),
           float(row[4]), float(row[5]))
    if any(abs(a - b) > 1e-6 for a, b in zip(want, got)):
        bad.append("%s/h%s want=%s got=%s" % (tag, hour, want, got))
check("n, sum, min, max, first and last all survive", not bad, "; ".join(bad[:3]))

print()
print("[3. the uncertain tags are labelled, not attributed]")
for tag in ("Shared", "Orphan"):
    n = con.execute("SELECT COUNT(*) FROM historian_rollup WHERE tag_name=? "
                    "AND gateway_id=?", (tag, GW)).fetchone()[0]
    check("  %-7s never lands on the real gateway" % tag, n == 0, n)
    n2 = con.execute("SELECT COUNT(*) FROM historian_rollup WHERE tag_name=? "
                     "AND gateway_id=?", (tag, tool.UNMAPPED_GATEWAY)).fetchone()[0]
    check("    but it IS kept, under the labelled id", n2 > 0, n2)
check("  the run says which tags it could not attribute",
      set(res.get("unmapped_tags") or []) == {"Shared", "Orphan"},
      res.get("unmapped_tags"))
con.close()

print()
print("[4. the saving is the point]")
# Row count, not file size: at 36 readings the page overhead dwarfs the data
# and a byte comparison says nothing. Rows are the property that scales -
# on the real file this is 10,556,079 readings becoming 42,336 buckets.
check("far fewer rows come out than went in",
      res["source_buckets"] < src["readings"],
      "%s buckets from %s readings (%.0fx)"
      % (res["source_buckets"], src["readings"],
         src["readings"] / max(1, res["source_buckets"])))

print()
print("[5. nothing is deleted on an unverified archive]")
SRC = open(os.path.join(ROOT, "scripts", "archive_legacy_stores.py"),
           encoding="utf-8").read()
check("delete is gated on the manifest",
      'if c.get("archivable") and not (r and r.get("verified")):' in SRC,
      "an unverified archive must leave the file alone")
check("  and even then it RENAMES rather than removes",
      'shutil.move(c["path"], c["path"] + ".todelete")' in SRC
      and "os.remove(" not in SRC,
      "the operator does the final removal, once they are satisfied")
check("  --apply is required to write anything",
      'if a.apply and live and not a.yes:' in SRC)
check("  the aggregation is the engine's, not a copy",
      "from app.services.retention_engine import _ROLLUP_FROM_RAW" in SRC,
      "a second copy of the bucket maths is how two tiers disagree")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
