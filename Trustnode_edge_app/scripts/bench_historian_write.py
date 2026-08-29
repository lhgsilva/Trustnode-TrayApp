# -*- coding: utf-8 -*-
"""What actually makes the historian write slow - measured, not argued.

2026-08-28. The live install logs "v2-writer slow HISTORIAN flush: 1 cycle(s)
in 2000-9000 ms (local DB lagging)" about once a minute. Its store is 13.4 GB
holding 4.7 days (~2.85 GB/day) with FIVE indexes on `historian_readings`, a
2 MB page cache (`cache_size = -2000`) and `synchronous = FULL`.

Rather than assert which of those matters, build a store with the SAME schema,
grow it until the indexes no longer fit in cache, then append at the live shape
(one commit per cycle, one row per tag) under each configuration and time it.

Run:  python scripts/bench_historian_write.py [--rows 2000000] [--cycles 120]

Nothing here touches the live database. It builds its own in a temp directory
and deletes it, so it is safe to run on the collecting machine - though it does
use the disk hard, so prefer a quiet moment.
"""
from __future__ import annotations

import argparse
import os
import random
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time

# The live table, verbatim, including every index on it. The point of the
# benchmark is the write amplification those indexes cause, so an approximation
# of them would measure the wrong thing.
DDL = """
CREATE TABLE historian_readings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL DEFAULT 'default',
  ts_utc TEXT NOT NULL,
  gateway_id TEXT NULL,
  gateway_name TEXT NULL,
  device_name TEXT NULL,
  plc_ip TEXT NULL,
  database_name TEXT NULL,
  tag_name TEXT NOT NULL,
  value REAL NULL,
  value_text TEXT NULL,
  data_type TEXT NULL,
  quality INTEGER NULL,
  quality_label TEXT NULL,
  source TEXT NULL,
  created_utc TEXT NOT NULL
);
"""
INDEXES = {
    "idx_hist_tenant_ts":
        "CREATE INDEX idx_hist_tenant_ts ON historian_readings(tenant_id, ts_utc DESC)",
    "idx_hist_tenant_gwid_tag_ts":
        "CREATE INDEX idx_hist_tenant_gwid_tag_ts ON historian_readings(tenant_id, gateway_id, tag_name, ts_utc DESC)",
    "idx_hist_tenant_gwname_tag_ts":
        "CREATE INDEX idx_hist_tenant_gwname_tag_ts ON historian_readings(tenant_id, gateway_name, tag_name, ts_utc DESC)",
    "idx_hist_tenant_tag_ts":
        "CREATE INDEX idx_hist_tenant_tag_ts ON historian_readings(tenant_id, tag_name, ts_utc DESC)",
    "idx_hist_tenant_gw_ts":
        "CREATE INDEX idx_hist_tenant_gw_ts ON historian_readings(tenant_id, gateway_id, ts_utc DESC)",
}

# The live shape: three gateways, 152 tags between them, one row per tag per
# second, committed once per cycle.
GATEWAYS = [("gw-plc", "PLC", 49), ("gw-ifm", "IFM", 16), ("EM1", "EM1", 87)]
TAGS = [(gid, gname, "Tag%03d" % i)
        for gid, gname, n in GATEWAYS for i in range(n)]
TENANT = "tenant-cust-e5916328"

COLS = ("tenant_id, ts_utc, gateway_id, gateway_name, device_name, plc_ip, "
        "database_name, tag_name, value, value_text, data_type, quality, "
        "quality_label, source, created_utc")
PLACEHOLDERS = ",".join(["?"] * 15)


def rows_for(ts: str):
    return [(TENANT, ts, gid, gname, "", "192.168.10.240", "", tag,
             random.random() * 100, None, "REAL", 192, "GOOD", "bench", ts)
            for gid, gname, tag in TAGS]


