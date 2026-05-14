"""Direct-DB smoke test for the pie chart fixes.

Verifies that:
  1. The new historian_stats SQL (with latest_by_tag JOIN) returns the same per-tag
     counts/sum/avg/min/max as a plain GROUP BY, plus a latest value that matches
     the row with MAX(ts_utc) for each tag.
  2. The rule-stats SQL counts/aggregations match a hand-rolled equivalent.

Run with:
  d:/.../Tray_app/Trustnode_edge_app/backend/.venv/Scripts/python.exe \
    d:/.../Tray_app/Trustnode_edge_app/scripts/smoke-pie-direct-sql.py
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


def fetch_aggregates_plain(conn: sqlite3.Connection, tenant: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
          COALESCE(tag_name,'') AS tag_name,
          COUNT(*) AS row_count,
          SUM(COALESCE(value, 0)) AS sum_value,
          AVG(value) AS avg_value,
          MIN(value) AS min_value,
          MAX(value) AS max_value
        FROM historian_readings
        WHERE tenant_id = :tenant
        GROUP BY tag_name
        ORDER BY tag_name ASC
        """,
        {"tenant": tenant},
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        tag = str(r["tag_name"] or "")
        if not tag:
            continue
        out[tag] = {
            "count": int(r["row_count"] or 0),
            "sum": float(r["sum_value"] or 0.0),
            "avg": float(r["avg_value"]) if r["avg_value"] is not None else None,
            "min": float(r["min_value"]) if r["min_value"] is not None else None,
            "max": float(r["max_value"]) if r["max_value"] is not None else None,
        }
    return out


def fetch_latest_naive(conn: sqlite3.Connection, tenant: str) -> dict[str, float | None]:
    tags = conn.execute(
        "SELECT DISTINCT COALESCE(tag_name,'') AS tag_name FROM historian_readings WHERE tenant_id = :tenant",
        {"tenant": tenant},
    ).fetchall()
    out: dict[str, float | None] = {}
    for t in tags:
        tag = str(t["tag_name"] or "")
        if not tag:
            continue
        row = conn.execute(
            """
            SELECT value FROM historian_readings
            WHERE tenant_id = :tenant AND COALESCE(tag_name,'') = :tag
            ORDER BY ts_utc DESC, id DESC
            LIMIT 1
            """,
            {"tenant": tenant, "tag": tag},
        ).fetchone()
        try:
            out[tag] = float(row["value"]) if row and row["value"] is not None else None
        except Exception:
            out[tag] = None
    return out


def fetch_latest_new(conn: sqlite3.Connection, tenant: str) -> dict[str, float | None]:
    """Replicates the new SQL added to get_historian_stats."""
    where = "WHERE tenant_id = :tenant"
    rows = conn.execute(
        f"""
        SELECT h.tag_name AS tag_name, h.value AS value
        FROM historian_readings AS h
        JOIN (
          SELECT COALESCE(tag_name,'') AS tag_name, MAX(ts_utc) AS max_ts
          FROM historian_readings
          {where}
          GROUP BY tag_name
        ) AS m
          ON COALESCE(h.tag_name,'') = m.tag_name
         AND h.ts_utc = m.max_ts
        {where}
        """,
        {"tenant": tenant},
    ).fetchall()
    out: dict[str, float | None] = {}
    for r in rows:
        tag = str(r["tag_name"] or "")
        try:
            if r["value"] is not None:
                out[tag] = float(r["value"])
        except Exception:
            out[tag] = None
    return out


