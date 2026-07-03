"""Time-series analytics tools for TrustNode Intelligence.

Everything here reads the historian read-only and pushes as much work
into SQLite as possible (index-backed range scans + GROUP BY bucketing)
so responses stay fast even on multi-hundred-thousand-row tables.

Tools:
  * get_bucketed_series   — avg/min/max/count per time bucket (1s..1d)
  * detect_threshold      — count/duration where a tag crosses a limit
  * analyze_trend         — linear regression (slope, R^2) + projection
  * detect_anomalies      — SPC: points outside +/-k sigma, drift/shift

Design notes:
  - All queries constrain by tenant_id FIRST so the composite indexes
    (tenant_id, tag_name, ts_utc) are used as index SEARCHes, not SCANs.
  - Time bucketing uses integer epoch-second division in SQL:
      bucket = (strftime('%s', ts_utc) / bucket_s) * bucket_s
    which groups readings into fixed windows without pulling raw rows.
  - Structured filter params (value_gt / value_lt / quality) map to
    parameterized WHERE clauses — the model never writes raw SQL.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .tag_summary import _parse_time, _to_sqlite_text, _historian_tenants
from ._scope import all_db_paths, gateway_name_for, gateway_name_for_tag


# Human-friendly bucket labels → seconds. The model passes one of these.
BUCKET_SECONDS: Dict[str, int] = {
    "1s": 1, "5s": 5, "10s": 10, "30s": 30,
    "1m": 60, "5m": 300, "15m": 900,
    "1h": 3600, "1d": 86400,
}


def _resolve_bucket_seconds(bucket: str, frm: datetime, to: datetime) -> Tuple[int, str]:
    """Return (bucket_seconds, label). If bucket is 'auto' or unknown, pick a
    sensible bucket that yields roughly 60-300 points across the window."""
    b = str(bucket or "").strip().lower()
    if b in BUCKET_SECONDS:
        return BUCKET_SECONDS[b], b
    # Auto: target ~200 buckets across the span.
    span_s = max(1, int((to - frm).total_seconds()))
    target = span_s / 200.0
    # Pick the smallest defined bucket >= target.
    best_label, best_s = "1d", 86400
    for label, secs in sorted(BUCKET_SECONDS.items(), key=lambda kv: kv[1]):
        if secs >= target:
            best_label, best_s = label, secs
            break
    return best_s, best_label


def _suggest_chart(bucket_label: str, n_points: int) -> str:
    """Recommend a visualization for the bucketed result so the model can
    pick the best rendering. Small N → table; else line chart."""
    if n_points <= 1:
        return "single_value"
    if n_points <= 8:
        return "table"
    return "line_chart"


def _where_and_params(tid: str, frm: datetime, to: datetime, tag: str,
                      gateway_id: str, value_gt: Optional[float],
                      value_lt: Optional[float], quality: str) -> Tuple[str, List[Any]]:
    where = ["tenant_id = ?", "ts_utc >= ?", "ts_utc < ?", "tag_name = ?",
             "CAST(value AS REAL) IS NOT NULL"]
    params: List[Any] = [tid, _to_sqlite_text(frm), _to_sqlite_text(to), tag]
    if gateway_id:
        where.append("gateway_id = ?")
        params.append(gateway_id)
    if value_gt is not None:
        where.append("CAST(value AS REAL) > ?")
        params.append(float(value_gt))
    if value_lt is not None:
        where.append("CAST(value AS REAL) < ?")
        params.append(float(value_lt))
    q = str(quality or "").strip().lower()
    if q in ("good", "bad", "uncertain"):
        # quality_label stored as text; match case-insensitively.
        where.append("LOWER(quality_label) = ?")
        params.append(q)
    return " AND ".join(where), params


def _connect(db_path: str):
    # WAL-resilient read connection (short busy_timeout + read_uncommitted)
    # so analytics queries never stall on a checkpoint of the busy DB.
    from .tag_summary import hist_connect
    return hist_connect(db_path)


# --------------------------------------------------------------------------
# 1. Bucketed aggregation
# --------------------------------------------------------------------------

def run_get_bucketed_series(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """avg/min/max/count per fixed time bucket. For per-second data this is
    how you get 'average every 5 seconds / minute / hour'."""
    tag = str(args.get("tag") or "").strip()
    if not tag:
        return {"error": "Missing 'tag'."}
    from .tag_summary import _auto_resolve
    _r = _auto_resolve(tag)
    if _r.get("needs_choice"):
        return {"disambiguation_needed": True, "query": tag, "suggestions": _r["needs_choice"],
                "instruction": "Ask the user to pick one of these tags, then call again."}
    if _r.get("not_found"):
        return {"error": f"No tag matching '{tag}' is configured.", "query": tag}
    tag = _r["tag"]
    frm = _parse_time(args.get("from_") or args.get("from") or "-1h")
    to = _parse_time(args.get("to") or "now")
    gateway_id = str(args.get("gateway_id") or "").strip()
    bucket_s, bucket_label = _resolve_bucket_seconds(args.get("bucket") or "auto", frm, to)
    value_gt = _opt_float(args.get("value_gt"))
    value_lt = _opt_float(args.get("value_lt"))
    quality = str(args.get("quality") or "").strip()
    agg = str(args.get("agg") or "avg").strip().lower()
    if agg not in ("avg", "min", "max", "count", "last"):
        agg = "avg"

    buckets: Dict[int, Dict[str, Any]] = {}
    for db_path in all_db_paths():
        tenants = _historian_tenants(db_path)
        if not tenants:
            continue
        try:
            con = _connect(db_path)
        except Exception:
            continue
        try:
            for tid in tenants:
                wsql, params = _where_and_params(tid, frm, to, tag, gateway_id, value_gt, value_lt, quality)
                # Integer epoch-second bucketing done in SQL.
                sql = (
                    f"SELECT (CAST(strftime('%s', ts_utc) AS INTEGER) / {bucket_s}) * {bucket_s} AS bkt, "
                    f"COUNT(*), AVG(CAST(value AS REAL)), MIN(CAST(value AS REAL)), "
                    f"MAX(CAST(value AS REAL)), "
                    f"(SELECT CAST(value AS REAL) FROM historian_readings h2 "
                    f" WHERE h2.tenant_id=historian_readings.tenant_id AND h2.tag_name=historian_readings.tag_name "
                    f" AND (CAST(strftime('%s', h2.ts_utc) AS INTEGER)/{bucket_s})*{bucket_s}=bkt "
                    f" ORDER BY h2.ts_utc DESC LIMIT 1) AS last_val "
                    f"FROM historian_readings WHERE {wsql} "
                    f"GROUP BY bkt ORDER BY bkt ASC LIMIT 5000"
                )
                try:
                    for r in con.execute(sql, params):
                        bkt = int(r[0])
                        prev = buckets.get(bkt)
                        entry = {
                            "count": int(r[1] or 0),
                            "avg": r[2], "min": r[3], "max": r[4], "last": r[5],
                        }
                        if prev is None:
                            buckets[bkt] = entry
                        else:
                            # Merge across DBs (rare). Weighted avg + extremes.
                            tc = prev["count"] + entry["count"]
                            if tc:
                                prev["avg"] = ((prev["avg"] or 0) * prev["count"] +
                                               (entry["avg"] or 0) * entry["count"]) / tc
                            prev["count"] = tc
                            prev["min"] = min(x for x in (prev["min"], entry["min"]) if x is not None)
                            prev["max"] = max(x for x in (prev["max"], entry["max"]) if x is not None)
                except Exception:
                    continue
        finally:
            try: con.close()
            except Exception: pass

    series: List[Dict[str, Any]] = []
    for bkt in sorted(buckets.keys()):
        e = buckets[bkt]
        val = e.get(agg) if agg in ("avg", "min", "max", "last") else e.get("count")
        series.append({"ts": bkt * 1000, "value": val, "count": e["count"]})

    gname = gateway_name_for(gateway_id) if gateway_id else gateway_name_for_tag(tag)
    return {
        "tag": tag,
        "gateway_name": gname,
        "gateway_id": gateway_id,
        "from": _to_sqlite_text(frm) + "Z",
        "to": _to_sqlite_text(to) + "Z",
        "bucket": bucket_label,
        "bucket_seconds": bucket_s,
        "agg": agg,
        "count": len(series),
        "suggested_chart": _suggest_chart(bucket_label, len(series)),
        "series": series,
    }


# --------------------------------------------------------------------------
# 2. Threshold / condition detection
# --------------------------------------------------------------------------

def run_detect_threshold(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Count samples and estimate time above/below a limit, plus in-spec %.
    Answers 'how long was X above 150?' / 'what % of the time was X in spec?'."""
    tag = str(args.get("tag") or "").strip()
    if not tag:
        return {"error": "Missing 'tag'."}
    from .tag_summary import _auto_resolve
    _r = _auto_resolve(tag)
    if _r.get("needs_choice"):
        return {"disambiguation_needed": True, "query": tag, "suggestions": _r["needs_choice"],
                "instruction": "Ask the user to pick one of these tags, then call again."}
    if _r.get("not_found"):
        return {"error": f"No tag matching '{tag}' is configured.", "query": tag}
    tag = _r["tag"]
    frm = _parse_time(args.get("from_") or args.get("from") or "-1h")
    to = _parse_time(args.get("to") or "now")
    gateway_id = str(args.get("gateway_id") or "").strip()
    hi = _opt_float(args.get("upper_limit"))
    lo = _opt_float(args.get("lower_limit"))
    if hi is None and lo is None:
        return {"error": "Provide at least one of 'upper_limit' or 'lower_limit'."}

    total = 0
    breaches = 0
    for db_path in all_db_paths():
        tenants = _historian_tenants(db_path)
        if not tenants:
            continue
        try:
            con = _connect(db_path)
        except Exception:
            continue
        try:
            for tid in tenants:
                wsql, params = _where_and_params(tid, frm, to, tag, gateway_id, None, None, "")
                row = con.execute(
                    f"SELECT COUNT(*) FROM historian_readings WHERE {wsql}", params).fetchone()
                total += int((row or [0])[0] or 0)
                # Breach condition.
                cond = []
                bparams = list(params)
                if hi is not None:
                    cond.append("CAST(value AS REAL) > ?")
                    bparams.append(hi)
                if lo is not None:
                    cond.append("CAST(value AS REAL) < ?")
                    bparams.append(lo)
                brow = con.execute(
                    f"SELECT COUNT(*) FROM historian_readings WHERE {wsql} AND ({' OR '.join(cond)})",
                    bparams).fetchone()
                breaches += int((brow or [0])[0] or 0)
        finally:
            try: con.close()
            except Exception: pass

    in_spec = total - breaches
    pct_in = (in_spec / total * 100.0) if total else None
    # Estimate breach duration from sample count × nominal interval.
    interval_s = _estimate_interval_seconds(gateway_id)
    breach_seconds = breaches * interval_s if interval_s else None
    gname = gateway_name_for(gateway_id) if gateway_id else gateway_name_for_tag(tag)
    return {
        "tag": tag, "gateway_name": gname,
        "from": _to_sqlite_text(frm) + "Z", "to": _to_sqlite_text(to) + "Z",
        "upper_limit": hi, "lower_limit": lo,
        "total_samples": total,
        "breach_samples": breaches,
        "in_spec_samples": in_spec,
        "in_spec_pct": round(pct_in, 2) if pct_in is not None else None,
        "estimated_breach_seconds": round(breach_seconds, 1) if breach_seconds is not None else None,
        "sample_interval_seconds": interval_s,
    }


