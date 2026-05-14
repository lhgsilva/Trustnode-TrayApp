#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def resolve_db_path() -> Path:
    env_path = os.environ.get("TRUSTNODE_APP_STORE_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()
    return Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ts_utc_now_str() -> str:
    return utc_now().strftime("%Y-%m-%d %H:%M:%S")


def to_db_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class CheckResult:
    name: str
    ok: bool
    details: dict[str, Any]


def query_scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def fetch_rows(
    conn: sqlite3.Connection,
    gateway_id: str,
    tag_name: str,
    from_ts: str | None = None,
    to_ts: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    where = ["gateway_id = ?", "tag_name = ?"]
    params: list[Any] = [gateway_id, tag_name]
    if from_ts:
        where.append("ts_utc >= ?")
        params.append(from_ts)
    if to_ts:
        where.append("ts_utc <= ?")
        params.append(to_ts)
    sql = f"""
        SELECT id, gateway_id, tag_name, value, ts_utc
        FROM historian_readings
        WHERE {" AND ".join(where)}
        ORDER BY ts_utc ASC, id ASC
    """
    if limit and limit > 0:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql, tuple(params)).fetchall())


def pick_seed(conn: sqlite3.Connection) -> sqlite3.Row | None:
    now = utc_now()
    recent_windows = [timedelta(hours=1), timedelta(hours=6), timedelta(hours=24)]

    for win in recent_windows:
        from_ts = to_db_ts(now - win)
        row = conn.execute(
            """
            SELECT gateway_id, tag_name, COUNT(*) AS c,
                   MAX(ts_utc) AS max_ts,
                   MIN(ts_utc) AS min_ts
            FROM historian_readings
            WHERE ts_utc >= ?
            GROUP BY gateway_id, tag_name
            ORDER BY c DESC
            LIMIT 1
            """,
            (from_ts,),
        ).fetchone()
        if row and int(row["c"] or 0) > 0:
            return row

    return conn.execute(
        """
        SELECT gateway_id, tag_name, COUNT(*) AS c,
               MAX(ts_utc) AS max_ts,
               MIN(ts_utc) AS min_ts
        FROM historian_readings
        GROUP BY gateway_id, tag_name
        ORDER BY c DESC
        LIMIT 1
        """
    ).fetchone()


