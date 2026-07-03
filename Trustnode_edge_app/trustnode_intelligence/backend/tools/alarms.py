"""Alarms / app_log tool. Reads from app_store's app_logs table."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from .tag_summary import _parse_time, _to_sqlite_text


def run_list_recent_alarms(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    limit = int(args.get("limit") or 50)
    since = _parse_time(args.get("since") or "-24h")
    from ._scope import all_db_paths
    seen = set()
    rows = []
    from .tag_summary import hist_connect
    for db_path in all_db_paths():
        try:
            con = hist_connect(db_path)
            try:
                cur = con.execute(
                    "SELECT ts_utc, level, category, gateway_id, gateway_name, message "
                    "FROM app_logs WHERE ts_utc >= ? "
                    "ORDER BY ts_utc DESC LIMIT ?",
                    (_to_sqlite_text(since), int(limit)),
                )
                for r in cur.fetchall():
                    key = (r[0], r[5])  # ts + message
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append({
                        "ts_utc": r[0], "level": r[1], "category": r[2],
                        "gateway_id": r[3], "gateway_name": r[4], "message": r[5],
                    })
            except sqlite3.OperationalError:
                pass
            finally:
                con.close()
        except Exception:
            continue
    rows.sort(key=lambda x: x["ts_utc"], reverse=True)
    rows = rows[:limit]
    return {"count": len(rows), "alarms": rows, "since": _to_sqlite_text(since) + "Z"}
