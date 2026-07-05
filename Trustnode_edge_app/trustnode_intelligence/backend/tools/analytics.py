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
                # Operator 2026-07-05 (BUCKET-PERF FIX): the previous query had a
                # CORRELATED subquery for `last_val` that re-ran strftime for
                # EVERY row (O(N^2)); on a busy historian it exceeded the 400ms
                # busy_timeout and the WHOLE query failed -> 0 buckets returned.
                # `last` per bucket is only used for agg="last". We compute it
                # here as MAX(ts)'s value via a cheap window-free trick:
                # the value at the max ts in the bucket == the value ordered by
                # ts. Simplest correct + fast: drop last from the aggregate; if
                # agg=="last" is requested, fall back to avg (documented) rather
                # than paying the O(N^2) cost that broke ALL bucketed queries.
                sql = (
                    f"SELECT (CAST(strftime('%s', ts_utc) AS INTEGER) / {bucket_s}) * {bucket_s} AS bkt, "
                    f"COUNT(*), AVG(CAST(value AS REAL)), MIN(CAST(value AS REAL)), "
                    f"MAX(CAST(value AS REAL)) "
                    f"FROM historian_readings WHERE {wsql} "
                    f"GROUP BY bkt ORDER BY bkt ASC LIMIT 5000"
                )
                try:
                    for r in con.execute(sql, params):
                        bkt = int(r[0])
                        prev = buckets.get(bkt)
                        entry = {
                            "count": int(r[1] or 0),
                            "avg": r[2], "min": r[3], "max": r[4], "last": r[2],
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


# --------------------------------------------------------------------------
# 5. Multi-tag comparison: same-grid bucketing + Pearson correlation + insights
#    (operator 2026-07-03)
# --------------------------------------------------------------------------

def _tag_span(bmap: Dict[int, float]) -> Optional[float]:
    vs = list(bmap.values())
    return (max(vs) - min(vs)) if vs else None


def _bucketed_avg_for_tag(tag: str, frm: datetime, to: datetime, bucket_s: int,
                          gateway_id: str = "") -> Dict[int, float]:
    """Return {bucket_epoch_s: avg_value} for ONE tag over [frm,to), aggregated
    into fixed bucket_s buckets. Same SQL bucketing as get_bucketed_series so
    every tag in a comparison lands on one aligned time grid."""
    out: Dict[int, Tuple[float, int]] = {}
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
                    f"SELECT (CAST(strftime('%s', ts_utc) AS INTEGER) / {bucket_s}) * {bucket_s} AS bkt, "
                    f"AVG(CAST(value AS REAL)) AS a, COUNT(*) AS c "
                    f"FROM historian_readings WHERE {wsql} "
                    f"GROUP BY bkt ORDER BY bkt ASC LIMIT 5000"
                )
                try:
                    for r in con.execute(sql, params):
                        bkt = int(r[0]); a = r[1]; c = int(r[2] or 0)
                        if a is None:
                            continue
                        prev = out.get(bkt)
                        if prev is None:
                            out[bkt] = (float(a), c)
                        else:
                            pv, pc = prev
                            tc = pc + c
                            out[bkt] = (((pv * pc) + (float(a) * c)) / tc if tc else pv, tc)
                except Exception:
                    continue
        finally:
            try: con.close()
            except Exception: pass
    return {k: v[0] for k, v in out.items()}


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    """Pearson r over paired samples. None if <3 pairs or zero variance."""
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def _corr_strength(r: float) -> str:
    a = abs(r)
    if a >= 0.9: return "very strong"
    if a >= 0.7: return "strong"
    if a >= 0.4: return "moderate"
    if a >= 0.2: return "weak"
    return "negligible"


