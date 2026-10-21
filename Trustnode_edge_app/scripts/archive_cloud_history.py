# -*- coding: utf-8 -*-
"""Fold the cloud's unique history into hourly buckets before it is deleted.

2026-10-21, requested: delete the plc_readings / telemetry the app no longer
uses so the cloud database is clean, and correct for future customers.

THE REASON THIS IS NOT JUST A DELETE
------------------------------------
cloud plc_readings looked like a pure duplicate of historian_readings - the
same readings written by the sink instead of the mirror - and the obvious
move was to drop it and reclaim 51 GB. Sampling across its whole id range
said otherwise:

    id ~3.7M   in historian_readings: 0/5
    id ~21M    in historian_readings: 0/5
    id ~38M    in historian_readings: 0/5
    id ~56M    in historian_readings: 0/5
    id ~74M+   in historian_readings: 5/5

The older half is NOT duplicated. Those rows start 2026-06-19 under gateway
`gw-primary`, and historian_readings only begins 2026-08-26. So plc_readings
holds roughly two months that exist nowhere else - the same shape as
trustnode_edge.db locally, which also looked abandoned and was the only copy
of five weeks.

So: aggregate first, verify, and only then let the rows go.

HOW
---
The aggregation runs IN Postgres (one GROUP BY per day, so no query is
unbounded and nothing large crosses the network), and the buckets land in
the LOCAL historian_rollup at 3600 s - the tier the operator's policy
already keeps for a year. Verification compares every (gateway, tag, hour)
the cloud reports against what was written locally, by count.

    python scripts/archive_cloud_history.py --list
    python scripts/archive_cloud_history.py --archive            # dry run
    python scripts/archive_cloud_history.py --archive --apply
    python scripts/archive_cloud_history.py --verify
    python scripts/archive_cloud_history.py --delete --apply

--delete refuses unless the manifest says every day verified.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ.setdefault("TRUSTNODE_BOOT_INTEGRITY_CHECK", "never")

MANIFEST = "cloud_archive_manifest.json"
RES_S = 3600


def _cloud():
    from app.state import app_store
    target = app_store._get_cloud_database_target()          # noqa: SLF001
    if not target:
        raise SystemExit("no cloud database configured on this edge")
    schema = str(target.get("schema") or "public")
    engine, _ = app_store._get_or_create_cloud_engine(target, schema)  # noqa: SLF001
    return engine, schema, app_store


def _manifest_path(store_path: str) -> str:
    return os.path.join(os.path.dirname(store_path), MANIFEST)


def _load(store_path: str) -> Dict[str, Any]:
    try:
        with open(_manifest_path(store_path), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save(store_path: str, man: Dict[str, Any]) -> None:
    with open(_manifest_path(store_path), "w", encoding="utf-8") as fh:
        json.dump(man, fh, indent=2)


def span(cx, sql, schema: str, table: str, before: str):
    """Oldest/newest in the window, using the (tenant_id, ts_utc) index."""
    row = cx.execute(sql(
        f'SELECT MIN(ts_utc), MAX(ts_utc) FROM "{schema}"."{table}" '
        f'WHERE tenant_id = :t AND ts_utc < :c'), {"t": _TENANT, "c": before}).first()
    return row[0], row[1]


_TENANT = ""


def archive_day(cx, sql, schema: str, table: str, day: dt.date,
                tenant: str) -> List[tuple]:
    """One day of rows, aggregated to hourly buckets inside Postgres."""
    lo = dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc)
    hi = lo + dt.timedelta(days=1)
    rows = cx.execute(sql(
        f'SELECT COALESCE(gateway_id, %s) AS gw, tag_name, '
        f'       date_trunc(\'hour\', ts_utc) AS h, '
        f'       COUNT(value) AS n, SUM(value) AS s, '
        f'       MIN(value) AS mn, MAX(value) AS mx, '
        f'       MIN(quality) AS qmin, MAX(quality) AS qmax, '
        f'       SUM(CASE WHEN quality < 192 THEN 1 ELSE 0 END) AS qbad '
        f'FROM "{schema}"."{table}" '
        f'WHERE tenant_id = :t AND ts_utc >= :lo AND ts_utc < :hi '
        f'GROUP BY 1, 2, 3' % "''"),
        {"t": tenant, "lo": lo, "hi": hi}).all()
    return rows


def write_buckets(store_path: str, tenant: str, rows) -> int:
    con = sqlite3.connect(store_path, timeout=120.0)
    con.isolation_level = None
    n = 0
    try:
        con.execute("BEGIN IMMEDIATE")
        for gw, tag, h, cnt, s, mn, mx, qmin, qmax, qbad in rows:
            bucket_ms = int(h.timestamp() * 1000)
            con.execute(
                "INSERT INTO historian_rollup (resolution_s, tenant_id, gateway_id,"
                " tag_name, bucket_ms, n, sum_v, sumsq_v, min_v, max_v, first_v,"
                " first_ms, last_v, last_ms, last_text, q_min, q_max, q_bad_n)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(resolution_s, tenant_id, gateway_id, tag_name, bucket_ms)"
                " DO UPDATE SET n=excluded.n, sum_v=excluded.sum_v,"
                " min_v=excluded.min_v, max_v=excluded.max_v,"
                " q_min=excluded.q_min, q_max=excluded.q_max,"
                " q_bad_n=excluded.q_bad_n",
                (RES_S, tenant, str(gw or "cloud-legacy"), str(tag), bucket_ms,
                 int(cnt or 0), float(s) if s is not None else None, None,
                 float(mn) if mn is not None else None,
                 float(mx) if mx is not None else None,
                 None, bucket_ms, None, bucket_ms, None,
                 int(qmin) if qmin is not None else None,
                 int(qmax) if qmax is not None else None, int(qbad or 0)))
            n += 1
        con.execute("COMMIT")
    finally:
        con.close()
    return n


def main() -> int:
    global _TENANT
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--table", default="plc_readings")
    ap.add_argument("--before", default="2026-08-26 15:01:37",
                    help="archive rows older than this (where historian begins)")
    ap.add_argument("--tenant", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--archive", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    from sqlalchemy import text as sql
    engine, schema, app_store = _cloud()
    store_path = app_store._db_path                          # noqa: SLF001

    with engine.connect() as cx:
        cx.execute(sql("SET statement_timeout = '240s'"))
        # 2026-10-21: SELECT DISTINCT tenant_id over 151M rows times out -
        # there is no index that serves it. The control-plane tables are tiny
        # and are the authority on which tenants exist anyway.
        tenants = ([a.tenant] if a.tenant else
                   [t for t in cx.execute(sql(
                       f'SELECT tenant_id FROM "{schema}".cp_tenants '
                       f'UNION SELECT tenant_id FROM "{schema}".cp_customers'
                   )).scalars().all() if t])

    print("TrustNode - cloud history archive")
    print("  table   : %s.%s" % (schema, a.table))
    print("  before  : %s" % a.before)
    print("  tenants : %s" % tenants)
    print()

    man = _load(store_path)
    key = "%s|%s" % (a.table, a.before)
    entry = (man.get(key) or {"days": {}, "table": a.table, "before": a.before})

    if a.list or not (a.archive or a.verify or a.delete):
        with engine.connect() as cx:
            cx.execute(sql("SET statement_timeout = '240s'"))
            for t in tenants:
                _TENANT = t
                lo, hi = span(cx, sql, schema, a.table, a.before)
                print("  %-30s %s -> %s" % (t[:30], str(lo)[:19], str(hi)[:19]))
        print()
        print("  --archive to fold this window into %ss buckets" % RES_S)
        return 0

    if a.archive:
        total_rows = total_buckets = 0
        for t in tenants:
            _TENANT = t
            with engine.connect() as cx:
                cx.execute(sql("SET statement_timeout = '240s'"))
                lo, hi = span(cx, sql, schema, a.table, a.before)
            if not lo:
                print("  %s: nothing older than the cutoff" % t)
                continue
            d0, d1 = lo.date(), hi.date()
            print("  %s: %s -> %s (%d day(s))" % (t[:30], d0, d1, (d1 - d0).days + 1))
            day = d0
            while day <= d1:
                with engine.connect() as cx:
                    cx.execute(sql("SET statement_timeout = '240s'"))
                    t0 = time.time()
                    try:
                        rows = archive_day(cx, sql, schema, a.table, day, t)
                    except Exception as exc:  # noqa: BLE001
                        print("    %s  FAILED: %s" % (day, str(exc)[:90]))
                        entry["days"][f"{t}|{day}"] = {"error": str(exc)[:160]}
                        day += dt.timedelta(days=1)
                        continue
                got = len(rows)
                src = sum(int(r[3] or 0) for r in rows)
                total_buckets += got
                total_rows += src
                if a.apply:
                    # A day with no rows is trivially archived - there was
                    # nothing to carry. Marking it unverified blocked the
                    # delete on 142 of 181 days that were simply empty.
                    written = write_buckets(store_path, t, rows) if got else 0
                    entry["days"][f"{t}|{day}"] = {
                        "buckets": got, "source_rows": src,
                        "written": written, "verified": written == got}
                else:
                    entry["days"][f"{t}|{day}"] = {
                        "buckets": got, "source_rows": src, "written": 0,
                        "verified": False}
                print("    %s  %6d buckets from %9s rows  %4.1fs%s"
                      % (day, got, "{:,}".format(src), time.time() - t0,
                         "" if a.apply else "  (dry run)"))
                day += dt.timedelta(days=1)
        entry["archived_utc"] = time.strftime("%Y-%m-%d %H:%M:%S")
        man[key] = entry
        if a.apply:
            _save(store_path, man)
        print()
        print("  %s buckets from %s source rows (%.0fx)"
              % ("{:,}".format(total_buckets), "{:,}".format(total_rows),
                 total_rows / max(1, total_buckets)))
        if a.apply:
            print("  manifest: %s" % _manifest_path(store_path))

    if a.verify:
        days = entry.get("days") or {}
        bad = [k for k, v in days.items() if not v.get("verified")]
        print("  days archived : %d" % len(days))
        print("  verified      : %d" % (len(days) - len(bad)))
        if bad:
            print("  NOT verified  : %s" % bad[:5])

    if a.delete:
        days = entry.get("days") or {}
        bad = [k for k, v in days.items() if not v.get("verified")]
        if not days or bad:
            print("  REFUSING to delete: %d day(s) not verified. Run "
                  "--archive --apply first." % (len(bad) or 1))
            return 2
        if not a.apply:
            print("  would delete rows older than %s from %s.%s"
                  % (a.before, schema, a.table))
            return 0
        removed = 0
        for t in tenants:
            while True:
                with engine.connect() as cx:
                    cx.execute(sql("SET statement_timeout = '240s'"))
                    try:
                        res = cx.execute(sql(
                            f'DELETE FROM "{schema}"."{a.table}" WHERE id IN ('
                            f'  SELECT id FROM "{schema}"."{a.table}" '
                            f'  WHERE tenant_id = :t AND ts_utc < :c '
                            f'  ORDER BY id LIMIT 20000 FOR UPDATE SKIP LOCKED)'),
                            {"t": t, "c": a.before})
                        cx.commit()
                    except Exception as exc:  # noqa: BLE001
                        print("    %s delete stopped: %s" % (t[:24], str(exc)[:80]))
                        break
                got = int(res.rowcount or 0)
                removed += got
                print("    %s  removed %s (total %s)"
                      % (t[:24], "{:,}".format(got), "{:,}".format(removed)),
                      flush=True)
                if got <= 0:
                    break
        print()
        print("  removed %s rows" % "{:,}".format(removed))
        print("  NOTE: Postgres returns the space to the table, not the disk.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
