import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests


def env(name: str, default: str) -> str:
    if name in os.environ:
        return str(os.environ.get(name, "")).strip()
    return str(default).strip()


def norm_utc_filter(value: Any) -> str:
    txt = str(value or "").strip()
    if not txt:
        return ""
    try:
        iso = txt.replace("Z", "+00:00")
        if " " in iso and "T" not in iso:
            iso = iso.replace(" ", "T")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def resolve_time_filter_range(cfg: dict[str, Any]) -> tuple[str, str]:
    preset = str(cfg.get("query_time_filter_preset") or "none").strip().lower()
    if preset == "none":
        return "", ""
    if preset == "custom":
        return norm_utc_filter(cfg.get("query_time_filter_from")), norm_utc_filter(cfg.get("query_time_filter_to"))

    windows = {
        "5m": 5 * 60,
        "15m": 15 * 60,
        "1h": 60 * 60,
        "6h": 6 * 60 * 60,
        "24h": 24 * 60 * 60,
        "7d": 7 * 24 * 60 * 60,
        "30d": 30 * 24 * 60 * 60,
    }
    sec = int(windows.get(preset, 0))
    if sec <= 0:
        return "", ""
    now = datetime.now(timezone.utc)
    start = now - timedelta(seconds=sec)
    return start.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")


def add_operator_sql(where_sql: str, params: dict[str, Any], op_raw: str, v1_raw: Any, v2_raw: Any) -> tuple[str, dict[str, Any]]:
    op = str(op_raw or "any").strip().lower()
    if op in {"", "any"}:
        return where_sql, params

    def to_float(v: Any) -> float | None:
        try:
            n = float(v)
            if n == float("inf") or n == float("-inf"):
                return None
            return n
        except Exception:
            return None

    v1 = to_float(v1_raw)
    v2 = to_float(v2_raw)
    if op in {"eq", "ne", "lt", "lte", "gt", "gte"} and v1 is None:
        return where_sql + " AND 1=0", params
    if op == "between" and (v1 is None or v2 is None):
        return where_sql + " AND 1=0", params

    if op == "eq":
        where_sql += " AND value = :rule_v1"
        params["rule_v1"] = v1
    elif op == "ne":
        where_sql += " AND value <> :rule_v1"
        params["rule_v1"] = v1
    elif op == "lt":
        where_sql += " AND value < :rule_v1"
        params["rule_v1"] = v1
    elif op == "lte":
        where_sql += " AND value <= :rule_v1"
        params["rule_v1"] = v1
    elif op == "gt":
        where_sql += " AND value > :rule_v1"
        params["rule_v1"] = v1
    elif op == "gte":
        where_sql += " AND value >= :rule_v1"
        params["rule_v1"] = v1
    elif op == "between":
        lo = min(v1, v2)
        hi = max(v1, v2)
        where_sql += " AND value >= :rule_v1 AND value <= :rule_v2"
        params["rule_v1"] = lo
        params["rule_v2"] = hi
    return where_sql, params


def row_to_stats(row: sqlite3.Row | None) -> dict[str, Any]:
    if not row:
        return {"count": 0, "sum": 0.0, "avg": None, "min": None, "max": None}
    return {
        "count": int((row["row_count"] or 0)),
        "sum": float((row["sum_value"] or 0.0)),
        "avg": float(row["avg_value"]) if row["avg_value"] is not None else None,
        "min": float(row["min_value"]) if row["min_value"] is not None else None,
        "max": float(row["max_value"]) if row["max_value"] is not None else None,
    }


@dataclass
class SmokeResult:
    widget_id: str
    widget_type: str
    source: str
    ok: bool
    details: dict[str, Any]