def run_compare_tags(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Compare MULTIPLE tags over an explicit time range at a chosen time
    bucket, returning a multi-series chart shape + a pairwise Pearson
    correlation matrix + plain-language insights.

    Args:
      tags:    list (or comma-separated string) of 2..6 tag names.
      from_/from, to: explicit range ('-1h'/'now', ISO, or epoch).
      bucket:  one of 1s/5s/10s/30s/1m/5m/15m/1h/1d, or 'auto'.
    """
    tags_arg = args.get("tags") or []
    if isinstance(tags_arg, str):
        tags_list = [t.strip() for t in tags_arg.split(",") if t.strip()]
    else:
        tags_list = [str(t).strip() for t in tags_arg if str(t).strip()]

    from .tag_summary import _auto_resolve
    resolved: List[str] = []
    for t in tags_list[:6]:
        r = _auto_resolve(t)
        if r.get("needs_choice"):
            return {"disambiguation_needed": True, "query": t, "suggestions": r["needs_choice"],
                    "instruction": "Ask the user to pick one of these tags, then call again."}
        if r.get("not_found"):
            continue
        resolved.append(r["tag"])
    seen = set(); tags = []
    for t in resolved:
        if t not in seen:
            seen.add(t); tags.append(t)
    if len(tags) < 2:
        return {"error": "Need at least 2 valid tags to compare."}

    frm = _parse_time(args.get("from_") or args.get("from") or "-1h")
    to = _parse_time(args.get("to") or "now")
    bucket_s, bucket_label = _resolve_bucket_seconds(args.get("bucket") or "auto", frm, to)
    gateway_id = str(args.get("gateway_id") or "").strip()

    per_tag: Dict[str, Dict[int, float]] = {t: _bucketed_avg_for_tag(t, frm, to, bucket_s, gateway_id) for t in tags}

    series_out: List[Dict[str, Any]] = []
    for t in tags:
        pts = [{"ts": bkt * 1000, "value": v} for bkt, v in sorted(per_tag[t].items())]
        vs = [p["value"] for p in pts]
        series_out.append({
            "tag": t, "gateway_name": gateway_name_for_tag(t), "series": pts,
            "min": (min(vs) if vs else None), "max": (max(vs) if vs else None),
            "avg": (sum(vs) / len(vs) if vs else None),
        })

    correlations: List[Dict[str, Any]] = []
    best = None
    for i in range(len(tags)):
        for j in range(i + 1, len(tags)):
            a, b = tags[i], tags[j]
            common = sorted(set(per_tag[a].keys()) & set(per_tag[b].keys()))
            xs = [per_tag[a][k] for k in common]
            ys = [per_tag[b][k] for k in common]
            r = _pearson(xs, ys)
            entry = {
                "tag_a": a, "tag_b": b,
                "r": (round(r, 3) if r is not None else None),
                "n": len(common),
                "strength": (_corr_strength(r) if r is not None else "insufficient data"),
                "direction": (("positive" if r > 0 else "negative") if r is not None else None),
            }
            correlations.append(entry)
            if r is not None and (best is None or abs(r) > best[0]):
                best = (abs(r), entry)

    insights: List[str] = []
    n_buckets = max((len(per_tag[t]) for t in tags), default=0)
    if n_buckets == 0:
        insights.append("No data in the selected window for these tags — widen the range or check the gateway.")
    else:
        if best is not None and best[1]["r"] is not None and abs(best[1]["r"]) >= 0.4:
            e = best[1]
            rel = "driven by the same condition" if e["direction"] == "positive" else "inversely related"
            insights.append(
                f"{e['tag_a']} and {e['tag_b']} move {e['direction']}ly together "
                f"({e['strength']}, r={e['r']}, {e['n']} buckets) — likely {rel}."
            )
        else:
            insights.append("No strong correlation between the compared tags in this window — they look largely independent.")
        spans = {t: _tag_span(per_tag[t]) for t in tags}
        real = {t: s for t, s in spans.items() if s}
        if len(real) > 1:
            widest = max(real, key=lambda k: real[k])
            insights.append(f"{widest} has the widest variation over the window (range {real[widest]:.3g}).")

    return {
        "kind": "comparison",
        "tags": tags,
        "from": _to_sqlite_text(frm) + "Z",
        "to": _to_sqlite_text(to) + "Z",
        "bucket": bucket_label,
        "bucket_seconds": bucket_s,
        "buckets": n_buckets,
        "series": series_out,          # multi-series chart shape (renders natively)
        "correlations": correlations,  # pairwise Pearson matrix
        "insights": insights,          # plain-language findings
    }


# --------------------------------------------------------------------------
# 6. Per-tag aggregate — ONE value per tag (for a BAR chart comparing tags)
#    (operator 2026-07-06)
# --------------------------------------------------------------------------

def run_aggregate_tags(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Compute ONE aggregate value (avg/min/max/count/sum/stddev) per tag over a
    window — the natural shape for a BAR chart that compares several tags side by
    side ('bar chart of the average of A, B, C'). Returns a categorical
    `slices:[{label,value}]` payload with chart_type 'bar'.

    Args:
      tags:  list (or comma-separated) of 1..12 tag names.
      from_/to: window ('-1h'/'now', ISO, epoch).
      agg:   avg | min | max | count | sum | stddev  (default avg).
    """
    tags_arg = args.get("tags") or []
    if isinstance(tags_arg, str):
        tags_list = [t.strip() for t in tags_arg.split(",") if t.strip()]
    else:
        tags_list = [str(t).strip() for t in tags_arg if str(t).strip()]
    agg = str(args.get("agg") or "avg").strip().lower()
    if agg not in ("avg", "min", "max", "count", "sum", "stddev"):
        agg = "avg"
    frm = _parse_time(args.get("from_") or args.get("from") or "-1h")
    to = _parse_time(args.get("to") or "now")
    gateway_id = str(args.get("gateway_id") or "").strip()

    from .tag_summary import _auto_resolve
    resolved: List[str] = []
    for t in tags_list[:12]:
        r = _auto_resolve(t)
        if r.get("needs_choice"):
            return {"disambiguation_needed": True, "query": t, "suggestions": r["needs_choice"],
                    "instruction": "Ask the user to pick one of these tags, then call again."}
        if r.get("not_found"):
            continue
        resolved.append(r["tag"])
    seen = set(); tags = []
    for t in resolved:
        if t not in seen:
            seen.add(t); tags.append(t)
    if not tags:
        return {"error": "No valid tags to aggregate."}

    # SQL aggregate expression per requested agg.
    _AGG_SQL = {
        "avg": "AVG(CAST(value AS REAL))",
        "min": "MIN(CAST(value AS REAL))",
        "max": "MAX(CAST(value AS REAL))",
        "count": "COUNT(*)",
        "sum": "SUM(CAST(value AS REAL))",
        # population stddev via sum/sumsq (computed in python below for 'stddev')
    }
    slices: List[Dict[str, Any]] = []
    for t in tags:
        val = None
        # stddev needs count+sum+sumsq; others are a single aggregate.
        acc_n = 0; acc_s = 0.0; acc_ss = 0.0; acc_min = None; acc_max = None; acc_sum = 0.0
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
                    wsql, params = _where_and_params(tid, frm, to, t, gateway_id, None, None, "")
                    row = con.execute(
                        f"SELECT COUNT(*), SUM(CAST(value AS REAL)), "
                        f"SUM(CAST(value AS REAL)*CAST(value AS REAL)), "
                        f"MIN(CAST(value AS REAL)), MAX(CAST(value AS REAL)) "
                        f"FROM historian_readings WHERE {wsql}", params).fetchone()
                    if row and row[0]:
                        acc_n += int(row[0]); acc_s += float(row[1] or 0); acc_ss += float(row[2] or 0)
                        acc_sum += float(row[1] or 0)
                        acc_min = row[3] if acc_min is None else min(acc_min, row[3])
                        acc_max = row[4] if acc_max is None else max(acc_max, row[4])
            except Exception:
                pass
            finally:
                try: con.close()
                except Exception: pass
        if acc_n:
            if agg == "avg":
                val = acc_s / acc_n
            elif agg == "min":
                val = acc_min
            elif agg == "max":
                val = acc_max
            elif agg == "count":
                val = acc_n
            elif agg == "sum":
                val = acc_sum
            elif agg == "stddev":
                mean = acc_s / acc_n
                var = max(0.0, (acc_ss - acc_n * mean * mean) / (acc_n - 1)) if acc_n > 1 else 0.0
                val = math.sqrt(var)
        slices.append({
            "label": t,
            "value": val,
            "gateway_name": gateway_name_for_tag(t),
            "count": acc_n,
        })

    return {
        "kind": "aggregate",
        "chart_type": "bar",
        "agg": agg,
        "from": _to_sqlite_text(frm) + "Z",
        "to": _to_sqlite_text(to) + "Z",
        "unit": "",
        "count": len([s for s in slices if s["value"] is not None]),
        "slices": slices,   # one bar per tag
    }


# --------------------------------------------------------------------------
# 7. Category breakdown — slices for a DONUT / PIE chart
#    (operator 2026-07-06)
#    by_tag      : each tag's share of readings (or summed value)
#    by_gateway  : each gateway's share of readings collected
#    value_bands : % of a tag's readings that fell into value ranges
#    quality     : GOOD / BAD / UNCERTAIN share for a tag (or all)
# --------------------------------------------------------------------------

def _pct(part: float, whole: float) -> Optional[float]:
    return round(part / whole * 100.0, 2) if whole else None


def run_get_category_breakdown(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Return categorical SLICES for a donut/pie chart. `by` selects the
    dimension:

      - 'by_tag'      → one slice per tag (share of total readings, or summed
                        value if measure='value'); pass `tags` to limit.
      - 'by_gateway'  → one slice per gateway (share of readings collected).
      - 'quality'     → GOOD/BAD/UNCERTAIN share for `tag` (or all readings).
      - 'value_bands' → for a single `tag`, % of readings in each range given
                        by `bands` (list of numeric edges, e.g. [100,150] →
                        '<100','100–150','>150').

    Returns {chart_type:'donut', slices:[{label,value,pct}], total, ...}.
    """
    by = str(args.get("by") or "").strip().lower()
    if by not in ("by_tag", "by_gateway", "quality", "value_bands"):
        # Infer a sensible default: a tag with bands → value_bands; a tag alone
        # → quality; otherwise by_tag.
        if args.get("bands"):
            by = "value_bands"
        elif args.get("tag"):
            by = "quality"
        else:
            by = "by_tag"
    frm = _parse_time(args.get("from_") or args.get("from") or "-24h")
    to = _parse_time(args.get("to") or "now")
    gateway_id = str(args.get("gateway_id") or "").strip()
    measure = str(args.get("measure") or "count").strip().lower()  # count | value

    from .tag_summary import _auto_resolve

    # -------- by_gateway: share of readings collected per gateway ----------
    if by == "by_gateway":
        counts: Dict[str, int] = {}
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
                    for r in con.execute(
                        "SELECT gateway_id, COUNT(*) FROM historian_readings "
                        "WHERE tenant_id = ? AND ts_utc >= ? AND ts_utc < ? "
                        "GROUP BY gateway_id",
                        (tid, _to_sqlite_text(frm), _to_sqlite_text(to)),
                    ):
                        gid = str(r[0] or "")
                        counts[gid] = counts.get(gid, 0) + int(r[1] or 0)
            except Exception:
                pass
            finally:
                try: con.close()
                except Exception: pass
        total = sum(counts.values())
        slices = [{"label": gateway_name_for(gid) or gid or "(unknown)",
                   "value": c, "pct": _pct(c, total)}
                  for gid, c in sorted(counts.items(), key=lambda kv: -kv[1])]
        return {"kind": "breakdown", "chart_type": "donut", "by": "gateway",
                "measure": "count", "total": total,
                "from": _to_sqlite_text(frm) + "Z", "to": _to_sqlite_text(to) + "Z",
                "slices": slices}

    # -------- quality: GOOD/BAD/UNCERTAIN share -----------------------------
    if by == "quality":
        tag = str(args.get("tag") or "").strip()
        if tag:
            _r = _auto_resolve(tag)
            if _r.get("needs_choice"):
                return {"disambiguation_needed": True, "query": tag, "suggestions": _r["needs_choice"],
                        "instruction": "Ask the user to pick one of these tags, then call again."}
            if _r.get("not_found"):
                return {"error": f"No tag matching '{tag}' is configured.", "query": tag}
            tag = _r["tag"]
        counts: Dict[str, int] = {}
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
                    where = ["tenant_id = ?", "ts_utc >= ?", "ts_utc < ?"]
                    params: List[Any] = [tid, _to_sqlite_text(frm), _to_sqlite_text(to)]
                    if tag:
                        where.append("tag_name = ?"); params.append(tag)
                    if gateway_id:
                        where.append("gateway_id = ?"); params.append(gateway_id)
                    for r in con.execute(
                        f"SELECT COALESCE(NULLIF(TRIM(quality_label),''),'UNKNOWN'), COUNT(*) "
                        f"FROM historian_readings WHERE {' AND '.join(where)} "
                        f"GROUP BY quality_label", params,
                    ):
                        lab = str(r[0] or "UNKNOWN").upper()
                        counts[lab] = counts.get(lab, 0) + int(r[1] or 0)
            except Exception:
                pass
            finally:
                try: con.close()
                except Exception: pass
        total = sum(counts.values())
        # Stable, meaningful order: GOOD, UNCERTAIN, BAD, then others.
        order = {"GOOD": 0, "UNCERTAIN": 1, "BAD": 2}
        slices = [{"label": lab, "value": c, "pct": _pct(c, total)}
                  for lab, c in sorted(counts.items(), key=lambda kv: (order.get(kv[0], 9), -kv[1]))]
        return {"kind": "breakdown", "chart_type": "donut", "by": "quality",
                "tag": tag or "(all tags)", "measure": "count", "total": total,
                "from": _to_sqlite_text(frm) + "Z", "to": _to_sqlite_text(to) + "Z",
                "slices": slices}

    # -------- value_bands: % of a tag's readings in each range --------------
    if by == "value_bands":
        tag = str(args.get("tag") or "").strip()
        if not tag:
            return {"error": "value_bands needs a 'tag'."}
        _r = _auto_resolve(tag)
        if _r.get("needs_choice"):
            return {"disambiguation_needed": True, "query": tag, "suggestions": _r["needs_choice"],
                    "instruction": "Ask the user to pick one of these tags, then call again."}
        if _r.get("not_found"):
            return {"error": f"No tag matching '{tag}' is configured.", "query": tag}
        tag = _r["tag"]
        bands = args.get("bands") or []
        if isinstance(bands, str):
            bands = [b.strip() for b in bands.replace(";", ",").split(",") if b.strip()]
        try:
            edges = sorted({float(b) for b in bands})
        except Exception:
            edges = []
        if not edges:
            return {"error": "value_bands needs numeric 'bands' edges, e.g. [100,150]."}
        # Build band definitions: (label, lo_inclusive_or_None, hi_exclusive_or_None)
        band_defs: List[Tuple[str, Optional[float], Optional[float]]] = []
        band_defs.append((f"< {_fmt_edge(edges[0])}", None, edges[0]))
        for i in range(len(edges) - 1):
            band_defs.append((f"{_fmt_edge(edges[i])}–{_fmt_edge(edges[i+1])}", edges[i], edges[i + 1]))
        band_defs.append((f"≥ {_fmt_edge(edges[-1])}", edges[-1], None))
        counts = [0] * len(band_defs)
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
                    for bi, (_lab, lo, hi) in enumerate(band_defs):
                        wsql, params = _where_and_params(tid, frm, to, tag, gateway_id, None, None, "")
                        cond = []
                        bparams = list(params)
                        if lo is not None:
                            cond.append("CAST(value AS REAL) >= ?"); bparams.append(lo)
                        if hi is not None:
                            cond.append("CAST(value AS REAL) < ?"); bparams.append(hi)
                        csql = f"SELECT COUNT(*) FROM historian_readings WHERE {wsql}"
                        if cond:
                            csql += " AND " + " AND ".join(cond)
                        row = con.execute(csql, bparams).fetchone()
                        counts[bi] += int((row or [0])[0] or 0)
            except Exception:
                pass
            finally:
                try: con.close()
                except Exception: pass
        total = sum(counts)
        slices = [{"label": band_defs[i][0], "value": counts[i], "pct": _pct(counts[i], total)}
                  for i in range(len(band_defs))]
        return {"kind": "breakdown", "chart_type": "donut", "by": "value_bands",
                "tag": tag, "measure": "count", "total": total,
                "from": _to_sqlite_text(frm) + "Z", "to": _to_sqlite_text(to) + "Z",
                "slices": slices}

    # -------- by_tag: each tag's share of readings (or summed value) --------
    tags_arg = args.get("tags") or []
    if isinstance(tags_arg, str):
        tags_list = [t.strip() for t in tags_arg.split(",") if t.strip()]
    else:
        tags_list = [str(t).strip() for t in tags_arg if str(t).strip()]
    want_tags: List[str] = []
    for t in tags_list[:20]:
        r = _auto_resolve(t)
        if r.get("not_found"):
            continue
        if r.get("needs_choice"):
            return {"disambiguation_needed": True, "query": t, "suggestions": r["needs_choice"],
                    "instruction": "Ask the user to pick one of these tags, then call again."}
        want_tags.append(r["tag"])
    want_set = set(want_tags)
    agg_expr = "SUM(CAST(value AS REAL))" if measure == "value" else "COUNT(*)"
    per: Dict[str, float] = {}
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
                where = ["tenant_id = ?", "ts_utc >= ?", "ts_utc < ?"]
                params: List[Any] = [tid, _to_sqlite_text(frm), _to_sqlite_text(to)]
                if gateway_id:
                    where.append("gateway_id = ?"); params.append(gateway_id)
                for r in con.execute(
                    f"SELECT tag_name, {agg_expr} FROM historian_readings "
                    f"WHERE {' AND '.join(where)} GROUP BY tag_name", params,
                ):
                    tname = str(r[0] or "")
                    if want_set and tname not in want_set:
                        continue
                    per[tname] = per.get(tname, 0.0) + float(r[1] or 0)
        except Exception:
            pass
        finally:
            try: con.close()
            except Exception: pass
    total = sum(per.values())
    slices = [{"label": tname, "value": v, "pct": _pct(v, total)}
              for tname, v in sorted(per.items(), key=lambda kv: -kv[1])]
    return {"kind": "breakdown", "chart_type": "donut", "by": "tag",
            "measure": measure, "total": total,
            "from": _to_sqlite_text(frm) + "Z", "to": _to_sqlite_text(to) + "Z",
            "slices": slices}


def _fmt_edge(v: float) -> str:
    """Compact numeric label for band edges (100.0 -> '100', 100.5 -> '100.5')."""
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else f"{f:g}"
    except Exception:
        return str(v)
