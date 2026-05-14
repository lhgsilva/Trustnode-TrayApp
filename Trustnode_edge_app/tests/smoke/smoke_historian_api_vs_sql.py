#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_db_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def resolve_db_path() -> Path:
    env_path = os.environ.get("TRUSTNODE_APP_STORE_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()
    return Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db"


def resolve_api_base() -> str:
    return os.environ.get("TRUSTNODE_API_BASE", "http://127.0.0.1:8001").rstrip("/")


def resolve_auth_user() -> str:
    return os.environ.get("TRUSTNODE_SMOKE_USER", "admin")


def resolve_auth_pass() -> str:
    return os.environ.get("TRUSTNODE_SMOKE_PASS", "admin")


@dataclass
class CheckResult:
    name: str
    ok: bool
    details: dict[str, Any]


def parse_ts(ts: str) -> datetime:
    txt = str(ts or "").strip()
    if not txt:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if "T" not in txt and " " in txt:
        txt = txt.replace(" ", "T", 1)
    txt = txt.replace("Z", "+00:00")
    return datetime.fromisoformat(txt).astimezone(timezone.utc)


def bucket_key(ts: str, mode: str) -> str:
    dt = parse_ts(ts)
    if mode == "1s":
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    if mode == "5s":
        sec = (dt.second // 5) * 5
        return dt.strftime("%Y-%m-%d %H:%M:") + f"{sec:02d}"
    if mode == "1m":
        return dt.strftime("%Y-%m-%d %H:%M:00")
    raise ValueError(mode)


def pick_seed(conn: sqlite3.Connection) -> sqlite3.Row | None:
    now = utc_now()
    for delta in (timedelta(hours=1), timedelta(hours=6), timedelta(hours=24)):
        row = conn.execute(
            """
            SELECT gateway_id, tag_name, COUNT(*) AS c, MAX(ts_utc) AS max_ts, MIN(ts_utc) AS min_ts
            FROM historian_readings
            WHERE ts_utc >= ?
            GROUP BY gateway_id, tag_name
            ORDER BY c DESC
            LIMIT 1
            """,
            (to_db_ts(now - delta),),
        ).fetchone()
        if row and int(row["c"] or 0) > 0:
            return row
    return conn.execute(
        """
        SELECT gateway_id, tag_name, COUNT(*) AS c, MAX(ts_utc) AS max_ts, MIN(ts_utc) AS min_ts
        FROM historian_readings
        GROUP BY gateway_id, tag_name
        ORDER BY c DESC
        LIMIT 1
        """
    ).fetchone()


def login_token(api_base: str, username: str, password: str) -> str:
    resp = requests.post(
        f"{api_base}/api/auth/login",
        json={"username": username, "password": password},
        timeout=20,
    )
    resp.raise_for_status()
    token = (resp.json() or {}).get("token")
    if not token:
        raise RuntimeError("login succeeded but token missing")
    return str(token)


def fetch_api_rows(
    api_base: str,
    token: str,
    gateway: str,
    tag: str,
    from_utc: str,
    to_utc: str,
    page_limit: int = 2000,
) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"}
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = {
            "gateway": gateway,
            "tag": tag,
            "from_utc": from_utc,
            "to_utc": to_utc,
            "limit": str(page_limit),
            "offset": str(offset),
        }
        r = requests.get(f"{api_base}/api/app-store/historian/range", params=params, headers=headers, timeout=30)
        r.raise_for_status()
        payload = r.json() or {}
        rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
        out.extend(rows)
        if len(rows) < page_limit:
            break
        offset += page_limit
        if offset > 300000:
            break
    return out


def fetch_sql_rows(
    conn: sqlite3.Connection,
    gateway: str,
    tag: str,
    from_utc: str,
    to_utc: str,
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT ts_utc, gateway_id, tag_name, value
            FROM historian_readings
            WHERE gateway_id = ?
              AND tag_name = ?
              AND ts_utc BETWEEN ? AND ?
            ORDER BY ts_utc DESC, id DESC
            """,
            (gateway, tag, from_utc, to_utc),
        ).fetchall()
    )


def canonical_api(rows: list[dict[str, Any]]) -> list[tuple[str, str, str, float | None]]:
    out: list[tuple[str, str, str, float | None]] = []
    for r in rows:
        ts = str(r.get("ts") or "")
        gw = str(r.get("gateway_id") or "")
        tag = str(r.get("tag") or "")
        v = r.get("value")
        out.append((ts, gw, tag, float(v) if v is not None else None))
    return out


def canonical_sql(rows: list[sqlite3.Row]) -> list[tuple[str, str, str, float | None]]:
    out: list[tuple[str, str, str, float | None]] = []
    for r in rows:
        ts = str(r["ts_utc"] or "")
        gw = str(r["gateway_id"] or "")
        tag = str(r["tag_name"] or "")
        v = r["value"]
        out.append((ts, gw, tag, float(v) if v is not None else None))
    return out


def check_time_filters(api_base: str, token: str, conn: sqlite3.Connection, gateway: str, tag: str) -> CheckResult:
    now = utc_now()
    windows = {
        "5m": timedelta(minutes=5),
        "15m": timedelta(minutes=15),
        "1h": timedelta(hours=1),
        "6h": timedelta(hours=6),
    }
    details: dict[str, Any] = {}
    ok = True
    for k, d in windows.items():
        frm = to_db_ts(now - d)
        to = to_db_ts(now)
        api_rows = fetch_api_rows(api_base, token, gateway, tag, frm, to)
        sql_rows = fetch_sql_rows(conn, gateway, tag, frm, to)
        api_c = canonical_api(api_rows)
        sql_c = canonical_sql(sql_rows)
        same_count = len(api_c) == len(sql_c)
        same_first = (api_c[0] if api_c else None) == (sql_c[0] if sql_c else None)
        same_last = (api_c[-1] if api_c else None) == (sql_c[-1] if sql_c else None)
        details[k] = {
            "from_utc": frm,
            "to_utc": to,
            "api_count": len(api_c),
            "sql_count": len(sql_c),
            "same_count": same_count,
            "same_first_row": same_first,
            "same_last_row": same_last,
        }
        ok = ok and same_count and same_first and same_last
    return CheckResult("api_vs_sql_time_filters", ok, details)


def check_limit_offset(api_base: str, token: str, gateway: str, tag: str) -> CheckResult:
    now = utc_now()
    frm = to_db_ts(now - timedelta(hours=6))
    to = to_db_ts(now)
    base = fetch_api_rows(api_base, token, gateway, tag, frm, to, page_limit=500)
    headers = {"Authorization": f"Bearer {token}"}

    def one(limit: int, offset: int) -> list[dict[str, Any]]:
        r = requests.get(
            f"{api_base}/api/app-store/historian/range",
            params={
                "gateway": gateway,
                "tag": tag,
                "from_utc": frm,
                "to_utc": to,
                "limit": str(limit),
                "offset": str(offset),
            },
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()
        return (r.json() or {}).get("rows") or []

    p0 = one(50, 0)
    p1 = one(50, 50)
    base_c = canonical_api(base)
    p0_c = canonical_api(p0)
    p1_c = canonical_api(p1)

    ok = p0_c == base_c[:50] and p1_c == base_c[50:100]
    return CheckResult(
        "api_limit_offset",
        ok,
        {
            "full_count": len(base_c),
            "page0_count": len(p0_c),
            "page1_count": len(p1_c),
            "page0_matches_full_slice": p0_c == base_c[:50],
            "page1_matches_full_slice": p1_c == base_c[50:100],
        },
    )


def check_grouping_conditions_from_api_vs_sql(api_base: str, token: str, conn: sqlite3.Connection, gateway: str, tag: str) -> CheckResult:
    now = utc_now()
    frm = to_db_ts(now - timedelta(hours=1))
    to = to_db_ts(now)
    api_rows = canonical_api(fetch_api_rows(api_base, token, gateway, tag, frm, to))
    sql_rows = canonical_sql(fetch_sql_rows(conn, gateway, tag, frm, to))

    api_vals = [v for (_ts, _gw, _tag, v) in api_rows if v is not None]
    sql_vals = [v for (_ts, _gw, _tag, v) in sql_rows if v is not None]

    api_buckets = {
        "1s": len({bucket_key(ts, "1s") for (ts, _gw, _tag, _v) in api_rows}),
        "5s": len({bucket_key(ts, "5s") for (ts, _gw, _tag, _v) in api_rows}),
        "1m": len({bucket_key(ts, "1m") for (ts, _gw, _tag, _v) in api_rows}),
    }
    sql_buckets = {
        "1s": len({bucket_key(ts, "1s") for (ts, _gw, _tag, _v) in sql_rows}),
        "5s": len({bucket_key(ts, "5s") for (ts, _gw, _tag, _v) in sql_rows}),
        "1m": len({bucket_key(ts, "1m") for (ts, _gw, _tag, _v) in sql_rows}),
    }

    if not api_vals or not sql_vals:
        return CheckResult(
            "api_vs_sql_grouping_conditions",
            False,
            {
                "reason": "empty_values",
                "api_rows": len(api_rows),
                "sql_rows": len(sql_rows),
            },
        )

    pivot = sorted(sql_vals)[len(sql_vals) // 2]

    def cond(values: list[float]) -> dict[str, int]:
        return {
            "gt": sum(1 for v in values if v > pivot),
            "gte": sum(1 for v in values if v >= pivot),
            "lt": sum(1 for v in values if v < pivot),
            "lte": sum(1 for v in values if v <= pivot),
            "eq": sum(1 for v in values if v == pivot),
            "ne": sum(1 for v in values if v != pivot),
        }

    api_cond = cond(api_vals)
    sql_cond = cond(sql_vals)

    ok = (
        len(api_rows) == len(sql_rows)
        and api_buckets == sql_buckets
        and api_cond == sql_cond
    )

    return CheckResult(
        "api_vs_sql_grouping_conditions",
        ok,
        {
            "rows": {"api": len(api_rows), "sql": len(sql_rows)},
            "buckets": {"api": api_buckets, "sql": sql_buckets},
            "pivot": pivot,
            "conditions": {"api": api_cond, "sql": sql_cond},
        },
    )


def run() -> int:
    db_path = resolve_db_path()
    api_base = resolve_api_base()
    reports_dir = Path("tests") / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%d_%H%M%S")
    report_path = reports_dir / f"smoke_historian_api_vs_sql_{stamp}.json"

    report: dict[str, Any] = {
        "started_utc": utc_now().isoformat(),
        "db_path": str(db_path),
        "api_base": api_base,
        "checks": [],
        "ok": False,
    }

    if not db_path.exists():
        report["error"] = "db_not_found"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 2

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        seed = pick_seed(conn)
        if not seed:
            report["error"] = "no_historian_rows"
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report, indent=2))
            return 3

        gateway = str(seed["gateway_id"])
        tag = str(seed["tag_name"])
        report["seed"] = {
            "gateway_id": gateway,
            "tag_name": tag,
            "rows": int(seed["c"]),
            "min_ts": seed["min_ts"],
            "max_ts": seed["max_ts"],
        }

        token = login_token(api_base, resolve_auth_user(), resolve_auth_pass())
        report["auth"] = {"user": resolve_auth_user(), "token_len": len(token)}

        checks = [
            check_time_filters(api_base, token, conn, gateway, tag),
            check_limit_offset(api_base, token, gateway, tag),
            check_grouping_conditions_from_api_vs_sql(api_base, token, conn, gateway, tag),
        ]
        report["checks"] = [{"name": c.name, "ok": c.ok, "details": c.details} for c in checks]
        report["ok"] = all(c.ok for c in checks)
        report["finished_utc"] = utc_now().isoformat()

        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        print(f"\nReport saved: {report_path}")
        return 0 if report["ok"] else 1
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["finished_utc"] = utc_now().isoformat()
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        print(f"\nReport saved: {report_path}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(run())
