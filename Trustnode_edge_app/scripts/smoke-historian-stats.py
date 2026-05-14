import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

import requests


def env(name: str, default: str) -> str:
    if name in os.environ:
        return str(os.environ.get(name, "")).strip()
    return str(default).strip()


def main() -> int:
    api_base = env("TRUSTNODE_API_BASE", "http://127.0.0.1:8000")
    username = env("TRUSTNODE_SMOKE_USER", "admin")
    password = env("TRUSTNODE_SMOKE_PASS", "admin")
    gateway = env("TRUSTNODE_SMOKE_GATEWAY", "PLC")
    tag = env("TRUSTNODE_SMOKE_TAG", "SimREAL[2]")
    db_path = Path(env("TRUSTNODE_APPSTORE_DB", str(Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db")))

    s = requests.Session()
    login = s.post(
        f"{api_base}/api/auth/login",
        json={"username": username, "password": password},
        timeout=20,
    )
    login.raise_for_status()
    token = login.json().get("token")
    if not token:
        raise RuntimeError("Login succeeded but no token returned")
    s.headers.update({"Authorization": f"Bearer {token}"})

    params = {"gateway": gateway, "tag": tag}
    stats_url = f"{api_base}/api/app-store/historian/stats?{urlencode(params)}"
    stats_res = s.get(stats_url, timeout=30)
    stats_res.raise_for_status()
    api_rows = list((stats_res.json() or {}).get("rows") or [])

    if not db_path.exists():
        raise FileNotFoundError(f"App-store db not found: {db_path}")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        db_rows = conn.execute(
            """
            SELECT
              COALESCE(tag_name,'') AS tag_name,
              COUNT(*) AS row_count,
              SUM(COALESCE(value, 0)) AS sum_value,
              AVG(value) AS avg_value,
              MIN(value) AS min_value,
              MAX(value) AS max_value
            FROM historian_readings
            WHERE (COALESCE(gateway_name,'') = ? OR COALESCE(gateway_id,'') = ?)
              AND LOWER(COALESCE(tag_name,'')) LIKE LOWER(?)
            GROUP BY tag_name
            ORDER BY tag_name ASC
            """,
            (gateway, gateway, f"%{tag}%"),
        ).fetchall()

    out = {
        "api_base": api_base,
        "gateway": gateway,
        "tag_filter": tag,
        "api_rows": api_rows,
        "sqlite_rows": [
            {
                "tag": str(r["tag_name"] or ""),
                "count": int(r["row_count"] or 0),
                "sum": float(r["sum_value"] or 0.0),
                "avg": float(r["avg_value"] or 0.0) if r["avg_value"] is not None else None,
                "min": float(r["min_value"] or 0.0) if r["min_value"] is not None else None,
                "max": float(r["max_value"] or 0.0) if r["max_value"] is not None else None,
            }
            for r in db_rows
        ],
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
