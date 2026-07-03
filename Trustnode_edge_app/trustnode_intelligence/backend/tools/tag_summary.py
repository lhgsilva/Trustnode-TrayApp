"""Tag-level analytics tools — min/max/avg/count/stddev + period comparison.

Reads from the existing historian. When data_source='local' we call
app_store.get_historian_rows_range directly. When data_source='cloud'
we route through the canonical-customer-DB read path.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple


_REL_RE = re.compile(r"^-(\d+)(s|m|h|d|w)$", re.IGNORECASE)


def _parse_time(value: str) -> datetime:
    """Accept ISO-8601, relative (-8h, -7d, -30m), or 'now'."""
    s = (value or "").strip()
    if not s or s.lower() == "now":
        return datetime.now(timezone.utc)
    m = _REL_RE.match(s)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        delta = {"s": timedelta(seconds=n), "m": timedelta(minutes=n),
                 "h": timedelta(hours=n), "d": timedelta(days=n),
                 "w": timedelta(weeks=n)}[unit]
        return datetime.now(timezone.utc) - delta
    # Try ISO-8601
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        raise ValueError(f"Cannot parse time '{value}' — use ISO-8601, relative ('-8h'), or 'now'.")


def _to_sqlite_text(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


import time as _time_mod


def hist_connect(db_path: str):
    """Open a WAL-RESILIENT read-only connection to a historian DB.

    Operator 2026-07-02 (WEDGE FIX): the app_store.db / telemetry.db WAL can
    grow large under constant historian writes, and a checkpoint in progress
    can make a normal read BLOCK for the full busy_timeout (seconds). When
    the AI runs several tool queries, those stalls tie up the executor and
    the module wedges. We open with:
      - a SHORT busy_timeout (400ms) so a read never waits long, and
      - `PRAGMA read_uncommitted=1` so the reader sees committed WAL pages
        without contending on the checkpoint lock.
    mode=ro + these pragmas = reads are effectively non-blocking.
    """
    import sqlite3 as _sqlite3
    con = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
    try:
        con.execute("PRAGMA busy_timeout=400")
        con.execute("PRAGMA read_uncommitted=1")
    except Exception:
        pass
    return con


# Cache the resolved tenant_id list per DB path for a while — it almost never
# changes, and `SELECT DISTINCT tenant_id` is a full scan of ~640k rows.
_TENANTS_CACHE: dict = {}
_TENANTS_TTL = 60.0


def _historian_tenants(db_path: str) -> List[str]:
    """Return the distinct tenant_ids present in a DB's historian_readings,
    cached 60s (the value is stable; the DISTINCT scan is expensive)."""
    now = _time_mod.monotonic()
    hit = _TENANTS_CACHE.get(db_path)
    if hit and (now - hit[0]) < _TENANTS_TTL:
        return list(hit[1])
    out: List[str] = []
    try:
        con = hist_connect(db_path)
    except Exception:
        return out
    try:
        for (tid,) in con.execute("SELECT DISTINCT tenant_id FROM historian_readings"):
            if tid is not None:
                out.append(str(tid))
    except _sqlite3.OperationalError:
        pass
    finally:
        try: con.close()
        except Exception: pass
    _TENANTS_CACHE[db_path] = (now, list(out))
    return out


def _fetch_rows(tag: str, frm: datetime, to: datetime, gateway_id: str,
                data_source: str) -> List[Dict[str, Any]]:
    """Pull rows from local SQLite across every candidate workspace DB.

    Operator 2026-07-02 (PERF): constrain by tenant_id so the composite
    index (tenant_id, tag_name, ts_utc DESC) is used as an index SEARCH
    instead of a full SCAN. We loop the DB's actual tenant_id(s) rather
    than hardcoding one — correct for single- and multi-tenant edges.
    """
    from ._scope import all_db_paths
    seen_ts = set()
    rows: List[Dict[str, Any]] = []
    for db_path in all_db_paths():
        tenants = _historian_tenants(db_path)
        if not tenants:
            continue
        try:
            con = hist_connect(db_path)
        except Exception:
            continue
        try:
            for tid in tenants:
                where = ["tenant_id = ?", "ts_utc >= ?", "ts_utc < ?", "tag_name = ?"]
                params: List[Any] = [tid, _to_sqlite_text(frm), _to_sqlite_text(to), tag]
                if gateway_id:
                    where.append("gateway_id = ?")
                    params.append(gateway_id)
                sql = (
                    f"SELECT ts_utc, gateway_id, tag_name, value, quality "
                    f"FROM historian_readings WHERE {' AND '.join(where)} "
                    f"ORDER BY ts_utc ASC LIMIT 200000"
                )
                try:
                    for r in con.execute(sql, params):
                        key = (r[0], r[1], r[2])  # ts, gateway, tag — dedup across DBs
                        if key in seen_ts:
                            continue
                        seen_ts.add(key)
                        rows.append({
                            "ts_utc": r[0], "gateway_id": r[1], "tag_name": r[2],
                            "value": r[3], "quality": r[4],
                        })
                except _sqlite3.OperationalError:
                    pass
        finally:
            try: con.close()
            except Exception: pass
    rows.sort(key=lambda x: x["ts_utc"])
    return rows


def _fetch_stats_sql(tag: str, frm: datetime, to: datetime, gateway_id: str) -> Dict[str, Any]:
    """SQL-side aggregation for tag stats. Returns count/min/max/avg/stddev
    computed IN SQLite — no need to ship up to 200k raw rows into Python.

    Operator 2026-07-02 (PERF): the old path pulled every row and did the
    math in Python (memory + CPU + GIL). For a "what's the average of X
    over 8h" question we only need one aggregate row. SQLite computes
    COUNT/MIN/MAX/AVG natively and, with the tenant_id-led index, does it
    as an index range scan. stddev is derived from SUM(x) and SUM(x*x)
    (SQLite has no STDDEV) in a single pass. Returns the SAME shape as the
    old _stats() so callers are unchanged.
    """
    import sqlite3 as _sqlite3
    from ._scope import all_db_paths
    agg = {"count": 0, "sum": 0.0, "sumsq": 0.0, "min": None, "max": None,
           "first": None, "last": None, "first_ts": None, "last_ts": None}
    for db_path in all_db_paths():
        tenants = _historian_tenants(db_path)
        if not tenants:
            continue
        try:
            con = hist_connect(db_path)
        except Exception:
            continue
        try:
            for tid in tenants:
                where = ["tenant_id = ?", "ts_utc >= ?", "ts_utc < ?", "tag_name = ?",
                         "CAST(value AS REAL) IS NOT NULL"]
                params: List[Any] = [tid, _to_sqlite_text(frm), _to_sqlite_text(to), tag]
                if gateway_id:
                    where.append("gateway_id = ?")
                    params.append(gateway_id)
                wsql = " AND ".join(where)
                try:
                    row = con.execute(
                        f"SELECT COUNT(*), SUM(CAST(value AS REAL)), "
                        f"SUM(CAST(value AS REAL)*CAST(value AS REAL)), "
                        f"MIN(CAST(value AS REAL)), MAX(CAST(value AS REAL)) "
                        f"FROM historian_readings WHERE {wsql}",
                        params,
                    ).fetchone()
                except _sqlite3.OperationalError:
                    continue
                if not row or not row[0]:
                    continue
                cnt, s, ssq, mn, mx = row
                agg["count"] += int(cnt or 0)
                agg["sum"] += float(s or 0.0)
                agg["sumsq"] += float(ssq or 0.0)
                agg["min"] = mn if agg["min"] is None else min(agg["min"], mn)
                agg["max"] = mx if agg["max"] is None else max(agg["max"], mx)
                # first/last value for the window (ordered by ts).
                fr = con.execute(
                    f"SELECT CAST(value AS REAL), ts_utc FROM historian_readings "
                    f"WHERE {wsql} ORDER BY ts_utc ASC LIMIT 1", params).fetchone()
                lr = con.execute(
                    f"SELECT CAST(value AS REAL), ts_utc FROM historian_readings "
                    f"WHERE {wsql} ORDER BY ts_utc DESC LIMIT 1", params).fetchone()
                if fr and (agg["first_ts"] is None or fr[1] < agg["first_ts"]):
                    agg["first"], agg["first_ts"] = fr[0], fr[1]
                if lr and (agg["last_ts"] is None or lr[1] > agg["last_ts"]):
                    agg["last"], agg["last_ts"] = lr[0], lr[1]
        finally:
            try: con.close()
            except Exception: pass
    n = agg["count"]
    if not n:
        return {"count": 0, "min": None, "max": None, "avg": None,
                "stddev": None, "first": None, "last": None}
    avg = agg["sum"] / n
    if n > 1:
        # Sample variance from sum + sum-of-squares (single-pass).
        var = max(0.0, (agg["sumsq"] - n * avg * avg) / (n - 1))
        sd = math.sqrt(var)
    else:
        sd = 0.0
    return {"count": n, "min": agg["min"], "max": agg["max"], "avg": avg,
            "stddev": sd, "first": agg["first"], "last": agg["last"]}


def _stats(values: List[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"count": 0, "min": None, "max": None, "avg": None, "stddev": None, "first": None, "last": None}
    n = len(values)
    mn = min(values); mx = max(values)
    avg = sum(values) / n
    if n > 1:
        var = sum((v - avg) ** 2 for v in values) / (n - 1)
        sd = math.sqrt(var)
    else:
        sd = 0.0
    return {"count": n, "min": mn, "max": mx, "avg": avg, "stddev": sd,
            "first": values[0], "last": values[-1]}


def _auto_resolve(tag: str) -> Dict[str, Any]:
    """Resolve a (possibly fuzzy) tag name INSIDE the data tool so the LLM
    doesn't need a separate find_tags round-trip (saves ~2s per query).

    Returns one of:
      {"tag": "<exact>"}                         — resolved, proceed
      {"needs_choice": ["opt1","opt2", ...]}     — ambiguous, ask the user
      {"not_found": True}                        — no close match
    """
    from ._scope import resolve_tag
    r = resolve_tag(tag, limit=5)
    if r.get("exact"):
        return {"tag": r["exact"]}
    sugg = r.get("suggestions") or []
    if len(sugg) == 1:
        # Single close match → use it directly, no round-trip.
        return {"tag": sugg[0]}
    if len(sugg) >= 2:
        return {"needs_choice": sugg}
    return {"not_found": True}


def run_get_tag_summary(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    tag = str(args.get("tag") or "").strip()
    if not tag:
        return {"error": "Missing 'tag'."}
    # Auto-resolve fuzzy tag in-tool (no find_tags round-trip).
    _res = _auto_resolve(tag)
    if _res.get("needs_choice"):
        return {"disambiguation_needed": True, "query": tag, "suggestions": _res["needs_choice"],
                "instruction": "Ask the user to pick one of these tags, then call again."}
    if _res.get("not_found"):
        return {"error": f"No tag matching '{tag}' is configured.", "query": tag}
    tag = _res["tag"]
    frm = _parse_time(args.get("from_") or args.get("from") or "")
    to = _parse_time(args.get("to") or "now")
    gateway_id = str(args.get("gateway_id") or "").strip()
    # Operator 2026-07-02 (PERF): aggregate in SQL (index-accelerated) instead
    # of pulling every raw row into Python. ~6x faster and O(1) memory.
    out = _fetch_stats_sql(tag, frm, to, gateway_id)
    out["tag"] = tag
    out["from"] = _to_sqlite_text(frm) + "Z"
    out["to"] = _to_sqlite_text(to) + "Z"
    out["data_source"] = context.get("data_source", "local")
    # Human gateway name so the LLM can show "PLC" instead of "gw-1781903...".
    try:
        from ._scope import gateway_name_for, gateway_name_for_tag
        if gateway_id:
            out["gateway_name"] = gateway_name_for(gateway_id)
        else:
            # Tag wasn't pinned to a gateway — look it up by tag name.
            out["gateway_name"] = gateway_name_for_tag(tag)
        out["gateway_id"] = gateway_id
    except Exception:
        pass
    return out


def run_get_tag_timeseries(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Return a downsampled time series for a tag over a window. The
    LLM uses this to render charts in the chat — output goes into a
    ```trustnode-chart fenced block that the frontend parses.

    Always downsamples to ~max_points buckets via uniform bucketing
    (keep the LAST value in each bucket — that's what the operator
    expects to see). Keeps payload small so token usage is bounded.
    """
    tag = str(args.get("tag") or "").strip()
    if not tag:
        return {"error": "Missing 'tag'."}
    _res = _auto_resolve(tag)
    if _res.get("needs_choice"):
        return {"disambiguation_needed": True, "query": tag, "suggestions": _res["needs_choice"],
                "instruction": "Ask the user to pick one of these tags, then call again."}
    if _res.get("not_found"):
        return {"error": f"No tag matching '{tag}' is configured.", "query": tag}
    tag = _res["tag"]
    frm = _parse_time(args.get("from_") or args.get("from") or "-1h")
    to = _parse_time(args.get("to") or "now")
    gateway_id = str(args.get("gateway_id") or "").strip()
    try:
        max_points = max(20, min(500, int(args.get("max_points") or 200)))
    except Exception:
        max_points = 200
    ds = context.get("data_source", "local")
    rows = _fetch_rows(tag, frm, to, gateway_id, ds)
    # Convert to (ts_ms, value) pairs.
    samples: List[Tuple[int, float]] = []
    for r in rows:
        try:
            v = float(r.get("value"))
            if math.isnan(v):
                continue
            ts_str = str(r.get("ts_utc") or "").strip()
            if not ts_str:
                continue
            # _fetch_rows stores ts as 'YYYY-MM-DD HH:MM:SS' UTC.
            try:
                dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except Exception:
                # ISO fallback
                if ts_str.endswith("Z"):
                    ts_str = ts_str[:-1] + "+00:00"
                dt = datetime.fromisoformat(ts_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            samples.append((int(dt.timestamp() * 1000), v))
        except Exception:
            continue
    samples.sort(key=lambda x: x[0])
    # Downsample by uniform time bucketing (keep last in each bucket).
    series: List[Dict[str, Any]] = []
    if samples:
        first_ms = samples[0][0]
        last_ms = samples[-1][0]
        span_ms = max(1, last_ms - first_ms)
        bucket_ms = max(1, span_ms // max_points)
        current_bucket = None
        current_last_ts = None
        current_last_v = None
        for ts_ms, v in samples:
            b = (ts_ms - first_ms) // bucket_ms
            if current_bucket is None:
                current_bucket = b
            if b != current_bucket:
                series.append({"ts": current_last_ts, "value": current_last_v})
                current_bucket = b
            current_last_ts = ts_ms
            current_last_v = v
        if current_last_ts is not None:
            series.append({"ts": current_last_ts, "value": current_last_v})
    stats = _stats([s["value"] for s in series])
    # Human gateway name so the chart subtitle + chat reply can show it.
    gateway_name = ""
    try:
        from ._scope import gateway_name_for, gateway_name_for_tag
        gateway_name = gateway_name_for(gateway_id) if gateway_id else gateway_name_for_tag(tag)
    except Exception:
        gateway_name = ""
    return {
        "tag": tag,
        "gateway_id": gateway_id,
        "gateway_name": gateway_name,
        "from": _to_sqlite_text(frm) + "Z",
        "to": _to_sqlite_text(to) + "Z",
        "data_source": ds,
        "count": stats.get("count"),
        "min": stats.get("min"),
        "max": stats.get("max"),
        "avg": stats.get("avg"),
        "series": series,
    }


def run_get_multi_tag_timeseries(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch downsampled series for MULTIPLE tags in one call. Returns
    the multi-series shape the chart renderer accepts natively:
      {from, to, series: [{tag, gateway_name, series:[{ts,value},...]}, ...]}

    This stops the LLM from looping get_tag_timeseries N times when the
    user asks to compare or overlay tags. The renderer will auto-place
    series on a right Y axis when their value ranges differ by >5x.
    """
    tags_arg = args.get("tags") or []
    if isinstance(tags_arg, str):
        # Tolerate comma-separated or single tag.
        tags_list = [t.strip() for t in tags_arg.split(",") if t.strip()]
    else:
        tags_list = [str(t).strip() for t in tags_arg if str(t).strip()]
    if not tags_list:
        return {"error": "Missing 'tags' (list of tag names)."}
    frm = _parse_time(args.get("from_") or args.get("from") or "-1h")
    to = _parse_time(args.get("to") or "now")
    gateway_id = str(args.get("gateway_id") or "").strip()
    try:
        max_points = max(20, min(500, int(args.get("max_points") or 200)))
    except Exception:
        max_points = 200
    ds = context.get("data_source", "local")

    out_series: List[Dict[str, Any]] = []
    for tag in tags_list[:6]:  # cap at 6 series to keep payload sane
        per_tag = run_get_tag_timeseries(
            {"tag": tag, "from_": _to_sqlite_text(frm) + "Z",
             "to": _to_sqlite_text(to) + "Z", "max_points": max_points,
             "gateway_id": gateway_id},
            context,
        )
        if not isinstance(per_tag, dict) or per_tag.get("error"):
            out_series.append({"tag": tag, "gateway_name": "", "series": [], "error": per_tag.get("error") if isinstance(per_tag, dict) else "fetch failed"})
            continue
        out_series.append({
            "tag": tag,
            "gateway_name": per_tag.get("gateway_name") or "",
            "series": per_tag.get("series") or [],
            "min": per_tag.get("min"),
            "max": per_tag.get("max"),
            "avg": per_tag.get("avg"),
        })
    return {
        "from": _to_sqlite_text(frm) + "Z",
        "to": _to_sqlite_text(to) + "Z",
        "data_source": ds,
        "series": out_series,
    }


def run_compare_periods(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    tag = str(args.get("tag") or "").strip()
    if not tag:
        return {"error": "Missing 'tag'."}
    gateway_id = str(args.get("gateway_id") or "").strip()
    a_from = _parse_time(args.get("period_a_from"))
    a_to = _parse_time(args.get("period_a_to"))
    b_from = _parse_time(args.get("period_b_from"))
    b_to = _parse_time(args.get("period_b_to"))
    ds = context.get("data_source", "local")

    def _stats_for(f: datetime, t: datetime):
        # Operator 2026-07-02 (PERF): SQL-side aggregation, index-accelerated.
        return _fetch_stats_sql(tag, f, t, gateway_id)

    a = _stats_for(a_from, a_to)
    b = _stats_for(b_from, b_to)
    delta = {}
    for k in ("min", "max", "avg", "stddev"):
        if a.get(k) is not None and b.get(k) is not None:
            delta[k] = b[k] - a[k]
        else:
            delta[k] = None
    return {
        "tag": tag, "data_source": ds,
        "period_a": {"from": _to_sqlite_text(a_from) + "Z",
                     "to":   _to_sqlite_text(a_to) + "Z", **a},
        "period_b": {"from": _to_sqlite_text(b_from) + "Z",
                     "to":   _to_sqlite_text(b_to) + "Z", **b},
        "delta_b_minus_a": delta,
    }