def sql_count_range(conn: sqlite3.Connection, gateway_id: str, tag_name: str, from_ts: str, to_ts: str) -> int:
    return int(
        query_scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM historian_readings
            WHERE gateway_id = ?
              AND tag_name = ?
              AND ts_utc BETWEEN ? AND ?
            """,
            (gateway_id, tag_name, from_ts, to_ts),
        )
        or 0
    )


def test_time_filters(conn: sqlite3.Connection, gateway_id: str, tag_name: str) -> CheckResult:
    now = utc_now()
    now_ts = to_db_ts(now)
    windows = {
        "5m": timedelta(minutes=5),
        "15m": timedelta(minutes=15),
        "1h": timedelta(hours=1),
        "6h": timedelta(hours=6),
        "24h": timedelta(hours=24),
    }

    counts: dict[str, int] = {}
    for key, delta in windows.items():
        from_ts = to_db_ts(now - delta)
        counts[key] = sql_count_range(conn, gateway_id, tag_name, from_ts, now_ts)

    monotonic = counts["5m"] <= counts["15m"] <= counts["1h"] <= counts["6h"] <= counts["24h"]
    has_data_last_24h = counts["24h"] > 0
    has_data_last_1h = counts["1h"] > 0

    return CheckResult(
        name="time_filters",
        ok=bool(monotonic and has_data_last_24h),
        details={
            "counts": counts,
            "monotonic_non_decreasing": monotonic,
            "has_data_last_1h": has_data_last_1h,
            "has_data_last_24h": has_data_last_24h,
        },
    )


def test_grouping(conn: sqlite3.Connection, gateway_id: str, tag_name: str) -> CheckResult:
    from_ts = to_db_ts(utc_now() - timedelta(hours=1))
    raw_count = int(
        query_scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM historian_readings
            WHERE gateway_id = ? AND tag_name = ? AND ts_utc >= ?
            """,
            (gateway_id, tag_name, from_ts),
        )
        or 0
    )

    group_1s = int(
        query_scalar(
            conn,
            """
            SELECT COUNT(*) FROM (
              SELECT strftime('%Y-%m-%d %H:%M:%S', ts_utc) AS bucket
              FROM historian_readings
              WHERE gateway_id = ? AND tag_name = ? AND ts_utc >= ?
              GROUP BY bucket
            )
            """,
            (gateway_id, tag_name, from_ts),
        )
        or 0
    )

    group_5s = int(
        query_scalar(
            conn,
            """
            SELECT COUNT(*) FROM (
              SELECT strftime('%Y-%m-%d %H:%M:', ts_utc) ||
                     printf('%02d', (CAST(strftime('%S', ts_utc) AS INTEGER) / 5) * 5) AS bucket
              FROM historian_readings
              WHERE gateway_id = ? AND tag_name = ? AND ts_utc >= ?
              GROUP BY bucket
            )
            """,
            (gateway_id, tag_name, from_ts),
        )
        or 0
    )

    group_1m = int(
        query_scalar(
            conn,
            """
            SELECT COUNT(*) FROM (
              SELECT strftime('%Y-%m-%d %H:%M:00', ts_utc) AS bucket
              FROM historian_readings
              WHERE gateway_id = ? AND tag_name = ? AND ts_utc >= ?
              GROUP BY bucket
            )
            """,
            (gateway_id, tag_name, from_ts),
        )
        or 0
    )

    ok = raw_count >= group_1s >= group_5s >= group_1m >= 0
    return CheckResult(
        name="grouping",
        ok=ok,
        details={
            "raw_last_1h": raw_count,
            "group_1s_last_1h": group_1s,
            "group_5s_last_1h": group_5s,
            "group_1m_last_1h": group_1m,
        },
    )


def test_row_selection(conn: sqlite3.Connection, gateway_id: str, tag_name: str) -> CheckResult:
    all_rows = fetch_rows(conn, gateway_id, tag_name, from_ts=to_db_ts(utc_now() - timedelta(hours=1)))

    last_50_sql = conn.execute(
        """
        SELECT id
        FROM historian_readings
        WHERE gateway_id = ? AND tag_name = ? AND ts_utc >= ?
        ORDER BY ts_utc DESC, id DESC
        LIMIT 50
        """,
        (gateway_id, tag_name, to_db_ts(utc_now() - timedelta(hours=1))),
    ).fetchall()

    last_50_sql_ids_desc = [int(r["id"]) for r in last_50_sql]
    last_50_sql_ids_asc = sorted(last_50_sql_ids_desc)

    all_ids = [int(r["id"]) for r in all_rows]
    tail_ids = all_ids[-50:] if all_ids else []

    ok = len(last_50_sql_ids_asc) <= 50 and tail_ids == last_50_sql_ids_asc
    return CheckResult(
        name="row_selection",
        ok=ok,
        details={
            "all_rows_last_1h": len(all_rows),
            "last_50_sql": len(last_50_sql_ids_asc),
            "tail_match": tail_ids == last_50_sql_ids_asc,
            "last_row_id_all": all_ids[-1] if all_ids else None,
            "last_row_id_last_50": last_50_sql_ids_asc[-1] if last_50_sql_ids_asc else None,
        },
    )