def build_base(path: str, target_rows: int) -> None:
    """Grow a store past the point where its indexes fit in any cache."""
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=OFF")       # build fast; measured later
    con.execute("PRAGMA cache_size=-262144")
    con.executescript(DDL)
    written = 0
    t0 = time.time()
    day = 0
    while written < target_rows:
        batch = []
        for sec in range(600):                   # ten minutes of data per txn
            ts = "2026-08-%02d %02d:%02d:%02d.000" % (
                10 + day // 86400, (sec // 3600) % 24, (sec // 60) % 60, sec % 60)
            batch.extend(rows_for(ts))
        con.executemany(
            "INSERT INTO historian_readings (%s) VALUES (%s)" % (COLS, PLACEHOLDERS),
            batch)
        con.commit()
        written += len(batch)
        day += 600
        if written % 500000 < len(batch):
            print("    %,d rows (%.0fs)".replace(",d", "d") % (written, time.time() - t0),
                  flush=True)
    # Indexes are created AFTER the bulk load, exactly as a real store would
    # have grown them - creating them first would measure a different thing.
    print("    building indexes...", flush=True)
    for sql in INDEXES.values():
        con.executescript(sql)
    con.commit()
    con.close()


def _open(path: str, cache_kb: int, synchronous: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=10.0)
    con.execute("PRAGMA synchronous=%s" % synchronous)
    con.execute("PRAGMA cache_size=-%d" % cache_kb)
    return con


def measure(path: str, cache_kb: int, synchronous: str, drop: tuple[str, ...],
            cycles: int, reconnect: bool = False) -> dict:
    """`reconnect=True` reproduces what the app does today.

    `append_historian_rows` runs `with self._connect() as conn:` - a FRESH
    connection per flush - so the page cache is built and thrown away every
    cycle and each flush re-reads the index pages it needs from disk. That is
    a different, and much slower, thing from the same work on a warm handle.
    """
    con = _open(path, cache_kb, synchronous)
    con.execute("PRAGMA journal_mode=WAL")
    for name in drop:
        con.execute("DROP INDEX IF EXISTS %s" % name)
    con.commit()
    if reconnect:
        con.close()

    sql = "INSERT INTO historian_readings (%s) VALUES (%s)" % (COLS, PLACEHOLDERS)
    times = []
    for i in range(cycles):
        ts = "2026-09-01 %02d:%02d:%02d.000" % ((i // 3600) % 24, (i // 60) % 60, i % 60)
        batch = rows_for(ts)
        t0 = time.perf_counter()
        if reconnect:
            con = _open(path, cache_kb, synchronous)
        con.executemany(sql, batch)
        con.commit()                              # one commit per cycle, as live
        if reconnect:
            con.close()
        times.append((time.perf_counter() - t0) * 1000.0)
    if not reconnect:
        con.close()
    times.sort()
    return {
        "median_ms": statistics.median(times),
        "p95_ms": times[int(len(times) * 0.95) - 1],
        "max_ms": times[-1],
        "rows_per_s": (len(TAGS) * 1000.0) / max(0.001, statistics.median(times)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=2_000_000,
                    help="rows in the base store before measuring")
    ap.add_argument("--cycles", type=int, default=120,
                    help="cycles (commits) to time per configuration")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="tn-bench-hist-")
    base = os.path.join(tmp, "base.db")
    print("TrustNode - what makes the historian write slow")
    print("  %d tags per cycle, one commit per cycle (the live shape)" % len(TAGS))
    print("  building a %d-row store in %s ..." % (args.rows, tmp), flush=True)
    t0 = time.time()
    build_base(base, args.rows)
    print("  built in %.0fs, %.0f MB" % (time.time() - t0, os.path.getsize(base) / 1e6))
    print()

    # Each configuration gets its own copy so none inherits another's cache
    # state or index set.
    DROP_NAME = ("idx_hist_tenant_gwname_tag_ts",)
    DROP_THREE = ("idx_hist_tenant_gwname_tag_ts", "idx_hist_tenant_tag_ts",
                  "idx_hist_tenant_ts")
    configs = [
        # The real baseline: a new connection per flush, so the cache is cold
        # every cycle and `synchronous` reverts to the FULL default.
        ("AS SHIPPED (new conn/flush, 2 MB, FULL)",  2_000,   "FULL",  (),  True),
        ("  + synchronous=NORMAL (the intent)",      2_000, "NORMAL",  (),  True),
        ("  + 128 MB cache, still per-flush",      131_072, "NORMAL",  (),  True),
        ("KEEP THE CONNECTION (128 MB, NORMAL)",   131_072, "NORMAL",  (), False),
        ("  + drop the gateway-name index",        131_072, "NORMAL", DROP_NAME, False),
        ("  + keep only 2 indexes",                131_072, "NORMAL", DROP_THREE, False),
    ]

    print("  %-42s %10s %10s %10s %12s" % ("configuration", "median", "p95", "max", "rows/s"))
    baseline = None
    for idx, (label, cache_kb, sync, drop, reconn) in enumerate(configs):
        path = os.path.join(tmp, "c%d.db" % idx)
        shutil.copyfile(base, path)
        res = measure(path, cache_kb, sync, drop, args.cycles, reconnect=reconn)
        if baseline is None:
            baseline = res["median_ms"]
        speed = baseline / max(0.001, res["median_ms"])
        print("  %-42s %8.1fms %8.1fms %8.1fms %10.0f   %.1fx" % (
            label, res["median_ms"], res["p95_ms"], res["max_ms"],
            res["rows_per_s"], speed))
        try:
            os.remove(path)
            for suf in ("-wal", "-shm"):
                if os.path.exists(path + suf):
                    os.remove(path + suf)
        except Exception:
            pass

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("  NOTE ON synchronous=NORMAL: with WAL it cannot corrupt the database.")
    print("  A power cut can lose the most recent commit(s) - seconds of data that")
    print("  the store-and-forward outbox re-sends anyway. FULL fsyncs on EVERY")
    print("  commit, which at one commit per second is one disk sync per second.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
