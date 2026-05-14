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


def resolve_db_path() -> Path:
    env_path = os.environ.get("TRUSTNODE_APP_STORE_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()
    return Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db"


def resolve_api_base() -> str:
    return os.environ.get("TRUSTNODE_API_BASE", "http://127.0.0.1:8000").rstrip("/")


def resolve_scope_key(conn: sqlite3.Connection) -> str:
    env_scope = os.environ.get("TRUSTNODE_SCOPE_KEY", "").strip()
    if env_scope:
        return env_scope
    row = conn.execute(
        """
        SELECT scope_key
        FROM config_documents_scoped
        WHERE domain = 'dashboard_configurations'
        ORDER BY updated_utc DESC
        LIMIT 1
        """
    ).fetchone()
    return str(row[0]) if row and row[0] else ""


def login_token(api_base: str) -> str:
    user = os.environ.get("TRUSTNODE_SMOKE_USER", "admin")
    pwd = os.environ.get("TRUSTNODE_SMOKE_PASS", "admin")
    res = requests.post(
        f"{api_base}/api/auth/login",
        json={"username": user, "password": pwd},
        timeout=20,
    )
    res.raise_for_status()
    payload = res.json() or {}
    tok = payload.get("token") or payload.get("access_token")
    if not tok:
        raise RuntimeError("login_ok_but_token_missing")
    return str(tok)


def normalize_iso_like(value: str) -> str:
    txt = str(value or "").strip()
    if not txt:
        return ""
    if "T" not in txt and " " in txt:
        txt = txt.replace(" ", "T", 1)
    txt = txt.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(txt).astimezone(timezone.utc).isoformat()
    except Exception:
        return ""


def resolve_preset_window_ms(preset: str) -> int:
    mapping = {
        "5m": 5 * 60 * 1000,
        "15m": 15 * 60 * 1000,
        "1h": 60 * 60 * 1000,
        "6h": 6 * 60 * 60 * 1000,
        "24h": 24 * 60 * 60 * 1000,
        "7d": 7 * 24 * 60 * 60 * 1000,
        "30d": 30 * 24 * 60 * 60 * 1000,
    }
    return int(mapping.get(str(preset or "none"), 0))


def resolve_range(cfg: dict[str, Any]) -> tuple[str, str]:
    preset = str(cfg.get("query_time_filter_preset") or "none")
    if preset == "none":
        return "", ""
    if preset == "custom":
        from_iso = normalize_iso_like(str(cfg.get("query_time_filter_from") or ""))
        to_iso = normalize_iso_like(str(cfg.get("query_time_filter_to") or ""))
        return from_iso, to_iso
    window_ms = resolve_preset_window_ms(preset)
    if window_ms <= 0:
        return "", ""
    now = utc_now()
    from_dt = now - timedelta(milliseconds=window_ms)
    return from_dt.isoformat(), now.isoformat()


def iso_to_sql_ts(iso_like: str) -> str:
    txt = normalize_iso_like(iso_like)
    if not txt:
        return ""
    dt = datetime.fromisoformat(txt)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def fetch_api_stats(
    api_base: str,
    token: str,
    gateway: str,
    tag: str,
    from_utc: str,
    to_utc: str,
) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"}
    params: dict[str, str] = {}
    if gateway:
        params["gateway"] = gateway
    if tag:
        params["tag"] = tag
    if from_utc:
        params["from_utc"] = from_utc
    if to_utc:
        params["to_utc"] = to_utc
    res = requests.get(f"{api_base}/api/app-store/historian/stats", headers=headers, params=params, timeout=30)
    res.raise_for_status()
    payload = res.json() or {}
    rows = payload.get("rows")
    return rows if isinstance(rows, list) else []


def fetch_api_rows(
    api_base: str,
    token: str,
    gateway: str,
    tag: str,
    from_utc: str,
    to_utc: str,
    max_rows: int = 1_000_000,
    page_limit: int = 5000,
) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"}
    out: list[dict[str, Any]] = []
    offset = 0
    while len(out) < max_rows:
        params: dict[str, str] = {
            "limit": str(page_limit),
            "offset": str(offset),
        }
        if gateway:
            params["gateway"] = gateway
        if tag:
            params["tag"] = tag
        if from_utc:
            params["from_utc"] = from_utc
        if to_utc:
            params["to_utc"] = to_utc
        res = requests.get(f"{api_base}/api/app-store/historian/range", headers=headers, params=params, timeout=30)
        res.raise_for_status()
        payload = res.json() or {}
        rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
        if not rows:
            break
        out.extend(rows)
        if len(rows) < page_limit:
            break
        offset += page_limit
    if len(out) > max_rows:
        out = out[:max_rows]
    return out


def sql_direct_count(
    conn: sqlite3.Connection,
    gateway: str,
    tag: str,
    from_sql: str,
    to_sql: str,
) -> int:
    where = [
        "(COALESCE(gateway_id,'') = ? OR COALESCE(gateway_name,'') = ?)",
        "LOWER(COALESCE(tag_name,'')) = LOWER(?)",
    ]
    params: list[Any] = [gateway, gateway, tag]
    if from_sql:
        where.append("ts_utc >= ?")
        params.append(from_sql)
    if to_sql:
        where.append("ts_utc <= ?")
        params.append(to_sql)
    row = conn.execute(
        f"SELECT COUNT(*) FROM historian_readings WHERE {' AND '.join(where)}",
        tuple(params),
    ).fetchone()
    return int(row[0] or 0) if row else 0


def rule_match(value: Any, operator: str, value1: Any, value2: Any) -> bool:
    try:
        raw = float(value)
    except Exception:
        return False
    op = str(operator or "any").lower()
    try:
        v1 = float(value1)
    except Exception:
        v1 = 0.0
    try:
        v2 = float(value2)
    except Exception:
        v2 = 0.0
    if op in {"any", "all"}:
        return True
    if op == "eq":
        return raw == v1
    if op in {"neq", "ne"}:
        return raw != v1
    if op == "lt":
        return raw < v1
    if op == "lte":
        return raw <= v1
    if op == "gt":
        return raw > v1
    if op == "gte":
        return raw >= v1
    if op == "between":
        lo = min(v1, v2)
        hi = max(v1, v2)
        return lo <= raw <= hi
    return False


def aggregate(values: list[float], mode: str) -> float:
    if not values:
        return 0.0
    kind = str(mode or "count").lower()
    if kind == "sum":
        return float(sum(values))
    if kind == "avg":
        return float(sum(values) / len(values))
    if kind == "min":
        return float(min(values))
    if kind == "max":
        return float(max(values))
    if kind == "latest":
        return float(values[-1])
    return float(len(values))


def evaluate_rules_from_rows(rows: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    rules = cfg.get("compute_rules")
    if not isinstance(rules, list):
        rules = []
    result_agg = str(cfg.get("query_result_aggregation") or "").strip().lower()
    out = []
    for idx, rule in enumerate(rules):
        gid = str((rule or {}).get("gateway_id") or "").strip()
        tag = str((rule or {}).get("tag_name") or "").strip()
        op = str((rule or {}).get("operator") or "any").strip().lower()
        v1 = (rule or {}).get("value1")
        v2 = (rule or {}).get("value2")
        agg = result_agg or str((rule or {}).get("aggregation") or "count").lower()
        subset = [
            r
            for r in rows
            if (not gid or str(r.get("gateway_id") or "") == gid)
            and (not tag or str(r.get("tag") or r.get("tag_name") or "") == tag)
            and rule_match(r.get("value"), op, v1, v2)
        ]
        vals: list[float] = []
        for r in subset:
            try:
                vals.append(float(r.get("value")))
            except Exception:
                pass
        out.append(
            {
                "label": str((rule or {}).get("label") or f"Item {idx + 1}"),
                "value": aggregate(vals, agg),
                "sample_count": len(subset),
            }
        )
    return out


@dataclass
class CheckResult:
    name: str
    ok: bool
    details: dict[str, Any]


def run() -> int:
    db_path = resolve_db_path()
    api_base = resolve_api_base()
    report_dir = Path("tests") / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%d_%H%M%S")
    report_path = report_dir / f"smoke_dashboard_widget_counts_{stamp}.json"

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
        scope_key = resolve_scope_key(conn)
        if not scope_key:
            report["error"] = "dashboard_scope_not_found"
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report, indent=2))
            return 3

        row = conn.execute(
            """
            SELECT payload_json
            FROM config_documents_scoped
            WHERE scope_key = ? AND domain = 'dashboard_configurations'
            LIMIT 1
            """,
            (scope_key,),
        ).fetchone()
        if not row:
            report["error"] = "dashboard_config_not_found"
            report["scope_key"] = scope_key
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report, indent=2))
            return 4

        payload = json.loads(str(row[0] or "{}"))
        widgets = payload.get("widgets") if isinstance(payload, dict) else []
        if not isinstance(widgets, list):
            widgets = []
        pie_widgets = [w for w in widgets if str((w or {}).get("type") or "") == "pie_chart"]
        report["scope_key"] = scope_key
        report["pie_widget_count"] = len(pie_widgets)

        token = login_token(api_base)
        checks: list[CheckResult] = []

        for w in pie_widgets:
            cfg = (w or {}).get("config") if isinstance((w or {}).get("config"), dict) else {}
            widget_id = str((w or {}).get("id") or "")
            ds = str(cfg.get("data_source_type") or "tag_direct")
            gateway = str(cfg.get("gateway_id") or "")
            tag = str(cfg.get("tag_name") or "")
            from_iso, to_iso = resolve_range(cfg)
            from_sql = iso_to_sql_ts(from_iso)
            to_sql = iso_to_sql_ts(to_iso)

            if ds == "tag_direct":
                agg = str(cfg.get("query_result_aggregation") or "count").lower()
                api_rows = fetch_api_stats(api_base, token, gateway, tag, from_iso, to_iso)
                api_row = next((r for r in api_rows if str(r.get("tag") or "").lower() == tag.lower()), None)
                api_value = 0.0
                if api_row:
                    if agg == "count":
                        api_value = float(api_row.get("count") or 0)
                    elif agg == "sum":
                        api_value = float(api_row.get("sum") or 0)
                    elif agg == "avg":
                        api_value = float(api_row.get("avg") or 0)
                    elif agg == "min":
                        api_value = float(api_row.get("min") or 0)
                    elif agg == "max":
                        api_value = float(api_row.get("max") or 0)
                sql_count = sql_direct_count(conn, gateway, tag, from_sql, to_sql)
                sql_value = float(sql_count) if agg == "count" else None
                ok = bool(api_row is not None and (agg != "count" or int(api_value) == int(sql_count)))
                checks.append(
                    CheckResult(
                        name=f"pie_direct:{widget_id}",
                        ok=ok,
                        details={
                            "widget_id": widget_id,
                            "tag": tag,
                            "gateway": gateway,
                            "aggregation": agg,
                            "from_iso": from_iso,
                            "to_iso": to_iso,
                            "api_row": api_row,
                            "api_value": api_value,
                            "sql_count": sql_count,
                            "sql_value": sql_value,
                        },
                    )
                )
            else:
                # Computed widgets: validate rule totals from API range rows vs direct SQL rule totals.
                # This mirrors the frontend rule evaluator path.
                rule_tags = sorted(
                    {
                        str((r or {}).get("tag_name") or "").strip()
                        for r in (cfg.get("compute_rules") or [])
                        if str((r or {}).get("tag_name") or "").strip()
                    }
                )
                api_rows_all: list[dict[str, Any]] = []
                for rt in rule_tags or [tag]:
                    api_rows_all.extend(fetch_api_rows(api_base, token, gateway, rt, from_iso, to_iso))
                api_eval = evaluate_rules_from_rows(api_rows_all, cfg)
                sql_eval = []
                for idx, rule in enumerate(cfg.get("compute_rules") or []):
                    gid = str((rule or {}).get("gateway_id") or gateway)
                    rtag = str((rule or {}).get("tag_name") or tag)
                    op = str((rule or {}).get("operator") or "any").lower()
                    v1 = (rule or {}).get("value1")
                    v2 = (rule or {}).get("value2")
                    where = [
                        "(COALESCE(gateway_id,'') = ? OR COALESCE(gateway_name,'') = ?)",
                        "LOWER(COALESCE(tag_name,'')) = LOWER(?)",
                    ]
                    params: list[Any] = [gid, gid, rtag]
                    if from_sql:
                        where.append("ts_utc >= ?")
                        params.append(from_sql)
                    if to_sql:
                        where.append("ts_utc <= ?")
                        params.append(to_sql)
                    if op == "gt":
                        where.append("CAST(value AS REAL) > ?")
                        params.append(float(v1 or 0))
                    elif op == "gte":
                        where.append("CAST(value AS REAL) >= ?")
                        params.append(float(v1 or 0))
                    elif op == "lt":
                        where.append("CAST(value AS REAL) < ?")
                        params.append(float(v1 or 0))
                    elif op == "lte":
                        where.append("CAST(value AS REAL) <= ?")
                        params.append(float(v1 or 0))
                    elif op == "between":
                        lo = min(float(v1 or 0), float(v2 or 0))
                        hi = max(float(v1 or 0), float(v2 or 0))
                        where.append("CAST(value AS REAL) BETWEEN ? AND ?")
                        params.extend([lo, hi])
                    elif op == "eq":
                        where.append("CAST(value AS REAL) = ?")
                        params.append(float(v1 or 0))
                    elif op in {"neq", "ne"}:
                        where.append("CAST(value AS REAL) != ?")
                        params.append(float(v1 or 0))
                    cnt = conn.execute(
                        f"SELECT COUNT(*) FROM historian_readings WHERE {' AND '.join(where)}",
                        tuple(params),
                    ).fetchone()[0]
                    sql_eval.append(
                        {
                            "label": str((rule or {}).get("label") or f"Item {idx + 1}"),
                            "value": float(cnt or 0),
                        }
                    )
                ok = True
                for a, b in zip(api_eval, sql_eval):
                    if int(round(float(a.get("value") or 0))) != int(round(float(b.get("value") or 0))):
                        ok = False
                        break
                checks.append(
                    CheckResult(
                        name=f"pie_computed:{widget_id}",
                        ok=ok,
                        details={
                            "widget_id": widget_id,
                            "gateway": gateway,
                            "tags_used": rule_tags,
                            "from_iso": from_iso,
                            "to_iso": to_iso,
                            "api_eval": api_eval,
                            "sql_eval": sql_eval,
                            "api_rows_total": len(api_rows_all),
                        },
                    )
                )

        report["checks"] = [{"name": c.name, "ok": c.ok, "details": c.details} for c in checks]
        report["ok"] = all(c.ok for c in checks) if checks else False
        report["finished_utc"] = utc_now().isoformat()
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        print(f"\nReport saved: {report_path}")
        return 0 if report["ok"] else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(run())