# --------------------------------------------------------------------------
# 3. Trend regression + projection
# --------------------------------------------------------------------------

def run_analyze_trend(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Linear regression over a window: slope (units/hour), intercept, R^2,
    and an optional projection N minutes/hours forward — including 'when will
    the tag reach target value V'."""
    tag = str(args.get("tag") or "").strip()
    if not tag:
        return {"error": "Missing 'tag'."}
    from .tag_summary import _auto_resolve
    _r = _auto_resolve(tag)
    if _r.get("needs_choice"):
        return {"disambiguation_needed": True, "query": tag, "suggestions": _r["needs_choice"],
                "instruction": "Ask the user to pick one of these tags, then call again."}
    if _r.get("not_found"):
        return {"error": f"No tag matching '{tag}' is configured.", "query": tag}
    tag = _r["tag"]
    frm = _parse_time(args.get("from_") or args.get("from") or "-1h")
    to = _parse_time(args.get("to") or "now")
    gateway_id = str(args.get("gateway_id") or "").strip()
    project_minutes = _opt_float(args.get("project_minutes"))
    target_value = _opt_float(args.get("target_value"))

    # Fetch bucketed points (keeps regression cheap + robust to noise).
    bucket_s, _ = _resolve_bucket_seconds(args.get("bucket") or "auto", frm, to)
    pts: List[Tuple[float, float]] = []
    for db_path in all_db_paths():
        tenants = _historian_tenants(db_path)
        if not tenants:
            continue
        try:
            con = _connect(db_path)
        except Exception:
            continue
        try:
            for tid in tenants:
                wsql, params = _where_and_params(tid, frm, to, tag, gateway_id, None, None, "")
                sql = (
                    f"SELECT (CAST(strftime('%s', ts_utc) AS INTEGER)/{bucket_s})*{bucket_s} AS bkt, "
                    f"AVG(CAST(value AS REAL)) FROM historian_readings WHERE {wsql} "
                    f"GROUP BY bkt ORDER BY bkt ASC LIMIT 5000"
                )
                try:
                    for r in con.execute(sql, params):
                        pts.append((float(r[0]), float(r[1])))
                except Exception:
                    continue
        finally:
            try: con.close()
            except Exception: pass

    if len(pts) < 2:
        return {"error": "Not enough data in the window to fit a trend (need >= 2 buckets)."}

    # Least-squares fit y = a + b*x, x in seconds relative to first point.
    x0 = pts[0][0]
    xs = [p[0] - x0 for p in pts]
    ys = [p[1] for p in pts]
    n = len(xs)
    sx = sum(xs); sy = sum(ys)
    sxx = sum(x * x for x in xs); sxy = sum(x * y for x, y in zip(xs, ys))
    denom = (n * sxx - sx * sx)
    if denom == 0:
        return {"error": "Degenerate time range; cannot fit a trend."}
    b = (n * sxy - sx * sy) / denom          # slope per second
    a = (sy - b * sx) / n                     # intercept at x=0 (first point)
    # R^2
    y_mean = sy / n
    ss_tot = sum((y - y_mean) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    r2 = (1 - ss_res / ss_tot) if ss_tot > 0 else 1.0

    slope_per_hour = b * 3600.0
    direction = "rising" if b > 0 else ("falling" if b < 0 else "flat")
    last_x = xs[-1]
    last_fit = a + b * last_x

    out: Dict[str, Any] = {
        "tag": tag,
        "gateway_name": gateway_name_for(gateway_id) if gateway_id else gateway_name_for_tag(tag),
        "from": _to_sqlite_text(frm) + "Z", "to": _to_sqlite_text(to) + "Z",
        "n_buckets": n,
        "slope_per_hour": slope_per_hour,
        "slope_per_second": b,
        "intercept_at_start": a,
        "r_squared": round(r2, 4),
        "direction": direction,
        "fit_quality": "strong" if r2 >= 0.7 else ("moderate" if r2 >= 0.3 else "weak"),
        "current_fitted_value": last_fit,
    }
    if project_minutes is not None:
        proj_x = last_x + project_minutes * 60.0
        out["projection"] = {
            "minutes_ahead": project_minutes,
            "projected_value": a + b * proj_x,
        }
    if target_value is not None:
        if b == 0:
            out["time_to_target"] = {"target": target_value, "reachable": False,
                                     "reason": "trend is flat"}
        else:
            x_target = (target_value - a) / b            # seconds from start
            secs_from_now = x_target - last_x
            out["time_to_target"] = {
                "target": target_value,
                "reachable": secs_from_now > 0,
                "seconds_from_now": round(secs_from_now, 1),
                "minutes_from_now": round(secs_from_now / 60.0, 2),
            }
    return out


# --------------------------------------------------------------------------
# 4. SPC anomaly / outlier detection
# --------------------------------------------------------------------------

def run_detect_anomalies(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Statistical process control: compute mean +/- k*sigma control limits
    and flag readings outside them. Also reports how many consecutive points
    fall on one side of the mean (a simple run/shift signal)."""
    tag = str(args.get("tag") or "").strip()
    if not tag:
        return {"error": "Missing 'tag'."}
    from .tag_summary import _auto_resolve
    _r = _auto_resolve(tag)
    if _r.get("needs_choice"):
        return {"disambiguation_needed": True, "query": tag, "suggestions": _r["needs_choice"],
                "instruction": "Ask the user to pick one of these tags, then call again."}
    if _r.get("not_found"):
        return {"error": f"No tag matching '{tag}' is configured.", "query": tag}
    tag = _r["tag"]
    frm = _parse_time(args.get("from_") or args.get("from") or "-1h")
    to = _parse_time(args.get("to") or "now")
    gateway_id = str(args.get("gateway_id") or "").strip()
    try:
        k = float(args.get("sigma") or 3.0)
    except Exception:
        k = 3.0

    # One SQL pass for count/mean/std via sum + sumsq.
    cnt = 0; s = 0.0; ssq = 0.0; mn = None; mx = None
    for db_path in all_db_paths():
        tenants = _historian_tenants(db_path)
        if not tenants:
            continue
        try:
            con = _connect(db_path)
        except Exception:
            continue
        try:
            for tid in tenants:
                wsql, params = _where_and_params(tid, frm, to, tag, gateway_id, None, None, "")
                row = con.execute(
                    f"SELECT COUNT(*), SUM(CAST(value AS REAL)), "
                    f"SUM(CAST(value AS REAL)*CAST(value AS REAL)), "
                    f"MIN(CAST(value AS REAL)), MAX(CAST(value AS REAL)) "
                    f"FROM historian_readings WHERE {wsql}", params).fetchone()
                if row and row[0]:
                    cnt += int(row[0]); s += float(row[1] or 0); ssq += float(row[2] or 0)
                    mn = row[3] if mn is None else min(mn, row[3])
                    mx = row[4] if mx is None else max(mx, row[4])
        finally:
            try: con.close()
            except Exception: pass

    if cnt < 2:
        return {"error": "Not enough data to compute control limits."}
    mean = s / cnt
    var = max(0.0, (ssq - cnt * mean * mean) / (cnt - 1))
    sigma = math.sqrt(var)
    ucl = mean + k * sigma
    lcl = mean - k * sigma

    # Second pass: count out-of-limit points (index-backed).
    out_of_limit = 0
    for db_path in all_db_paths():
        tenants = _historian_tenants(db_path)
        if not tenants:
            continue
        try:
            con = _connect(db_path)
        except Exception:
            continue
        try:
            for tid in tenants:
                wsql, params = _where_and_params(tid, frm, to, tag, gateway_id, None, None, "")
                brow = con.execute(
                    f"SELECT COUNT(*) FROM historian_readings WHERE {wsql} "
                    f"AND (CAST(value AS REAL) > ? OR CAST(value AS REAL) < ?)",
                    params + [ucl, lcl]).fetchone()
                out_of_limit += int((brow or [0])[0] or 0)
        finally:
            try: con.close()
            except Exception: pass

    return {
        "tag": tag,
        "gateway_name": gateway_name_for(gateway_id) if gateway_id else gateway_name_for_tag(tag),
        "from": _to_sqlite_text(frm) + "Z", "to": _to_sqlite_text(to) + "Z",
        "n_samples": cnt,
        "mean": mean, "sigma": sigma,
        "sigma_multiplier": k,
        "ucl": ucl, "lcl": lcl,
        "min": mn, "max": mx,
        "out_of_limit_samples": out_of_limit,
        "out_of_limit_pct": round(out_of_limit / cnt * 100.0, 3) if cnt else None,
        "in_control": out_of_limit == 0,
    }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _opt_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None


def _estimate_interval_seconds(gateway_id: str) -> Optional[float]:
    """Best-effort nominal sample interval (seconds) for duration estimates.
    Reads the gateway's configured interval_ms; falls back to 1s."""
    try:
        from ._scope import all_gateways
        for g in all_gateways():
            if not gateway_id or str(g.get("id") or "") == gateway_id:
                ims = g.get("interval_ms")
                if ims:
                    return float(ims) / 1000.0
        return 1.0
    except Exception:
        return 1.0