def main() -> int:
    api_base = env("TRUSTNODE_API_BASE", "http://127.0.0.1:8000")
    username = env("TRUSTNODE_SMOKE_USER", "admin")
    password = env("TRUSTNODE_SMOKE_PASS", "admin")
    scope_key = env("TRUSTNODE_DASHBOARD_SCOPE", "default|-|edge-1868e2b401|admin")
    tenant_id = env("TRUSTNODE_TENANT_ID", "default")
    db_path = Path(env("TRUSTNODE_APPSTORE_DB", str(Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db")))

    if not db_path.exists():
        raise FileNotFoundError(f"App-store db not found: {db_path}")

    session = requests.Session()
    login = session.post(
        f"{api_base}/api/auth/login",
        json={"username": username, "password": password},
        timeout=20,
    )
    login.raise_for_status()
    token = (login.json() or {}).get("token")
    if not token:
        raise RuntimeError("Login succeeded but token was not returned")
    session.headers.update({"Authorization": f"Bearer {token}"})

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT payload_json
            FROM config_documents_scoped
            WHERE scope_key = ? AND domain = 'dashboard_configurations'
            """,
            (scope_key,),
        ).fetchone()
        if not row:
            raise RuntimeError(f"No dashboard_configurations found for scope: {scope_key}")

        payload = json.loads(str(row["payload_json"] or "{}"))
        widgets = list((payload or {}).get("widgets") or [])
        pie_widgets = [w for w in widgets if str(w.get("type") or "") == "pie_chart"]

        results: list[SmokeResult] = []
        for widget in pie_widgets:
            widget_id = str(widget.get("id") or "")
            cfg = dict(widget.get("config") or {})
            source = str(cfg.get("data_source_type") or "tag_direct")
            gateway = str(cfg.get("gateway_id") or "").strip()
            tag = str(cfg.get("tag_name") or "").strip()
            from_utc, to_utc = resolve_time_filter_range(cfg)

            if source == "tag_direct":
                stats_params = {}
                if gateway:
                    stats_params["gateway"] = gateway
                if tag:
                    stats_params["tag"] = tag
                if from_utc:
                    stats_params["from_utc"] = from_utc
                if to_utc:
                    stats_params["to_utc"] = to_utc

                api_rows = (
                    session.get(
                        f"{api_base}/api/app-store/historian/stats?{urlencode(stats_params)}",
                        timeout=30,
                    ).json()
                    or {}
                ).get("rows") or []
                api_rows = list(api_rows)

                where = "WHERE tenant_id = :tenant"
                params: dict[str, Any] = {"tenant": tenant_id}
                if gateway:
                    where += " AND (COALESCE(gateway_name,'') = :gateway OR COALESCE(gateway_id,'') = :gateway)"
                    params["gateway"] = gateway
                if tag:
                    where += " AND LOWER(COALESCE(tag_name,'')) = LOWER(:tag_exact)"
                    params["tag_exact"] = tag
                if from_utc:
                    where += " AND ts_utc >= :from_utc"
                    params["from_utc"] = from_utc
                if to_utc:
                    where += " AND ts_utc <= :to_utc"
                    params["to_utc"] = to_utc

                db_rows = conn.execute(
                    f"""
                    SELECT
                      COALESCE(tag_name,'') AS tag_name,
                      COUNT(*) AS row_count,
                      SUM(COALESCE(value, 0)) AS sum_value,
                      AVG(value) AS avg_value,
                      MIN(value) AS min_value,
                      MAX(value) AS max_value
                    FROM historian_readings
                    {where}
                    GROUP BY tag_name
                    ORDER BY tag_name ASC
                    """,
                    params,
                ).fetchall()

                latest_map: dict[str, float | None] = {}
                for db_row in db_rows:
                    tag_name = str(db_row["tag_name"] or "")
                    if not tag_name:
                        continue
                    local_where = where + " AND COALESCE(tag_name,'') = :__latest_tag"
                    local_params = dict(params)
                    local_params["__latest_tag"] = tag_name
                    lr = conn.execute(
                        f"""
                        SELECT value FROM historian_readings
                        {local_where}
                        ORDER BY ts_utc DESC, id DESC
                        LIMIT 1
                        """,
                        local_params,
                    ).fetchone()
                    try:
                        latest_map[tag_name] = float(lr["value"]) if lr is not None and lr["value"] is not None else None
                    except Exception:
                        latest_map[tag_name] = None

                db_norm = [
                    {
                        "tag": str(r["tag_name"] or ""),
                        "count": int(r["row_count"] or 0),
                        "sum": float(r["sum_value"] or 0.0),
                        "avg": float(r["avg_value"]) if r["avg_value"] is not None else None,
                        "min": float(r["min_value"]) if r["min_value"] is not None else None,
                        "max": float(r["max_value"]) if r["max_value"] is not None else None,
                        "latest": latest_map.get(str(r["tag_name"] or "")),
                    }
                    for r in db_rows
                    if str(r["tag_name"] or "").strip()
                ]

                ok = json.dumps(api_rows, sort_keys=True) == json.dumps(db_norm, sort_keys=True)
                results.append(
                    SmokeResult(
                        widget_id=widget_id,
                        widget_type="pie_chart",
                        source=source,
                        ok=ok,
                        details={
                            "gateway": gateway,
                            "tag": tag,
                            "from_utc": from_utc,
                            "to_utc": to_utc,
                            "api_rows": api_rows,
                            "db_rows": db_norm,
                        },
                    )
                )
                continue

            # computed source
            rules = list(cfg.get("compute_rules") or [])
            api_payload = {
                "rules": rules,
                "from_utc": from_utc,
                "to_utc": to_utc,
                "gateway": gateway,
                "edge_id": "",
                "prefer_cloud": "",
            }
            api_rows = (
                session.post(
                    f"{api_base}/api/app-store/historian/rule-stats",
                    json=api_payload,
                    timeout=30,
                ).json()
                or {}
            ).get("rows") or []
            api_rows = list(api_rows)

            db_rules: list[dict[str, Any]] = []
            for idx, rule in enumerate(rules):
                rule_id = str(rule.get("id") or f"rule-{idx + 1}")
                rule_gateway = str(rule.get("gateway_id") or "").strip() or gateway
                rule_tag = str(rule.get("tag_name") or "").strip()
                op = str(rule.get("operator") or "any")
                agg = str(rule.get("aggregation") or "count").strip().lower()

                where = "WHERE tenant_id = :tenant"
                params = {"tenant": tenant_id}
                if rule_gateway:
                    if rule_gateway.lower().startswith("gw-"):
                        where += " AND COALESCE(gateway_id,'') = :gateway_id"
                        params["gateway_id"] = rule_gateway
                    else:
                        where += " AND (COALESCE(gateway_id,'') = :gateway_name OR COALESCE(gateway_name,'') = :gateway_name)"
                        params["gateway_name"] = rule_gateway
                if rule_tag:
                    where += " AND LOWER(COALESCE(tag_name,'')) = LOWER(:tag_exact)"
                    params["tag_exact"] = rule_tag
                if from_utc:
                    where += " AND ts_utc >= :from_utc"
                    params["from_utc"] = from_utc
                if to_utc:
                    where += " AND ts_utc <= :to_utc"
                    params["to_utc"] = to_utc
                where, params = add_operator_sql(where, params, op, rule.get("value1"), rule.get("value2"))

                agg_row = conn.execute(
                    f"""
                    SELECT
                      COUNT(*) AS row_count,
                      SUM(value) AS sum_value,
                      AVG(value) AS avg_value,
                      MIN(value) AS min_value,
                      MAX(value) AS max_value
                    FROM historian_readings
                    {where}
                    """,
                    params,
                ).fetchone()
                stats = row_to_stats(agg_row)
                latest = None
                if agg == "latest" and stats["count"] > 0:
                    latest_row = conn.execute(
                        f"""
                        SELECT value
                        FROM historian_readings
                        {where}
                        ORDER BY ts_utc DESC, id DESC
                        LIMIT 1
                        """,
                        params,
                    ).fetchone()
                    if latest_row is not None:
                        try:
                            latest = float(latest_row["value"])
                        except Exception:
                            latest = None

                if agg == "sum":
                    metric = float(stats["sum"])
                elif agg == "avg":
                    metric = float(stats["avg"] or 0.0)
                elif agg == "min":
                    metric = float(stats["min"] or 0.0)
                elif agg == "max":
                    metric = float(stats["max"] or 0.0)
                elif agg == "latest":
                    metric = float(latest or 0.0)
                else:
                    metric = float(stats["count"])

                db_rules.append(
                    {
                        "id": rule_id,
                        "value": metric,
                        "count": int(stats["count"]),
                        "sum": float(stats["sum"]),
                        "avg": float(stats["avg"]) if stats["avg"] is not None else None,
                        "min": float(stats["min"]) if stats["min"] is not None else None,
                        "max": float(stats["max"]) if stats["max"] is not None else None,
                        "latest": float(latest) if latest is not None else None,
                    }
                )

            api_by_id = {
                str(r.get("id") or ""): {
                    "value": float(r.get("value") or 0.0),
                    "count": int(r.get("count") or 0),
                    "sum": float(r.get("sum") or 0.0),
                    "avg": float(r["avg"]) if r.get("avg") is not None else None,
                    "min": float(r["min"]) if r.get("min") is not None else None,
                    "max": float(r["max"]) if r.get("max") is not None else None,
                    "latest": float(r["latest"]) if r.get("latest") is not None else None,
                }
                for r in api_rows
            }
            db_by_id = {r["id"]: {k: r[k] for k in ("value", "count", "sum", "avg", "min", "max", "latest")} for r in db_rules}
            ok = json.dumps(api_by_id, sort_keys=True) == json.dumps(db_by_id, sort_keys=True)

            results.append(
                SmokeResult(
                    widget_id=widget_id,
                    widget_type="pie_chart",
                    source=source,
                    ok=ok,
                    details={
                        "gateway": gateway,
                        "from_utc": from_utc,
                        "to_utc": to_utc,
                        "api_by_id": api_by_id,
                        "db_by_id": db_by_id,
                    },
                )
            )

    output = {
        "scope_key": scope_key,
        "tenant_id": tenant_id,
        "db_path": str(db_path),
        "results": [r.__dict__ for r in results],
        "all_ok": all(r.ok for r in results) if results else True,
    }
    print(json.dumps(output, indent=2))
    return 0 if output["all_ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