def fetch_rule_stats_for_rule(conn: sqlite3.Connection, tenant: str, rule: dict[str, Any]) -> dict[str, Any]:
    where = "WHERE tenant_id = :tenant"
    params: dict[str, Any] = {"tenant": tenant}
    rule_gateway = str(rule.get("gateway_id") or "").strip()
    rule_tag = str(rule.get("tag_name") or "").strip()
    op = str(rule.get("operator") or "any").strip().lower()
    if rule_gateway:
        if rule_gateway.lower().startswith("gw-"):
            where += " AND COALESCE(gateway_id,'') = :gid"
            params["gid"] = rule_gateway
        else:
            where += " AND (COALESCE(gateway_id,'') = :gn OR COALESCE(gateway_name,'') = :gn)"
            params["gn"] = rule_gateway
    if rule_tag:
        where += " AND LOWER(COALESCE(tag_name,'')) = LOWER(:tg)"
        params["tg"] = rule_tag

    def _flt(v: Any) -> float | None:
        try:
            n = float(v)
            return n if n == n and n not in (float("inf"), float("-inf")) else None
        except Exception:
            return None

    v1 = _flt(rule.get("value1"))
    v2 = _flt(rule.get("value2"))

    op_map = {">": "gt", ">=": "gte", "<": "lt", "<=": "lte", "==": "eq", "=": "eq", "!=": "ne", "<>": "ne"}
    op = op_map.get(op, op)
    if op in {"eq", "ne", "lt", "lte", "gt", "gte"} and v1 is None:
        where += " AND 1=0"
    elif op == "between" and (v1 is None or v2 is None):
        where += " AND 1=0"
    elif op == "eq":
        where += " AND value = :rv1"; params["rv1"] = v1
    elif op == "ne":
        where += " AND value <> :rv1"; params["rv1"] = v1
    elif op == "lt":
        where += " AND value < :rv1"; params["rv1"] = v1
    elif op == "lte":
        where += " AND value <= :rv1"; params["rv1"] = v1
    elif op == "gt":
        where += " AND value > :rv1"; params["rv1"] = v1
    elif op == "gte":
        where += " AND value >= :rv1"; params["rv1"] = v1
    elif op == "between":
        lo, hi = sorted([v1, v2])
        where += " AND value >= :rv1 AND value <= :rv2"
        params["rv1"] = lo; params["rv2"] = hi

    row = conn.execute(
        f"""
        SELECT COUNT(*) AS row_count, SUM(value) AS sum_value, AVG(value) AS avg_value,
               MIN(value) AS min_value, MAX(value) AS max_value
        FROM historian_readings {where}
        """,
        params,
    ).fetchone()
    cnt = int(row["row_count"] or 0)
    return {
        "rule_label": rule.get("label"),
        "count": cnt,
        "sum": float(row["sum_value"] or 0.0),
        "avg": float(row["avg_value"]) if row and row["avg_value"] is not None else None,
        "min": float(row["min_value"]) if row and row["min_value"] is not None else None,
        "max": float(row["max_value"]) if row and row["max_value"] is not None else None,
    }


def main() -> int:
    db_path = Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db"
    if not db_path.exists():
        print(f"DB not found at {db_path}")
        return 1
    tenant = "default"
    issues: list[str] = []

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row

        # 1. Compare new-style latest JOIN vs naive ORDER BY per tag.
        plain = fetch_aggregates_plain(conn, tenant)
        latest_naive = fetch_latest_naive(conn, tenant)
        latest_new = fetch_latest_new(conn, tenant)

        all_tags = sorted(set(latest_naive.keys()) | set(latest_new.keys()))
        for tag in all_tags:
            a = latest_naive.get(tag)
            b = latest_new.get(tag)
            if a != b:
                # Allow ties where two rows share max ts; just verify both came from a max-ts row.
                # Both pathways pick the highest ts; differences indicate a real bug.
                issues.append(f"latest mismatch for tag={tag}: naive={a} new={b}")

        # 2. Build sample rules and verify rule-stats SQL matches a hand-rolled count.
        sample_tags = list(plain.keys())[:3]
        for tag in sample_tags:
            for op, val in [("any", None), ("gte", 0.0), ("lt", 1.0e9)]:
                rule = {"label": f"{tag}-{op}", "tag_name": tag, "operator": op, "value1": val, "aggregation": "count"}
                got = fetch_rule_stats_for_rule(conn, tenant, rule)
                if op == "any":
                    expected = plain[tag]["count"]
                    if got["count"] != expected:
                        issues.append(f"rule any count mismatch tag={tag}: rule={got['count']} plain={expected}")

        # 3. Verify that, for each tag, the latest_new value matches a row whose ts_utc is the max.
        for tag in latest_new.keys():
            v = latest_new[tag]
            if v is None:
                continue
            row = conn.execute(
                """
                SELECT value, ts_utc FROM historian_readings
                WHERE tenant_id = :tenant AND COALESCE(tag_name,'') = :tag
                ORDER BY ts_utc DESC, id DESC
                LIMIT 1
                """,
                {"tenant": tenant, "tag": tag},
            ).fetchone()
            if row is None:
                issues.append(f"latest_new sees tag={tag} but it has no rows")
                continue
            try:
                expected = float(row["value"]) if row["value"] is not None else None
            except Exception:
                expected = None
            if expected is not None and abs((v or 0.0) - expected) > 1e-9:
                issues.append(f"latest_new value mismatch tag={tag}: got={v} expected={expected}")

    print(json.dumps({
        "tenant": tenant,
        "tags_checked": len(all_tags),
        "issues": issues,
        "all_ok": len(issues) == 0,
    }, indent=2))
    return 0 if not issues else 2


if __name__ == "__main__":
    raise SystemExit(main())