def test_conditions(conn: sqlite3.Connection, gateway_id: str, tag_name: str) -> CheckResult:
    rows = fetch_rows(conn, gateway_id, tag_name, from_ts=to_db_ts(utc_now() - timedelta(hours=24)))
    values = [float(r["value"]) for r in rows if r["value"] is not None]
    if len(values) < 5:
        return CheckResult(
            name="conditions",
            ok=False,
            details={"reason": "not_enough_rows_for_condition_testing", "rows": len(values)},
        )

    mn = min(values)
    mx = max(values)
    med = statistics.median(values)
    low = mn + (mx - mn) * 0.33
    high = mn + (mx - mn) * 0.66

    from_ts = to_db_ts(utc_now() - timedelta(hours=24))
    gt = int(query_scalar(conn, "SELECT COUNT(*) FROM historian_readings WHERE gateway_id=? AND tag_name=? AND ts_utc>=? AND CAST(value AS REAL) > ?", (gateway_id, tag_name, from_ts, med)) or 0)
    gte = int(query_scalar(conn, "SELECT COUNT(*) FROM historian_readings WHERE gateway_id=? AND tag_name=? AND ts_utc>=? AND CAST(value AS REAL) >= ?", (gateway_id, tag_name, from_ts, med)) or 0)
    lt = int(query_scalar(conn, "SELECT COUNT(*) FROM historian_readings WHERE gateway_id=? AND tag_name=? AND ts_utc>=? AND CAST(value AS REAL) < ?", (gateway_id, tag_name, from_ts, med)) or 0)
    lte = int(query_scalar(conn, "SELECT COUNT(*) FROM historian_readings WHERE gateway_id=? AND tag_name=? AND ts_utc>=? AND CAST(value AS REAL) <= ?", (gateway_id, tag_name, from_ts, med)) or 0)
    between = int(query_scalar(conn, "SELECT COUNT(*) FROM historian_readings WHERE gateway_id=? AND tag_name=? AND ts_utc>=? AND CAST(value AS REAL) BETWEEN ? AND ?", (gateway_id, tag_name, from_ts, low, high)) or 0)
    eq = int(query_scalar(conn, "SELECT COUNT(*) FROM historian_readings WHERE gateway_id=? AND tag_name=? AND ts_utc>=? AND CAST(value AS REAL) = ?", (gateway_id, tag_name, from_ts, med)) or 0)
    ne = int(query_scalar(conn, "SELECT COUNT(*) FROM historian_readings WHERE gateway_id=? AND tag_name=? AND ts_utc>=? AND CAST(value AS REAL) != ?", (gateway_id, tag_name, from_ts, med)) or 0)
    total = int(query_scalar(conn, "SELECT COUNT(*) FROM historian_readings WHERE gateway_id=? AND tag_name=? AND ts_utc>=?", (gateway_id, tag_name, from_ts)) or 0)

    logic_ok = gte >= gt and lte >= lt and eq + ne == total and between <= total and total > 0
    return CheckResult(
        name="conditions",
        ok=logic_ok,
        details={
            "rows": total,
            "min": mn,
            "max": mx,
            "median": med,
            "low_threshold": low,
            "high_threshold": high,
            "counts": {
                "gt": gt,
                "gte": gte,
                "lt": lt,
                "lte": lte,
                "between": between,
                "eq": eq,
                "ne": ne,
            },
        },
    )


def run() -> int:
    db_path = resolve_db_path()
    reports_dir = Path("tests") / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "started_utc": utc_now().isoformat(),
        "db_path": str(db_path),
        "checks": [],
        "ok": False,
    }

    stamp = utc_now().strftime("%Y%m%d_%H%M%S")
    out = reports_dir / f"smoke_historian_query_builder_{stamp}.json"

    if not db_path.exists():
        report["error"] = "db_not_found"
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 2

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        seed = pick_seed(conn)
        if not seed:
            report["error"] = "no_historian_rows"
            out.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report, indent=2))
            return 3

        gateway_id = str(seed["gateway_id"])
        tag_name = str(seed["tag_name"])
        report["seed"] = {
            "gateway_id": gateway_id,
            "tag_name": tag_name,
            "rows": int(seed["c"]),
            "min_ts": seed["min_ts"],
            "max_ts": seed["max_ts"],
            "generated_utc": ts_utc_now_str(),
        }

        checks = [
            test_time_filters(conn, gateway_id, tag_name),
            test_grouping(conn, gateway_id, tag_name),
            test_row_selection(conn, gateway_id, tag_name),
            test_conditions(conn, gateway_id, tag_name),
        ]
        report["checks"] = [{"name": c.name, "ok": c.ok, "details": c.details} for c in checks]
        report["ok"] = all(c.ok for c in checks)
        report["finished_utc"] = utc_now().isoformat()

        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        print(f"\nReport saved: {out}")
        return 0 if report["ok"] else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(run())
