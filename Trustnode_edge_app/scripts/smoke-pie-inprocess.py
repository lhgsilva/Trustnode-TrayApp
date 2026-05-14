"""In-process smoke test that imports the actual app_store service module
and calls get_historian_stats / get_historian_rule_stats directly against
the production SQLite db, then compares results against an independent
SQL implementation.

This bypasses the network and the running backend so we can verify the
new SQL changes without restarting the live process.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

# Ensure tenant context is set before importing the service.
import os
os.environ.setdefault("TRUSTNODE_TENANT_ID", "default")
os.environ.setdefault("TRUSTNODE_DATA_DIR", str(Path.home() / ".trustnode_edge" / "data"))

from app.tenant import set_current_tenant  # noqa: E402
from app.services.app_store import AppStore  # noqa: E402


def main() -> int:
    set_current_tenant("default")
    store = AppStore()

    db_path = Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db"
    if not db_path.exists():
        print(f"DB missing: {db_path}")
        return 1

    issues: list[str] = []

    # 1. historian_stats now returns latest per tag.
    api_rows = store.get_historian_stats(prefer_cloud_reads=False)
    api_by_tag = {str(r.get("tag") or ""): r for r in api_rows}

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        for tag, row in api_by_tag.items():
            ref = conn.execute(
                """
                SELECT COUNT(*) AS c, SUM(COALESCE(value,0)) AS s,
                       AVG(value) AS a, MIN(value) AS mn, MAX(value) AS mx
                FROM historian_readings
                WHERE tenant_id='default' AND COALESCE(tag_name,'')=:tag
                """,
                {"tag": tag},
            ).fetchone()
            if int(ref["c"] or 0) != int(row.get("count") or 0):
                issues.append(f"{tag}: count api={row.get('count')} db={ref['c']}")
            if abs(float(ref["s"] or 0.0) - float(row.get("sum") or 0.0)) > 1e-6:
                issues.append(f"{tag}: sum api={row.get('sum')} db={ref['s']}")

            # Verify latest
            latest_ref = conn.execute(
                """
                SELECT value FROM historian_readings
                WHERE tenant_id='default' AND COALESCE(tag_name,'')=:tag
                ORDER BY ts_utc DESC, id DESC LIMIT 1
                """,
                {"tag": tag},
            ).fetchone()
            if latest_ref is not None:
                expected = float(latest_ref["value"]) if latest_ref["value"] is not None else None
                got = row.get("latest")
                if expected is not None and got is None:
                    issues.append(f"{tag}: latest missing (expected {expected})")
                elif expected is not None and abs(float(got) - expected) > 1e-9:
                    issues.append(f"{tag}: latest api={got} db={expected}")

    # 2. rule-stats: pick three tags and check count for op=any
    sample_tags = list(api_by_tag.keys())[:3]
    rules = [
        {"id": f"r{i}", "label": t, "tag_name": t, "operator": "any", "aggregation": "count", "color": "#14a89a"}
        for i, t in enumerate(sample_tags)
    ]
    rule_rows = store.get_historian_rule_stats(rules=rules, prefer_cloud_reads=False)
    rule_by_tag = {str(r.get("tag_name") or ""): r for r in rule_rows}
    for tag in sample_tags:
        api_cnt = int(rule_by_tag.get(tag, {}).get("count") or 0)
        ref_cnt = int(api_by_tag[tag].get("count") or 0)
        if api_cnt != ref_cnt:
            issues.append(f"rule count any tag={tag}: rule={api_cnt} stats={ref_cnt}")

    # 3. rule-stats with operator filter
    if sample_tags:
        first_tag = sample_tags[0]
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM historian_readings
                WHERE tenant_id='default' AND COALESCE(tag_name,'')=:t AND value >= 0
                """,
                {"t": first_tag},
            ).fetchone()
            expected = int(row["c"] or 0)
        gte_rules = [{"id": "rgte", "label": "gte0", "tag_name": first_tag, "operator": "gte",
                      "value1": 0, "aggregation": "count", "color": "#14a89a"}]
        gte_rows = store.get_historian_rule_stats(rules=gte_rules, prefer_cloud_reads=False)
        got = int((gte_rows[0] if gte_rows else {}).get("count") or 0)
        if got != expected:
            issues.append(f"rule gte0 tag={first_tag}: got={got} expected={expected}")

    summary = {
        "tags_seen": len(api_by_tag),
        "sample_rule_tags": sample_tags,
        "issues": issues,
        "all_ok": len(issues) == 0,
        "sample_stats_row": api_rows[:1] if api_rows else None,
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0 if not issues else 2


if __name__ == "__main__":
    raise SystemExit(main())
