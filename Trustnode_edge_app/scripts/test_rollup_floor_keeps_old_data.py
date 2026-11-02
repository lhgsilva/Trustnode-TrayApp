# -*- coding: utf-8 -*-
"""A chart over a window that starts before the rollup does loses nothing.

2026-11-02 review of the backfill fix: the first rollup pass is bounded to the
last week, so a chart or report over an older range has buckets only for the
LATER part of its window. The rollup/live merge read live only the TAIL after
the newest bucket - the head before the oldest bucket was never fetched, and
such a chart would have started a week late. The head is now read live too.

    python scripts/test_rollup_floor_keeps_old_data.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="tn_floor_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-floor"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-66s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def _ts(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) + ".000"


def main() -> int:
    print("a window that starts before the rollup keeps its head")
    from app.state import app_store, retention_engine
    from app.tenant import set_current_tenant
    set_current_tenant("tenant-cust-floor")
    from app.services import retention_engine as re_mod

    now = time.time()
    rows = []
    for i in range(0, 3 * 3600, 5):
        for tag in ("A", "B"):
            rows.append({"ts_utc": _ts(now - 3 * 3600 + i), "gateway_id": "gw1", "gateway_name": "PLC",
                         "device_name": "d", "tag_name": tag, "value": float(i % 50), "quality": 192,
                         "quality_label": "GOOD", "source": "test"})
    app_store.append_historian_rows(rows)

    # Materialise only the last hour (the floor), as a customer's first pass does.
    saved = re_mod.NO_POLICY_BACKFILL_S
    re_mod.NO_POLICY_BACKFILL_S = 3600
    try:
        for _ in range(6):
            retention_engine.rollup_once(reason="test")
    finally:
        re_mod.NO_POLICY_BACKFILL_S = saved
    import sqlite3
    con = sqlite3.connect(os.environ["TRUSTNODE_APP_STORE_PATH"])
    oldest = con.execute("SELECT MIN(bucket_ms) FROM historian_rollup WHERE resolution_s=60").fetchone()[0]
    n60 = con.execute("SELECT COUNT(*) FROM historian_rollup WHERE resolution_s=60").fetchone()[0]
    con.close()
    check("the rollup covers only the last hour", n60 > 0 and oldest is not None
          and oldest >= (now - 3600 - 120) * 1000, "%d rows, oldest %s" % (n60, oldest))

    a, b = _ts(now - 3 * 3600), _ts(now)
    out = app_store.get_historian_agg_rows(bucket="minute", from_utc=a, to_utc=b, gateway="gw1", tag="A")
    stamps = sorted(r["ts"] for r in out)
    from_rollup = [r for r in out if r.get("source") == "historian_rollup"]
    check("a 3-hour chart still has ~180 minute points", 170 <= len(out) <= 182, "%d" % len(out))
    check("  its first point is at the start of the window, not an hour late",
          stamps and stamps[0] <= _ts(now - 3 * 3600 + 120), stamps[:1])
    check("  the later part comes from the rollup, the head from live",
          len(from_rollup) >= 50 and len(out) - len(from_rollup) >= 100,
          "rollup=%d live=%d" % (len(from_rollup), len(out) - len(from_rollup)))
    keys = [(r["ts"], r["tag"]) for r in out]
    check("  and no minute is served twice", len(keys) == len(set(keys)))

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
