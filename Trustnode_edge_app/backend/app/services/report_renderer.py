"""PDF report renderer (reportlab).

A "template" is an ordered list of sections. Each section is one of:

    {type: "header",   title: str, subtitle?: str, show_generated_at?: bool}
    {type: "text",     html?: str, text: str}
    {type: "kpi_grid", title?: str, items: [{label, gateway_id, tag_name, aggregation, value1?, value2?}]}
    {type: "line_chart" | "area_chart" | "bar_chart",
        title?: str, gateway_id, tag_name, time_range, group_interval?, readings_count?}
    {type: "pie_chart", title?: str,
        data_source_type: "tag_direct"|"computed",
        gateway_id, tag_name, query_result_aggregation,
        compute_rules?: [{label, gateway_id, tag_name, operator, value1, value2, aggregation, color}]}
    {type: "table",    title?: str, gateway_id, tag_name, time_range, row_limit}
    {type: "spacer",   height?: int}

`time_range` is either {preset: "5m"|"15m"|"1h"|"6h"|"24h"|"7d"|"30d"} or
{preset: "custom", from_utc, to_utc}, or {preset: "none"} for "all rows".

The renderer queries the same historian endpoints the dashboard uses, so chart
values in PDFs match what the user sees on the live dashboard.
"""
from __future__ import annotations

import hashlib
import io
import math
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _utc_str_to_local_iso(ts_utc: str) -> str:
    """Convert a historian ts_utc string to the operator's local ISO string.

    Mirror of the helper in plc_manager.py so reports + exports show the
    timestamp the operator actually experienced, not raw UTC.
    """
    raw = str(ts_utc or "").strip()
    if not raw:
        return ""
    cand = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    parsed: datetime | None = None
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            parsed = datetime.strptime(cand.split("+", 1)[0], fmt)
            break
        except Exception:
            continue
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(cand)
        except Exception:
            return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    except Exception:
        return raw
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.graphics.shapes import (
    Circle, Drawing, Group, Line, Rect, String)
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.charts.legends import Legend

from app.services.reports_store import _resolve_reports_dir


def _app_store():
    # Late import to avoid circular dependency at module load time
    # (state.py composes the renderer, which would otherwise import state).
    from app.state import app_store as _store
    return _store


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #
TIME_PRESET_SECONDS = {
    "5m": 5 * 60,
    "15m": 15 * 60,
    "1h": 60 * 60,
    "6h": 6 * 60 * 60,
    "24h": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
}


def _batch_service():
    """Resolve BatchService lazily (module may be unlicensed; that's fine —
    a batch time-range on an unlicensed install just yields an empty window)."""
    try:
        from app.modules.batch_management.service import BatchService
        return BatchService(_app_store())
    except Exception:
        return None


def _resolve_batch_v2(batch_id: str):
    """v2 batches (the current module) live in a different table shape than v1,
    so a v2 batch id must be resolved via BatchExecutionService, not the legacy
    BatchService. Try v2 FIRST for an explicit id — otherwise batch-anchored
    report charts get an empty window and render blank."""
    if not batch_id:
        return None
    try:
        from app.modules.batch_management.service_v2 import BatchExecutionService
        b = BatchExecutionService(_app_store()).get_batch(batch_id)
        return b or None
    except Exception:
        return None


def _resolve_batch(batch_id: str = "", batch_of_type_id: str = "") -> dict[str, Any] | None:
    """Return a batch dict for an explicit id, or the latest batch of a type.
    v2 first (current module), then legacy v1 as a fallback."""
    if batch_id:
        v2 = _resolve_batch_v2(batch_id)
        if v2:
            return v2
    svc = _batch_service()
    if not svc:
        return None
    try:
        if batch_id:
            return svc.get_batch(batch_id)
        if batch_of_type_id:
            return svc.latest_batch_for_type(batch_of_type_id)
    except Exception:
        return None
    return None


def _resolve_batch_window(time_range: dict[str, Any]) -> tuple[str, str]:
    """Resolve a {preset:'batch', batch_id | batch_of_type_id} time range to the
    batch's (started_utc, ended_utc). A still-running batch uses started→now."""
    b = _resolve_batch(str(time_range.get("batch_id") or ""),
                        str(time_range.get("batch_of_type_id") or ""))
    if not b:
        return "", ""
    start = str(b.get("started_utc") or "").strip()
    end = str(b.get("ended_utc") or "").strip()
    if start and not end:
        end = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # Trim the trailing millis so it compares/filters cleanly (historian ts_utc
    # is 'YYYY-MM-DD HH:MM:SS.fff'; the range filter uses >= start, <= end, so
    # keeping millis is fine — but normalize whitespace).
    return start, end


def _resolve_time_range(time_range: Any) -> tuple[str, str]:
    """Return (from_utc, to_utc) text suitable for historian filters."""
    if not isinstance(time_range, dict):
        return "", ""
    preset = str(time_range.get("preset") or "none").strip().lower()
    if preset in {"none", ""}:
        return "", ""
    # Operator 2026-07-06: a BATCH-anchored range — the section pulls exactly the
    # window of a chosen batch (or the latest batch of a type). This is how a
    # report shows "the tags collected during batch X".
    if preset == "batch":
        return _resolve_batch_window(time_range)
    if preset == "custom":
        return (
            str(time_range.get("from_utc") or "").strip(),
            str(time_range.get("to_utc") or "").strip(),
        )
    if preset == "run":
        # "The run's window" - but only a TRIGGERED run has one, and
        # apply_window_override replaces this preset before the fetchers ever
        # see it. Previewing the same template by hand reached here, where an
        # unknown preset means ("", "") - no filter at all. Every section then
        # scanned the WHOLE historian, which on a multi-gigabyte store hangs
        # the render and starves the requests behind it. Fall back to a day.
        seconds = TIME_PRESET_SECONDS["24h"]
    else:
        seconds = TIME_PRESET_SECONDS.get(preset, 0)
    if seconds <= 0:
        return "", ""
    now = datetime.now(timezone.utc)
    start = now - timedelta(seconds=seconds)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.strftime(fmt), now.strftime(fmt)


# --------------------------------------------------------------------------- #
# data fetching (reuses app_store methods so values match the dashboard)
# --------------------------------------------------------------------------- #
def _fetch_series(section: dict[str, Any], default_limit: int = 240) -> list[dict[str, Any]]:
    from_utc, to_utc = _resolve_time_range(section.get("time_range"))
    gateway = str(section.get("gateway_id") or "").strip()
    tag = str(section.get("tag_name") or "").strip()
    limit = max(20, min(int(section.get("readings_count") or default_limit), 5000))
    rows = _app_store().get_historian_rows_range(
        from_utc=from_utc,
        to_utc=to_utc,
        limit=limit,
        offset=0,
        gateway=gateway,
        tag=tag,
        prefer_cloud_reads=False,
    )
    # Most-recent-first from the API; charts want oldest-first.
    return list(reversed(rows or []))


def _normalize_series_list(section: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the section's series list (multi-tag charts). For backward compat
    the single (gateway_id, tag_name, color, axis) attributes on the section
    are treated as series[0] when no series array is provided.

    Each series may carry these optional analytics fields:
      aggregation: one of avg/min/max/last/sum/count/median (None = raw points)
      operator:    one of any/eq/ne/lt/lte/gt/gte/between (value filter)
      value1, value2: numeric thresholds used by the operator above

    Aggregation is interpreted against the section-level `bucket` (e.g. "1h").
    With bucket="raw" (default), aggregation collapses the WHOLE series to a
    single point; with a bucket set, samples are grouped per bucket and the
    aggregation reduces each bucket to one value.
    """
    series_list = section.get("series")
    out_list: list[dict[str, Any]] = []
    if isinstance(series_list, list) and series_list:
        for s in series_list:
            if not isinstance(s, dict):
                continue
            out_list.append(_normalize_one_series(s, section))
    if out_list:
        return out_list
    # Single-series fallback so legacy templates (no series array) still render.
    return [
        _normalize_one_series(
            {
                "id": "s0",
                "label": section.get("series_label") or section.get("tag_name") or "",
                "gateway_id": section.get("gateway_id") or "",
                "tag_name": section.get("tag_name") or "",
                "unit": section.get("unit") or "",
            },
            section,
        )
    ]


def _normalize_one_series(s: dict[str, Any], section: dict[str, Any]) -> dict[str, Any]:
    operator = str(s.get("operator") or "any").strip().lower()
    if operator not in {"any", "eq", "ne", "lt", "lte", "gt", "gte", "between"}:
        operator = "any"
    aggregation = str(s.get("aggregation") or "").strip().lower()
    if aggregation not in {"", "avg", "min", "max", "last", "sum", "count", "median"}:
        aggregation = ""

    def _to_num(v: Any) -> float | None:
        if v is None or v == "":
            return None
        try:
            return float(v)
        except Exception:
            return None

    def _safe_float(value: Any, default: float) -> float:
        """Normalize a series multiplier/offset to a float. The frontend can
        save these as empty strings if the operator clears the input, which
        used to crash float() and abort the whole _section_chart render —
        the PDF then either dropped the section entirely or printed the
        catch-all "[error rendering section: ...]" message even though the
        preview rendered fine."""
        if value is None or value == "":
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    return {
        "id": str(s.get("id") or uuid.uuid4().hex[:8]),
        "label": str(s.get("label") or s.get("tag_name") or ""),
        "gateway_id": str(s.get("gateway_id") or section.get("gateway_id") or ""),
        "tag_name": str(s.get("tag_name") or ""),
        "color": (str(s.get("color") or "").strip() or None),
        # 4-way, not left/right: forcing anything unknown to "left" is what
        # made a third unit share the first axis's scale.
        "axis": norm_axis4(s.get("axis")),
        "chart_type": str(s.get("chart_type") or "").lower() or None,
        "unit": str(s.get("unit") or section.get("unit") or ""),
        "multiplier": _safe_float(s.get("multiplier"), 1.0),
        "offset": _safe_float(s.get("offset"), 0.0),
        # This dict is REBUILT key by key, so anything missing here is saved
        # by the editor and then silently dropped before it is drawn. None
        # means "follow the section's setting".
        "line_width": _to_num(s.get("line_width")),
        # Rebuilt key by key - a field missing here is saved by the editor
        # and silently dropped before it is drawn. "" means "follow the
        # section's setting".
        "interpolation": norm_interpolation(s.get("interpolation"), ""),
        "aggregation": aggregation,
        "operator": operator,
        "value1": _to_num(s.get("value1")),
        "value2": _to_num(s.get("value2")),
    }


# Bucket strings → seconds. "raw" / "" means "no bucketing".
_BUCKET_SECONDS: dict[str, int] = {
    "raw": 0,
    "1s": 1,
    "10s": 10,
    "30s": 30,
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 3600,
    "4h": 4 * 3600,
    "12h": 12 * 3600,
    "1d": 86400,
}


def _bucket_seconds(section: dict[str, Any]) -> int:
    raw = str(section.get("bucket") or "raw").strip().lower()
    return int(_BUCKET_SECONDS.get(raw, 0))


def _passes_value_filter(value: float | None, op: str, v1: float | None, v2: float | None) -> bool:
    """Apply the legacy reporting value-predicate to a single numeric sample.

    Operator semantics match get_historian_rule_stats:
      any         always true
      eq/ne       value == / != v1
      lt/lte      value < / <= v1
      gt/gte      value > / >= v1
      between     min(v1,v2) <= value <= max(v1,v2)

    A None value never passes any filter except "any".
    """
    if op == "any":
        return True
    if value is None or not math.isfinite(value):
        return False
    if op in {"eq", "ne", "lt", "lte", "gt", "gte"} and v1 is None:
        return False
    if op == "between" and (v1 is None or v2 is None):
        return False
    if op == "eq":
        return value == v1
    if op == "ne":
        return value != v1
    if op == "lt":
        return value < v1
    if op == "lte":
        return value <= v1
    if op == "gt":
        return value > v1
    if op == "gte":
        return value >= v1
    if op == "between":
        lo, hi = (v1, v2) if (v1 <= v2) else (v2, v1)  # type: ignore[operator]
        return lo <= value <= hi
    return True


def _reduce_bucket(values: list[float], how: str) -> float | None:
    """Reduce a list of finite floats to a single value per the named op."""
    if not values:
        return None
    how = (how or "").lower()
    if how == "avg":
        return sum(values) / len(values)
    if how == "min":
        return min(values)
    if how == "max":
        return max(values)
    if how == "sum":
        return sum(values)
    if how == "count":
        return float(len(values))
    if how == "median":
        s = sorted(values)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 == 1 else (s[mid - 1] + s[mid]) / 2.0
    # "last" or unknown -> last sample by input order
    return values[-1]


def _utc_to_epoch_seconds(ts_raw: str) -> int:
    """Parse a historian ts string to integer epoch seconds.

    Accepts ISO with optional timezone or naive UTC strings. Falls back to 0
    when the input can't be parsed (caller treats 0 as 'unknown bucket').
    """
    if not ts_raw:
        return 0
    try:
        from datetime import datetime, timezone
        txt = ts_raw.replace("Z", "+00:00")
        if " " in txt and "T" not in txt:
            txt = txt.replace(" ", "T")
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return 0


def _apply_series_analytics(
    points: list[tuple[str, float | None, str | None]],
    series: dict[str, Any],
    bucket_seconds: int,
) -> list[tuple[str, float | None, str | None]]:
    """Apply the per-series value filter then the bucket+aggregation reduction.

    points: oldest-first 3-tuples (ts_utc, value, value_text).
    Returns a list with the same 3-tuple shape so downstream chart / table
    code can keep iterating the way it does today.

    If the series carries no operator/aggregation tweaks, the input is
    returned unchanged.
    """
    op = str(series.get("operator") or "any")
    v1 = series.get("value1")
    v2 = series.get("value2")
    agg = str(series.get("aggregation") or "")
    if op == "any" and bucket_seconds <= 0 and not agg:
        return points

    # 1) value filter — drops rows that don't satisfy the predicate.
    filtered: list[tuple[str, float | None, str | None]] = []
    for p in points:
        v = p[1]
        if _passes_value_filter(v, op, v1, v2):
            filtered.append(p)

    if not filtered:
        return []

    # 2) bucket + aggregation. With bucket=0 and a named aggregation, collapse
    # the whole series to a single sample (timestamp = last sample's ts).
    if not agg and bucket_seconds <= 0:
        return filtered

    if bucket_seconds <= 0:
        nums = [float(p[1]) for p in filtered if p[1] is not None and math.isfinite(p[1])]
        reduced = _reduce_bucket(nums, agg)
        last_ts = filtered[-1][0]
        return [(last_ts, reduced, None)]

    # Group samples into fixed-width epoch buckets, then reduce.
    buckets: dict[int, list[float]] = {}
    bucket_first_ts: dict[int, str] = {}
    for p in filtered:
        v = p[1]
        if v is None or not math.isfinite(v):
            continue
        epoch = _utc_to_epoch_seconds(p[0])
        if epoch <= 0:
            continue
        bk = (epoch // bucket_seconds) * bucket_seconds
        buckets.setdefault(bk, []).append(float(v))
        bucket_first_ts.setdefault(bk, p[0])

    if not buckets:
        return []

    out: list[tuple[str, float | None, str | None]] = []
    for bk in sorted(buckets.keys()):
        values = buckets[bk]
        reduced = _reduce_bucket(values, agg or "avg")
        # Use the bucket start as the canonical ts so the chart's X axis lines
        # up cleanly. Format mirrors historian output: "YYYY-MM-DDTHH:MM:SSZ".
        from datetime import datetime, timezone
        # Same shape as every other timestamp in a report. The ISO "T...Z"
        # form made a bucketed chart's axis read "2026-10-01T15:10:00" while
        # the raw chart beside it read "2026-10-01 15:10:00".
        bk_ts = datetime.fromtimestamp(bk, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        out.append((bk_ts, reduced, None))
    return out


def _span_seconds(from_utc: str, to_utc: str) -> float:
    """Length of a resolved report window, or 0 when it cannot be parsed."""
    fmts = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S")
    def _p(txt: str):
        clean = str(txt or "").replace("Z", "").split("+")[0].strip()
        for f in fmts:
            try:
                return datetime.strptime(clean, f)
            except Exception:
                continue
        return None
    a, b = _p(from_utc), _p(to_utc)
    if not a or not b:
        return 0.0
    return max(0.0, (b - a).total_seconds())


def _bucketed_or_raw_rows(from_utc: str, to_utc: str, gateway: str, tag: str,
                          limit: int) -> list[dict[str, Any]]:
    """Rows for a chart series, bucketed when the window is wide.

    get_historian_rows_range caps at 5 000 rows. At a 1 s cadence that is under
    two hours, so a report section asking for 24 h got the newest ~1.4 h and
    drew it across a 24 h axis — wrong, and wrong in a way that looks fine.

    Past two hours we read the aggregate path instead (which buckets raw data
    live when the rollup tables have not covered the window). Anything shorter
    keeps the raw path exactly as it was.
    """
    span = _span_seconds(from_utc, to_utc)
    if span > 2 * 3600:
        bucket = "day" if span > 45 * 86400 else ("hour" if span > 3 * 86400 else "minute")
        bucket_s = 86400 if bucket == "day" else (3600 if bucket == "hour" else 60)
        # Size the read from the WINDOW, not from `limit` — that is a raw-row
        # budget for the raw path, and using it here capped a 24 h minute-bucket
        # section at 960 of the 1 440 buckets it needed, i.e. 16 h of a 24 h
        # axis. One bucket per interval, plus headroom for partial edges.
        need = int(span / bucket_s) + 2
        try:
            rows = _app_store().get_historian_agg_rows(
                bucket=bucket, from_utc=from_utc, to_utc=to_utc,
                gateway=gateway, tag=tag, limit=max(500, min(need * 2, 20000)),
            )
        except Exception:
            rows = []
        if rows:
            return rows
    raw = _app_store().get_historian_rows_range(
        from_utc=from_utc,
        to_utc=to_utc,
        limit=limit,
        offset=0,
        gateway=gateway,
        tag=tag,
        prefer_cloud_reads=False,
    )
    # 2026-10-11, asked: do reports show the gateway's period, or whatever is
    # in the historian?
    #
    # The raw read is `ORDER BY ts_utc DESC LIMIT n`, so when a window holds
    # more readings than the budget it returns the NEWEST n - and the chart
    # drew them across the whole axis. At a 1 s cadence a 1 h section showed
    # the last four minutes labelled as an hour. That is the same defect the
    # docstring above describes for windows over two hours, which only the
    # wide case was fixed for.
    #
    # Truncated means the window is denser than the budget, so bucket it: one
    # point per minute covers the whole span at a density a PDF can draw. The
    # report never shows finer than the gateway actually collected, because
    # bucketing can only aggregate rows that exist.
    if len(raw) >= limit and span > 120:
        try:
            bucketed = _app_store().get_historian_agg_rows(
                bucket="minute", from_utc=from_utc, to_utc=to_utc,
                gateway=gateway, tag=tag,
                limit=max(500, min(int(span / 60) + 2, 20000)))
        except Exception:
            bucketed = []
        if bucketed:
            return bucketed
    return raw


def _fetch_multi_series(section: dict[str, Any], default_limit: int = 240) -> tuple[list[dict[str, Any]], list[list[tuple[str, float | None, str | None]]]]:
    """Fetch each series independently then align by index.

    Returns:
      series_meta: list of normalized series dicts (length N)
      aligned: list of N lists; each inner list is [(ts, value, value_text), ...] oldest-first.
        For numeric tags value_text is None; for string tags value is None and
        value_text carries the original text. Charts ignore value_text; data
        tables prefer value_text when set so operators see the actual string.
    """
    from_utc, to_utc = _resolve_time_range(section.get("time_range"))
    limit = max(20, min(int(section.get("readings_count") or default_limit), 5000))
    series_meta = _normalize_series_list(section)
    bucket_secs = _bucket_seconds(section)
    aligned: list[list[tuple[str, float | None, str | None]]] = []
    for s in series_meta:
        gw = (s.get("gateway_id") or "").strip()
        tag = (s.get("tag_name") or "").strip()
        if not tag:
            aligned.append([])
            continue
        # A raw range read is capped (5 000 rows), which at 1 Hz is under two
        # hours — so a "24h trend" section silently rendered the first slice of
        # the window and called it a day. Past a couple of hours, read
        # PRE-BUCKETED rows instead: the whole window, at a density a PDF chart
        # can actually draw. Short ranges keep the raw path unchanged.
        rows = _bucketed_or_raw_rows(from_utc, to_utc, gw, tag, limit)
        rows = list(reversed(rows or []))
        mult = float(s.get("multiplier") or 1.0)
        off = float(s.get("offset") or 0.0)
        points: list[tuple[str, float | None, str | None]] = []
        for r in rows:
            try:
                raw = r.get("value")
                val = (float(raw) * mult + off) if raw is not None else None
            except Exception:
                val = None
            text_raw = r.get("value_text")
            text_val = str(text_raw) if (text_raw is not None and text_raw != "") else None
            points.append((str(r.get("ts") or ""), val, text_val))
        # Apply per-series value filter + section bucket + aggregation.
        # When the series has no analytics tweaks this is a no-op.
        points = _apply_series_analytics(points, s, bucket_secs)
        aligned.append(points)
    return series_meta, aligned


def _rate_per_minute(rows: list[dict[str, Any]], want: str) -> tuple[float | None, int]:
    """Peak rate of change, in units per MINUTE.

    A TCU test is graded on how fast it heats and cools ("peak heating rate
    +10.35 C/min", "peak cooling rate -10.86"), which no plain aggregation can
    express: it is a slope, not a value. `rate_max` is the steepest rise,
    `rate_min` the steepest fall (a negative number).

    Samples are compared consecutively. Pairs less than a second apart are
    skipped - dividing by a near-zero interval manufactures enormous slopes
    out of ordinary jitter.
    """
    best: float | None = None
    used = 0
    prev_t = prev_v = None
    for r in rows or []:
        # get_historian_rows_range returns "ts"; other paths carry "ts_utc".
        # Reading only ts_utc skipped every row and the KPI rendered "-".
        ts = str(r.get("ts") or r.get("ts_utc") or "")
        try:
            v = float(r.get("value"))
        except (TypeError, ValueError):
            continue
        try:
            t = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if prev_t is not None:
            secs = (t - prev_t).total_seconds()
            if secs >= 1.0:
                slope = (v - prev_v) * 60.0 / secs
                used += 1
                if best is None:
                    best = slope
                elif want == "rate_min":
                    best = min(best, slope)
                else:
                    best = max(best, slope)
        prev_t, prev_v = t, v
    return best, used


def _ramp_seconds(rows: list[dict[str, Any]], low: Any, high: Any) -> float | None:
    """Seconds from first crossing `low` to first crossing `high` after it.

    2026-10-01: "ramp time" on a TCU test. A rate (C/min) answers "how fast at
    its steepest"; this answers "how long did it take to get there", which is
    what a test sheet records. Returns None when either threshold is never
    reached, so an incomplete ramp reads as "-" rather than 0.
    """
    try:
        lo = float(low)
        hi = float(high)
    except (TypeError, ValueError):
        return None
    t_lo = None
    for r in rows or []:
        try:
            v = float(r.get("value"))
        except (TypeError, ValueError):
            continue
        ts = _row_ts(r)
        try:
            at = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if t_lo is None:
            if (v >= lo) if hi >= lo else (v <= lo):
                t_lo = at
            continue
        if (v >= hi) if hi >= lo else (v <= hi):
            return max(0.0, (at - t_lo).total_seconds())
    return None


def _window_seconds(from_utc: str, to_utc: str) -> float | None:
    try:
        a = datetime.strptime(str(from_utc)[:19], "%Y-%m-%d %H:%M:%S")
        b = datetime.strptime(str(to_utc)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return max(0.0, (b - a).total_seconds())


def parse_value_map(raw: Any) -> dict[str, str]:
    """An operator's value mask, as a lookup.

    Accepts a dict straight from the API, or the shorthand the editor
    offers: "1=PASS, 0=FAIL". 2026-10-09, requested: "when linked to a tag
    we should have [an] option to create a condition to a mask, like if the
    value is 1 we should show in the report passed and 0 failed".

    Keys are compared as trimmed strings, and a float that is whole is also
    matched by its integer form - a tag read as 1.0 must match a mask
    written as "1", which is how anyone would write it.
    """
    if isinstance(raw, dict):
        return {str(k).strip(): str(v) for k, v in raw.items()}
    out: dict[str, str] = {}
    for part in str(raw or "").split(","):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


def apply_value_map(mapping: dict[str, str], value, text_val):
    """The masked label for a reading, or None when nothing matches."""
    if not mapping:
        return None
    candidates = []
    if text_val not in (None, ""):
        candidates.append(str(text_val).strip())
    if value is not None:
        try:
            f = float(value)
        except (TypeError, ValueError):
            f = None
        if f is not None:
            if f.is_integer():
                candidates.append(str(int(f)))
            candidates.append(("%g" % f))
            candidates.append(str(f))
    for c in candidates:
        if c in mapping:
            return mapping[c]
    # "*" is the catch-all, so a mask can say "anything else = CHECK".
    return mapping.get("*")


def _fetch_kpi_value(item: dict[str, Any], from_utc: str = "",
                     to_utc: str = "") -> tuple[float | None, int, str | None]:
    """Return (value, sample_count, text_value) for a single KPI cell.

    For numeric tags `text_value` is None and `value` holds the aggregate.
    For string-typed tags `value` is None and `text_value` carries the most
    recent text reading; the KPI cell renders the text directly.

    `from_utc`/`to_utc` scope the aggregate. They were NOT passed before, so
    every KPI in every report aggregated the WHOLE historian regardless of
    the section's time range — including cycle reports, which are supposed to
    cover only their cycle. Empty strings keep the old all-time behaviour for
    a section that genuinely asks for "all available".
    """
    rule = {
        "id": item.get("id") or uuid.uuid4().hex,
        "label": item.get("label") or "",
        "gateway_id": item.get("gateway_id") or "",
        "tag_name": item.get("tag_name") or "",
        "operator": item.get("operator") or "any",
        "value1": item.get("value1"),
        "value2": item.get("value2"),
        "aggregation": item.get("aggregation") or "latest",
        "color": item.get("color") or "#14a89a",
    }
    agg = str(rule["aggregation"]).strip().lower()
    if agg == "duration_s":
        # How long the window lasted - the test's own duration, per cycle.
        return (_window_seconds(from_utc, to_utc), 1, None)
    if agg == "ramp_time":
        raw = _app_store().get_historian_rows_range(
            from_utc=from_utc, to_utc=to_utc, limit=5000, offset=0,
            gateway=rule["gateway_id"], tag=rule["tag_name"],
            prefer_cloud_reads=False)
        secs = _ramp_seconds(list(reversed(raw or [])),
                             item.get("ramp_from"), item.get("ramp_to"))
        return (secs, 0 if secs is None else 1, None)
    if agg in {"rate_max", "rate_min"}:
        raw = _app_store().get_historian_rows_range(
            from_utc=from_utc, to_utc=to_utc, limit=5000, offset=0,
            gateway=rule["gateway_id"], tag=rule["tag_name"],
            prefer_cloud_reads=False)
        value, used = _rate_per_minute(list(reversed(raw or [])), agg)
        return (value, used, None)
    rows = _app_store().get_historian_rule_stats(
        rules=[rule], gateway=rule["gateway_id"],
        from_utc=from_utc, to_utc=to_utc, prefer_cloud_reads=False)
    numeric_val: float | None = None
    sample_count = 0
    if rows:
        row = rows[0] or {}
        v = row.get("value")
        try:
            numeric_val = float(v) if v is not None else None
        except Exception:
            numeric_val = None
        sample_count = int(row.get("count") or 0)
    # A text-typed tag stores its string in value_text and leaves `value`
    # NULL, and the aggregate over a column of NULLs comes back as 0.0 - not
    # None. Returning here on any non-None number therefore made a KPI cell
    # bound to a work order read "0.00" instead of "WO-4471", with nothing
    # to suggest it was the wrong column (2026-10-09, found while adding the
    # pill card).
    #
    # So zero is the one number worth a second look. The lookup below only
    # WINS if a text value actually exists, so a genuine reading of 0 still
    # renders as 0.
    if numeric_val is not None and numeric_val != 0.0:
        return (numeric_val, sample_count, None)
    # Text-tag fallback: look up the most recent text value via the historian
    # range query. If none, return the number (or None so the cell renders "-").
    try:
        rows_text = _app_store().get_historian_rows_range(
            gateway=rule["gateway_id"],
            tag=rule["tag_name"],
            from_utc=from_utc,
            to_utc=to_utc,
            limit=1,
            prefer_cloud_reads=False,
        )
    except Exception:
        rows_text = []
    if rows_text:
        text_value = rows_text[0].get("value_text") or None
        if text_value:
            return (None, sample_count or 1, str(text_value))
    return (numeric_val, sample_count, None)


def _fetch_pie_data(section: dict[str, Any]) -> list[tuple[str, float, str]]:
    """Return list of (label, value, color_hex) for the pie."""
    source = str(section.get("data_source_type") or "tag_direct").strip().lower()
    if source == "computed":
        rules = section.get("compute_rules") or []
        if not isinstance(rules, list) or not rules:
            return []
        rows = _app_store().get_historian_rule_stats(
            rules=rules,
            gateway=str(section.get("gateway_id") or ""),
            prefer_cloud_reads=False,
        )
        out: list[tuple[str, float, str]] = []
        for r in rows or []:
            try:
                out.append((str(r.get("label") or ""), float(r.get("value") or 0.0), str(r.get("color") or "#14a89a")))
            except Exception:
                continue
        return out
    # tag_direct
    from_utc, to_utc = _resolve_time_range(section.get("time_range"))
    stats = _app_store().get_historian_stats(
        from_utc=from_utc,
        to_utc=to_utc,
        gateway=str(section.get("gateway_id") or ""),
        tag=str(section.get("tag_name") or ""),
        prefer_cloud_reads=False,
    )
    agg = str(section.get("query_result_aggregation") or "count").strip().lower()
    out_d: list[tuple[str, float, str]] = []
    palette = _default_palette()
    for idx, r in enumerate(stats or []):
        try:
            if agg == "sum":
                val = float(r.get("sum") or 0.0)
            elif agg == "avg":
                val = float(r.get("avg") or 0.0)
            elif agg == "min":
                val = float(r.get("min") or 0.0)
            elif agg == "max":
                val = float(r.get("max") or 0.0)
            elif agg == "latest":
                val = float(r.get("latest") or 0.0)
            else:
                val = float(r.get("count") or 0.0)
            out_d.append((str(r.get("tag") or ""), val, palette[idx % len(palette)]))
        except Exception:
            continue
    return out_d


# --------------------------------------------------------------------------- #
# styling
# --------------------------------------------------------------------------- #
ACCENT_PRIMARY = colors.HexColor("#14a89a")
ACCENT_DARK = colors.HexColor("#0e8479")
INK_HEAD = colors.HexColor("#0f172a")
INK_BODY = colors.HexColor("#1f2937")
INK_MUTED = colors.HexColor("#64748b")
RULE_COLOR = colors.HexColor("#cbd5e1")
TABLE_HEAD_BG = colors.HexColor("#0e8479")
TABLE_ALT_BG = colors.HexColor("#f1f5f9")


def _default_palette() -> list[str]:
    return [
        "#14a89a", "#0e8479", "#3cd2c2", "#1f3a5f",
        "#6e8dd2", "#e0a050", "#e2585d", "#a78bfa",
    ]


def _build_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "TitleStyle", parent=base["Title"], textColor=INK_HEAD,
            fontSize=22, leading=26, alignment=0, spaceAfter=4,
        ),
        "subtitle": ParagraphStyle(
            "SubtitleStyle", parent=base["Normal"], textColor=INK_MUTED,
            fontSize=11, leading=14, spaceAfter=8,
        ),
        "section_title": ParagraphStyle(
            "SectionTitle", parent=base["Heading2"], textColor=INK_HEAD,
            fontSize=14, leading=18, spaceBefore=12, spaceAfter=6,
        ),
        "section_caption": ParagraphStyle(
            "SectionCaption", parent=base["Normal"], textColor=INK_MUTED,
            fontSize=9, leading=12, spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "Body", parent=base["Normal"], textColor=INK_BODY,
            fontSize=10, leading=14,
        ),
        "kpi_label": ParagraphStyle(
            "KPILabel", parent=base["Normal"], textColor=INK_MUTED,
            fontSize=8, leading=10, alignment=1,
        ),
        "kpi_value": ParagraphStyle(
            "KPIValue", parent=base["Normal"], textColor=INK_HEAD,
            fontSize=16, leading=20, alignment=1, spaceAfter=2,
        ),
        "footer": ParagraphStyle(
            "Footer", parent=base["Normal"], textColor=INK_MUTED,
            fontSize=8, leading=10, alignment=1,
        ),
    }


_LOGO_PATH_CACHE: Path | None = None


def _resolve_logo_path() -> Path | None:
    """Find the TrustNode brand logo to embed in PDF report headers.

    Matches the asset the in-app header uses (`trustenode-004.png`) so the
    branding is consistent across the dashboard chrome and exported reports.
    Falls back to the older `trustnode_logo.png` if the preferred file is
    missing. Result is cached after the first hit (PDF generation is a hot
    path). Returns None when nothing readable is found; the header then
    degrades to a text wordmark.
    """
    global _LOGO_PATH_CACHE
    if _LOGO_PATH_CACHE is not None:
        return _LOGO_PATH_CACHE if _LOGO_PATH_CACHE.exists() else None
    env_path = os.environ.get("TRUSTNODE_LOGO_PATH", "").strip()
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path))
    # PyInstaller frozen path: assets are extracted under sys._MEIPASS at runtime.
    # The spec embeds branding/ via the datas list, and we also keep a flat copy
    # at the bundle root for backward compatibility.
    frozen_base = getattr(sys, "_MEIPASS", None)
    if frozen_base:
        frozen_root = Path(frozen_base)
        candidates.extend([
            frozen_root / "branding" / "trustenode-004.png",
            frozen_root / "branding" / "trustnode_logo.png",
            frozen_root / "trustenode-004.png",
            frozen_root / "trustnode_logo.png",
        ])
    # Onedir frozen path: assets live next to the executable.
    if getattr(sys, "frozen", False):
        try:
            exe_dir = Path(sys.executable).resolve().parent
            candidates.extend([
                exe_dir / "branding" / "trustenode-004.png",
                exe_dir / "branding" / "trustnode_logo.png",
                exe_dir / "trustenode-004.png",
                exe_dir / "trustnode_logo.png",
            ])
        except Exception:
            pass
    # backend/app/services/report_renderer.py -> 3 parents up -> Trustnode_edge_app/
    here = Path(__file__).resolve()
    edge_app_root = here.parents[3] if len(here.parents) >= 4 else None
    if edge_app_root is not None:
        # Prefer the same asset rendered in the app's top-left header. The
        # public/assets path is the source-of-truth in dev; dist/assets is
        # where Vite emits it after build; web_cloud_readonly is the deployed
        # bundle. Public/<file>.png (no /assets/) is provided as a stable
        # backend-friendly location.
        candidates.extend([
            edge_app_root / "frontend" / "public" / "trustenode-004.png",
            edge_app_root / "frontend" / "public" / "assets" / "trustenode-004.png",
            edge_app_root / "frontend" / "dist" / "assets" / "trustenode-004.png",
            edge_app_root / "web_cloud_readonly" / "assets" / "trustenode-004.png",
            # Legacy fallback — older deployments only had this 457KB asset.
            edge_app_root / "frontend" / "public" / "trustnode_logo.png",
            edge_app_root / "frontend" / "dist" / "trustnode_logo.png",
            edge_app_root / "web_cloud_readonly" / "trustnode_logo.png",
        ])
    # Last resort: package data dir.
    candidates.append(Path.home() / ".trustnode_edge" / "data" / "trustenode-004.png")
    candidates.append(Path.home() / ".trustnode_edge" / "data" / "trustnode_logo.png")
    for path in candidates:
        try:
            if path.exists() and path.is_file():
                _LOGO_PATH_CACHE = path
                return path
        except Exception:
            continue
    return None


def _resolve_company_logo_path() -> Path | None:
    """Find the operator-uploaded company logo to embed on the LEFT side of
    PDF report headers. Reads app_settings.company_logo_path (set from the
    Interface page); falls back to ~/.trustnode_edge/data/company_logo.* so
    legacy installs that hand-dropped a file in the data dir still work.

    Returns the resolved Path, or None if no readable file is found."""
    try:
        from app.state import app_store as _app_store_singleton
        bootstrap = _app_store_singleton.get_bootstrap(prefer_cloud_reads=False) or {}
    except Exception:
        bootstrap = {}
    settings = bootstrap.get("app_settings") if isinstance(bootstrap.get("app_settings"), dict) else {}
    candidate_paths: list[Path] = []
    explicit = str((settings or {}).get("company_logo_path") or "").strip()
    if explicit:
        candidate_paths.append(Path(explicit))
    data_dir = Path(os.environ.get("TRUSTNODE_DATA_DIR") or
                    (Path.home() / ".trustnode_edge" / "data"))
    for ext in (".png", ".jpg", ".jpeg", ".gif"):
        candidate_paths.append(data_dir / f"company_logo{ext}")
    for p in candidate_paths:
        try:
            if p.is_file() and p.stat().st_size > 0:
                return p
        except Exception:
            continue
    return None


def _on_page(canvas, doc, *, branding_title: str = "TrustNode") -> None:
    canvas.saveState()
    from reportlab.lib.utils import ImageReader

    logo_h_mm = 12.0
    logo_top_mm = 4.0  # distance from page top to top of logo

    # Top-left: company logo (operator-uploaded via the Interface page).
    # When set, it sits in the same row as the TrustNode brand logo on the
    # right, mirroring the layout. Operators can replace it without
    # touching the bundled brand asset.
    company_logo_path = _resolve_company_logo_path()
    if company_logo_path is not None:
        try:
            reader = ImageReader(str(company_logo_path))
            iw, ih = reader.getSize()
            aspect = float(iw) / float(ih) if ih else 1.0
            logo_h = logo_h_mm * mm
            logo_w = logo_h * aspect
            max_w = 42 * mm
            if logo_w > max_w:
                logo_w = max_w
                logo_h = logo_w / aspect if aspect else logo_h
            canvas.drawImage(
                reader,
                15 * mm,
                A4[1] - logo_top_mm * mm - logo_h,
                width=logo_w,
                height=logo_h,
                mask="auto",
                preserveAspectRatio=True,
            )
        except Exception:
            # Bad image; leave the left side empty so we still publish.
            pass

    # Top-right: TrustNode brand logo (same trustnode_logo.png used by the app
    # header). Graceful fallback to a text wordmark only when the file is missing.
    logo_path = _resolve_logo_path()
    drew_logo = False
    if logo_path is not None:
        try:
            reader = ImageReader(str(logo_path))
            iw, ih = reader.getSize()
            aspect = float(iw) / float(ih) if ih else 1.0
            logo_h = logo_h_mm * mm
            logo_w = logo_h * aspect
            # Cap the width so a wide logo doesn't push past the page margin.
            max_w = 42 * mm
            if logo_w > max_w:
                logo_w = max_w
                logo_h = logo_w / aspect if aspect else logo_h
            canvas.drawImage(
                reader,
                A4[0] - 15 * mm - logo_w,
                A4[1] - logo_top_mm * mm - logo_h,
                width=logo_w,
                height=logo_h,
                mask="auto",
                preserveAspectRatio=True,
            )
            drew_logo = True
        except Exception:
            # Logo failed to decode for some reason; fall through to text.
            drew_logo = False
    if not drew_logo:
        # Visual fallback so the corner isn't empty when the asset can't be
        # found in this deployment. Uses the same accent colour as the rule.
        canvas.setFillColor(ACCENT_DARK)
        canvas.setFont("Helvetica-Bold", 11)
        canvas.drawRightString(A4[0] - 15 * mm, A4[1] - 11 * mm, branding_title)

    # Accent rule, lowered so it sits clearly under the logo strip.
    canvas.setStrokeColor(ACCENT_PRIMARY)
    canvas.setLineWidth(0.6)
    rule_y = A4[1] - (logo_top_mm + logo_h_mm + 2.0) * mm
    canvas.line(15 * mm, rule_y, A4[0] - 15 * mm, rule_y)

    # Footer
    canvas.setFillColor(INK_MUTED)
    canvas.setFont("Helvetica", 8)
    page_text = f"Page {doc.page}"
    canvas.drawRightString(A4[0] - 15 * mm, 10 * mm, page_text)
    canvas.drawString(15 * mm, 10 * mm, datetime.now(timezone.utc).strftime("Generated %Y-%m-%d %H:%M UTC"))
    canvas.restoreState()


# --------------------------------------------------------------------------- #
# section renderers
# --------------------------------------------------------------------------- #
#: Alignment names as ReportLab's Paragraph numbers them.
_ALIGN_CODES = {"left": 0, "center": 1, "centre": 1, "right": 2, "justify": 4}


def _hex_colour(value: Any, fallback=None):
    """A #rrggbb from the editor, or the fallback. Never raises: a typo in a
    colour box must not take the whole report down."""
    txt = str(value or "").strip()
    if not txt:
        return fallback
    if not txt.startswith("#"):
        txt = "#" + txt
    try:
        return colors.HexColor(txt)
    except Exception:                                   # noqa: BLE001
        return fallback


def styled_text_style(section: dict[str, Any], base: ParagraphStyle,
                      *, prefix: str = "") -> ParagraphStyle:
    """`base`, with the section's own font settings applied.

    2026-10-09, requested: "we should be able to modify fonts size, style and
    color, in special for text sections ... and how the text should be
    aligned". Every text block rendered with one hard-coded body style, so a
    heading inside a report could not be made bigger, a warning could not be
    red, and nothing could be centred.

    `prefix` lets one section carry two independent settings - the title and
    the body - without inventing a second section type. Anything not set
    falls through to `base`, so existing templates render exactly as before.
    """
    g = lambda k: section.get(prefix + k)               # noqa: E731
    size = g("font_size")
    colour = _hex_colour(g("font_color"), None)
    bold = bool(g("bold"))
    italic = bool(g("italic"))
    align = str(g("align") or "").strip().lower()
    if (size in (None, "") and colour is None and not bold and not italic
            and align not in _ALIGN_CODES):
        return base                                     # nothing to override

    try:
        pt = float(size)
    except (TypeError, ValueError):
        pt = float(base.fontSize)
    pt = max(5.0, min(72.0, pt))

    # Helvetica is the base family; the bold/italic faces are named variants.
    face = "Helvetica"
    if bold and italic:
        face = "Helvetica-BoldOblique"
    elif bold:
        face = "Helvetica-Bold"
    elif italic:
        face = "Helvetica-Oblique"
    else:
        face = base.fontName or "Helvetica"

    return ParagraphStyle(
        "%s-%s" % (base.name, prefix or "body"), parent=base,
        fontName=face,
        fontSize=pt,
        # Leading has to follow the size or a 24pt line overlaps the next one.
        leading=max(pt * 1.25, pt + 2),
        textColor=colour if colour is not None else base.textColor,
        alignment=_ALIGN_CODES.get(align, base.alignment),
    )


def pill_band(text: str, styles, *, background: Any = None, color: Any = None,
              corners: str = "both", radius: Any = None, align: str = "left",
              font_size: Any = None, bold: bool = True, width: float = 0.0):
    """ONE rounded, filled band with text inside it.

    2026-10-09, clarified: "it need to be a header but just in a pill style
    ... the value inside of the pill is the title too". The first attempt
    drew a heading AND a band under it, so the screenshot showed "INITIAL
    TEST" above a separate pill reading "Result PASS". The band IS the
    heading: one thing, one line.

    Built as a single-cell Table because that is what carries ROUNDEDCORNERS
    in ReportLab - the same mechanism the pill cards use, so the two always
    look like the same component.
    """
    try:
        size = float(font_size or 0) or 13.0
    except (TypeError, ValueError):
        size = 13.0
    size = max(6.0, min(48.0, size))
    try:
        rad = float(radius or 0)
    except (TypeError, ValueError):
        rad = 0.0
    if rad <= 0:
        rad = size
    radii = {
        "both": [rad, rad, rad, rad],
        "left": [rad, 0, 0, rad],
        "right": [0, rad, rad, 0],
        "top": [rad, rad, 0, 0],
        "bottom": [0, 0, rad, rad],
        "none": [0, 0, 0, 0],
        "square": [0, 0, 0, 0],
    }.get(str(corners or "both").strip().lower(), [rad, rad, rad, rad])

    bg = _hex_colour(background, colors.HexColor("#14a89a"))
    ink = _hex_colour(color, colors.white)
    para = ParagraphStyle(
        "PillBand", parent=styles["body"], fontSize=size,
        leading=size * 1.25, textColor=ink,
        alignment=_ALIGN_CODES.get(str(align or "left").strip().lower(), 0),
        fontName="Helvetica-Bold" if bold else "Helvetica",
    )
    safe = (str(text or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    table = Table([[Paragraph(safe, para)]], colWidths=[width or None])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), bg),
        ("VALIGN", (0, 0), (0, 0), "MIDDLE"),
        ("TOPPADDING", (0, 0), (0, 0), size * 0.55),
        ("BOTTOMPADDING", (0, 0), (0, 0), size * 0.55),
        ("LEFTPADDING", (0, 0), (0, 0), size * 0.9),
        ("RIGHTPADDING", (0, 0), (0, 0), size * 0.9),
        ("ROUNDEDCORNERS", radii),
    ]))
    table.hAlign = "LEFT"
    return table


def _section_header(section: dict[str, Any], styles, story: list) -> None:
    """The report banner: title, subtitle, generated-at stamp.

    2026-10-09, requested: "all the text section including the title header
    one should have the option to centralized and make font adjustments and
    alignment". The banner was the one piece of text with no controls at
    all - which is the piece most people want centred.

    Three independent runs, so a centred 28pt title can sit over a smaller
    left-aligned subtitle: `title_*`, `subtitle_*` and `stamp_*`.
    """
    title = str(section.get("title") or "Report").strip() or "Report"
    if section.get("as_pill"):
        # The heading IS the pill. The title text - which may be a {TagName}
        # token, resolved before we get here - sits inside the band, and
        # nothing is drawn above it.
        story.append(pill_band(
            title, styles,
            background=section.get("pill_background"),
            color=section.get("pill_color") or section.get("title_font_color"),
            corners=section.get("pill_corners") or "both",
            radius=section.get("pill_radius"),
            align=section.get("title_align") or "left",
            font_size=section.get("title_font_size") or 13,
            bold=section.get("title_bold") is not False,
            width=_avail_width(section),
        ))
    else:
        story.append(Paragraph(
            title, styled_text_style(section, styles["title"], prefix="title_")))
    subtitle = str(section.get("subtitle") or "").strip()
    if subtitle:
        story.append(Paragraph(
            subtitle,
            styled_text_style(section, styles["subtitle"], prefix="subtitle_")))
    if section.get("show_generated_at") is not False:
        story.append(Paragraph(
            datetime.now(timezone.utc).strftime("Generated %Y-%m-%d %H:%M:%S UTC"),
            styled_text_style(section, styles["section_caption"], prefix="stamp_"),
        ))
    story.append(Spacer(1, 4 * mm))


def _section_text(section: dict[str, Any], styles, story: list) -> None:
    title = str(section.get("title") or "").strip()
    if title:
        # `title_*` keys style the heading, the bare keys style the body, so
        # one text section can hold a large coloured heading over normal
        # prose without needing two sections.
        story.append(Paragraph(
            title, styled_text_style(section, styles["section_title"],
                                     prefix="title_")))
    text = str(section.get("text") or section.get("html") or "").strip()
    if not text:
        return
    # Treat as plain text; reportlab Paragraph escapes for safety.
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")
    story.append(Paragraph(safe, styled_text_style(section, styles["body"])))
    story.append(Spacer(1, 3 * mm))


def _section_image(section: dict[str, Any], styles, story: list) -> None:
    """Embed an operator-supplied image in the report flow. The section
    payload accepts either a local file path (path) or a base64-encoded
    data URI (data_url). Width is capped at the printable page width;
    aspect ratio is preserved. Use `align` ('left' / 'center' / 'right')
    and `width_mm` (1 .. 180) to tune the placement."""
    title = str(section.get("title") or "").strip()
    if title:
        story.append(Paragraph(title, styles["section_title"]))
    caption = str(section.get("caption") or "").strip()

    img_reader = None
    try:
        from reportlab.lib.utils import ImageReader
        data_url = str(section.get("data_url") or "").strip()
        if data_url.startswith("data:"):
            # data:image/png;base64,XXXX
            try:
                _, payload = data_url.split(",", 1)
            except ValueError:
                payload = ""
            if payload:
                import base64 as _b64
                img_bytes = _b64.b64decode(payload, validate=False)
                img_reader = ImageReader(io.BytesIO(img_bytes))
        if img_reader is None:
            path_str = str(section.get("path") or "").strip()
            if path_str:
                path = Path(path_str).expanduser()
                # When the operator types a relative filename, look in the
                # standard data dir so they don't have to memorize an
                # absolute path.
                if not path.is_absolute():
                    data_dir = Path(os.environ.get("TRUSTNODE_DATA_DIR") or
                                    (Path.home() / ".trustnode_edge" / "data"))
                    path = data_dir / path
                if path.is_file():
                    img_reader = ImageReader(str(path))
    except Exception as exc:
        story.append(Paragraph(f"Image failed to decode: {exc}", styles["section_caption"]))
        return

    if img_reader is None:
        story.append(Paragraph("No image source configured.", styles["section_caption"]))
        return

    page_width = _avail_width(section)
    try:
        iw_px, ih_px = img_reader.getSize()
    except Exception:
        iw_px, ih_px = (1, 1)
    aspect = float(iw_px) / float(ih_px) if ih_px else 1.0
    width_mm = section.get("width_mm")
    try:
        width_mm = float(width_mm) if width_mm is not None and width_mm != "" else None
    except Exception:
        width_mm = None
    if width_mm is None or width_mm <= 0:
        # Default: 70 % of the printable width — feels at home next to
        # adjacent KPI and chart blocks.
        target_w = page_width * 0.7
    else:
        target_w = min(page_width, width_mm * mm)
    target_h = target_w / aspect if aspect else target_w * 0.5
    # Soft cap so a tall portrait image doesn't blow past a page.
    max_h = 130 * mm
    if target_h > max_h:
        target_h = max_h
        target_w = target_h * aspect

    align = str(section.get("align") or "center").lower()
    h_align = "RIGHT" if align == "right" else ("LEFT" if align == "left" else "CENTER")

    img = Image(img_reader, width=target_w, height=target_h, hAlign=h_align)
    story.append(img)
    if caption:
        story.append(Paragraph(caption, styles["section_caption"]))
    story.append(Spacer(1, 3 * mm))


#: Full printable width of the page body.
_FULL_WIDTH = A4[0] - 30 * mm


def _avail_width(section: dict[str, Any]) -> float:
    """How wide this section may draw.

    A section inside a `row` is handed `_width`; everything else gets the full
    page. Without this a chart placed in a column still drew itself at full
    page width and overflowed its cell.
    """
    try:
        w = float(section.get("_width") or 0)
    except (TypeError, ValueError):
        w = 0.0
    return w if w > 60 else _FULL_WIDTH


def _section_pill_card(section: dict[str, Any], styles, story: list) -> None:
    """Rounded status pills - a label, a value, and a colour that means it.

    2026-10-09, requested: "a new section that would be a pill card style
    that we can format color, backgroud and aligment to, this also can be
    linked to a plc tag".

    A KPI grid answers "what was the number"; a pill answers "is it alright".
    PASS in green beside a work order in grey reads at arm's length on a
    printed test report in a way a table of figures does not.

    Each pill carries:
      label          text to the left, optional
      text           literal text, or {TagName} / {max:Tag} tokens
      gateway_id +
      tag_name       read live from the section's window, like a KPI cell
      aggregation    last | first | avg | min | max   (default last)
      unit           appended to a numeric value
      color          the TEXT colour
      background     the pill's fill
      border_color   an outline, when the fill is pale

    Section-level: `align` (left / center / right), `columns`, `font_size`,
    and `pill_radius`. A pill bound to a tag that has no reading shows "-",
    never a blank - a missing reading has to be visible.
    """
    title = str(section.get("title") or "").strip()
    # When the band IS the heading, a separate heading above it is the
    # duplication the operator photographed: "INITIAL TEST" sitting over a
    # pill that read "Result PASS" (2026-10-09).
    if title and not section.get("as_header"):
        story.append(Paragraph(
            title, styled_text_style(section, styles["section_title"],
                                     prefix="title_")))

    pills = section.get("pills") or section.get("items") or []
    if not isinstance(pills, list) or not pills:
        story.append(Paragraph("No pills configured.", styles["section_caption"]))
        return

    frm, to = _resolve_time_range(section.get("time_range"))
    gw = str(section.get("gateway_id") or "")

    try:
        size = float(section.get("font_size") or 0) or 10.0
    except (TypeError, ValueError):
        size = 10.0
    size = max(6.0, min(28.0, size))
    try:
        radius = float(section.get("pill_radius") or 0)
    except (TypeError, ValueError):
        radius = 0.0
    if radius <= 0:
        radius = size            # fully rounded ends by default
    # Which corners are rounded. ReportLab wants the four radii in the order
    # top-left, top-right, bottom-right, bottom-left - so "left" is the two
    # on the left edge, which is what makes a pair of pills read as one
    # joined badge (2026-10-09: "the pill section should have option to have
    # one side rounded, not rounded and both").
    corner_mode = str(section.get("pill_corners") or "both").strip().lower()
    corners = {
        "both": [radius, radius, radius, radius],
        "left": [radius, 0, 0, radius],
        "right": [0, radius, radius, 0],
        "top": [radius, radius, 0, 0],
        "bottom": [0, 0, radius, radius],
        "none": [0, 0, 0, 0],
        "square": [0, 0, 0, 0],
    }.get(corner_mode, [radius, radius, radius, radius])

    cells: list = []
    styles_for: list = []
    for pill in pills:
        if not isinstance(pill, dict):
            continue
        label = str(pill.get("label") or "").strip()

        # A tag binding wins over literal text: it is the more specific
        # instruction, and leaving both set should not be ambiguous.
        value_text = ""
        if str(pill.get("tag_name") or "").strip():
            # "latest", not "last": the fetcher's vocabulary is
            # count/sum/avg/min/max/latest, and an unrecognised name falls
            # through to its COUNT branch - a pill bound to a text tag read
            # "30.00" (the number of rows) instead of the work order.
            agg = str(pill.get("aggregation") or "latest").strip().lower()
            if agg in ("last", "", "value"):
                agg = "latest"
            item = {
                "gateway_id": str(pill.get("gateway_id") or gw),
                "tag_name": str(pill.get("tag_name")),
                "aggregation": agg,
            }
            value, sample_n, text_val = _fetch_kpi_value(item, frm, to)
            # A mask turns a raw reading into the word it stands for -
            # "1" is PASS on a sign-off sheet, not 1.00. It applies before
            # any other formatting, including the "-" for a missing
            # reading: a mask may deliberately label that case via "*".
            masked = apply_value_map(parse_value_map(pill.get("value_map")),
                                     value, text_val)
            if masked is not None and (sample_n or text_val):
                value_text = masked
            elif text_val:
                value_text = text_val if len(text_val) <= 32 else text_val[:31] + "\u2026"
            elif value is None or not sample_n:
                # No reading in the window. "-", never 0: a tag that was
                # never recorded must not render as a confident zero, which
                # is how an unplugged device reads as a healthy one.
                value_text = "-"
            else:
                value_text = (f"{value:.2f}" if abs(value) < 1000 else f"{value:.0f}")
                unit = str(pill.get("unit") or "").strip()
                if unit:
                    value_text = "%s %s" % (value_text, unit)
        else:
            raw = str(pill.get("text") or "")
            value_text = resolve_text_tokens(raw, frm, to, gw) if "{" in raw else raw

        body = " ".join(x for x in (label, value_text) if x).strip() or "-"
        # The editor shows a swatch for every pill, so an unset colour must
        # resolve to the SAME thing the swatch shows or the PDF will not
        # match the picker. White text is the default only when there is a
        # fill behind it; on no background it would be invisible.
        _bg_set = _hex_colour(pill.get("background"), None) is not None
        ink = _hex_colour(pill.get("color"),
                          colors.white if _bg_set else INK_HEAD)
        # A pill centres its text because it is a badge; a header band reads
        # as a heading, so it follows the section's own alignment instead.
        _hdr = bool(section.get("as_header"))
        para = ParagraphStyle(
            "Pill", parent=styles["body"],
            fontSize=size * (1.35 if _hdr else 1.0),
            leading=size * (1.35 if _hdr else 1.0) * 1.2,
            textColor=ink,
            alignment=(_ALIGN_CODES.get(
                str(section.get("align") or "left").strip().lower(), 0)
                if _hdr else 1),
            fontName=("Helvetica-Bold"
                      if (pill.get("bold") or _hdr) else "Helvetica"),
        )
        cells.append(Paragraph(
            body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
            para))
        styles_for.append(pill)

    if not cells:
        story.append(Paragraph("No pills configured.", styles["section_caption"]))
        return

    # 2026-10-09, requested: "the pill also should have a option to be a
    # header". A coloured band across the page, used as the heading for the
    # part of the report that follows - the same pill, given the whole line
    # and a larger face, instead of a separate section type to keep in step.
    as_header = bool(section.get("as_header"))

    # One table cell per pill, so each keeps its own fill and the row stays
    # on one line. Columns cap how many sit side by side.
    cols = 1 if as_header else max(1, min(int(section.get("columns") or len(cells)), 8))
    avail = _avail_width(section)
    per = avail / cols

    rows: list[list] = []
    styling: list[list] = []
    for i in range(0, len(cells), cols):
        rows.append(cells[i:i + cols])
        styling.append(styles_for[i:i + cols])
    # Pad the last row so the table is rectangular.
    while len(rows[-1]) < cols:
        rows[-1].append("")
        styling[-1].append(None)

    cmds = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), size * (0.8 if as_header else 0.45)),
        ("BOTTOMPADDING", (0, 0), (-1, -1), size * (0.8 if as_header else 0.45)),
        ("LEFTPADDING", (0, 0), (-1, -1), size * 0.8),
        ("RIGHTPADDING", (0, 0), (-1, -1), size * 0.8),
        ("ROUNDEDCORNERS", corners),
    ]
    for r, row_styles in enumerate(styling):
        for c, pill in enumerate(row_styles):
            if not isinstance(pill, dict):
                continue
            bg = _hex_colour(pill.get("background"), None)
            if bg is not None:
                cmds.append(("BACKGROUND", (c, r), (c, r), bg))
            edge = _hex_colour(pill.get("border_color"), None)
            if edge is not None:
                cmds.append(("BOX", (c, r), (c, r), 0.6, edge))

    table = Table(rows, colWidths=[per] * cols)
    table.setStyle(TableStyle(cmds))
    table.hAlign = ("LEFT" if as_header else _ALIGNMENTS.get(
        str(section.get("align") or "").strip().lower(), "LEFT"))
    story.append(table)
    story.append(Spacer(1, 3 * mm))


def _section_kpi_grid(section: dict[str, Any], styles, story: list) -> None:
    title = str(section.get("title") or "").strip()
    if title:
        story.append(Paragraph(title, styles["section_title"]))
    items = section.get("items") or []
    if not isinstance(items, list) or not items:
        story.append(Paragraph("No KPI items configured.", styles["section_caption"]))
        return
    cells: list[list] = []
    row: list = []
    labels: list[str] = []
    values: list[str] = []
    cols = max(1, min(int(section.get("columns") or 4), 6))
    # The section's own window. This is the PDF's KPI grid - a SECOND dispatch
    # site from the JSON payload builder, and it was still calling without a
    # window after the payload one was fixed, so every KPI in every rendered
    # report went on aggregating all of history.
    kpi_from, kpi_to = _resolve_time_range(section.get("time_range"))
    for item in items:
        value, sample_count, text_val = _fetch_kpi_value(item, kpi_from, kpi_to)
        label = str(item.get("label") or item.get("tag_name") or "")
        _masked = apply_value_map(parse_value_map(item.get("value_map")),
                                  value, text_val)
        if _masked is not None and (sample_count or text_val):
            # Same mask vocabulary as the pills: a KPI that reads PASS/FAIL
            # is far more use on a test report than one that reads 1.00.
            value_text = _masked
        elif text_val:
            # Text-typed tag: show the latest string verbatim. Trim long
            # values so they fit a KPI cell — full text stays in the data
            # table sections if needed.
            display = text_val if len(text_val) <= 24 else text_val[:23] + "…"
            value_text = display
        elif value is None or not sample_count:
            # sample_count too, not just None. A tag with NO rows in the
            # window aggregates to 0.0, and a KPI reading "0.00" for a
            # device that was never collected is the same failure as an
            # unplugged block showing a confident zero (2026-10-09).
            value_text = "-"
        else:
            value_text = (f"{value:.2f}" if abs(value) < 1000 else f"{value:.0f}")
        # Fixed value/label bands. A plain [value, label] cell with VALIGN
        # MIDDLE centres the PAIR, so a label that wraps to two lines pushes
        # its number up and the row of figures no longer reads as a row.
        cell = Table(
            [[Paragraph(value_text, styles["kpi_value"])],
             [Paragraph(label, styles["kpi_label"])]],
            rowHeights=[22, 24],
            style=TableStyle([
                ("VALIGN", (0, 0), (0, 0), "MIDDLE"),
                ("VALIGN", (0, 1), (0, 1), "TOP"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]))
        row.append(cell)
        labels.append(label)
        values.append(value_text)
        if len(row) == cols:
            cells.append(row)
            row = []
    if row:
        while len(row) < cols:
            row.append("")
        cells.append(row)
    page_width = _avail_width(section)
    col_w = page_width / cols

    # 2026-10-02: "it should be formatted to minimum space on top and bottom
    # and the same" - the cells carried 8pt above and below on top of the
    # value/label bands, so a KPI strip was mostly air. 3pt, equal.
    style = str(section.get("kpi_style") or "cards").strip().lower()
    if style == "table":
        # A plain two-row table: labels across the top, values beneath.
        head = [Paragraph(l, styles["kpi_label"]) for l in labels]
        vals = [Paragraph(v, styles["kpi_value"]) for v in values]
        while len(head) % cols:
            head.append("")
            vals.append("")
        rows_out: list[list] = []
        for i in range(0, len(head), cols):
            rows_out.append(head[i:i + cols])
            rows_out.append(vals[i:i + cols])
        table = Table(rows_out, colWidths=[col_w] * cols)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
            ("BOX", (0, 0), (-1, -1), 0.4, RULE_COLOR),
            ("INNERGRID", (0, 0), (-1, -1), 0.3, RULE_COLOR),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
    else:
        table = Table(cells, colWidths=[col_w] * cols)
        card = [
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
            ("BOX", (0, 0), (-1, -1), 0.4, RULE_COLOR),
            ("INNERGRID", (0, 0), (-1, -1), 0.3, RULE_COLOR),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
        if str(section.get("kpi_corners") or "round").strip().lower() != "square":
            # Rounded corners read as cards rather than as a grid.
            card.append(("ROUNDEDCORNERS", [5, 5, 5, 5]))
        table.setStyle(TableStyle(card))
    story.append(table)
    story.append(Spacer(1, 3 * mm))


#: The same five the dashboard widgets offer, by the same names, so a chart
#: configured on the dashboard and a chart in a report mean the same thing.
#: "linear" is the report default: it is what every existing report was
#: drawn with, and changing that silently would redraw them all.
CHART_INTERPOLATIONS = ("linear", "monotone", "natural", "stepBefore", "stepAfter")


def norm_interpolation(value: Any, fallback: str = "linear") -> str:
    v = str(value or "").strip()
    return v if v in CHART_INTERPOLATIONS else fallback


def _interpolate(coords: list[tuple[float, float]], mode: str) -> list[tuple[float, float]]:
    """Expand a point list into the polyline that will actually be drawn.

    2026-10-11, reported: "we also are missing the interpolation option for
    charts". The PDF drew straight segments between samples and nothing else,
    so a stepped signal (a setpoint, a state) was drawn as a ramp it never
    performed - a diagonal line between two readings implies values that were
    never measured.

    Everything downstream still receives a plain list of points, so the area
    fill and the line always agree.
    """
    mode = norm_interpolation(mode)
    if mode == "linear" or len(coords) < 2:
        return coords
    if mode in ("stepAfter", "stepBefore"):
        out: list[tuple[float, float]] = [coords[0]]
        for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
            # stepAfter holds the OLD value to the next sample, then jumps;
            # stepBefore jumps at the old sample, then holds.
            out.append((x1, y0) if mode == "stepAfter" else (x0, y1))
            out.append((x1, y1))
        return out

    # monotone / natural: Catmull-Rom through the samples, subdivided into
    # short segments. The curve passes through every reading either way;
    # "monotone" additionally refuses to leave the span of the two samples
    # it sits between, so a smoothed line cannot invent a peak that the
    # data does not contain.
    SEG = 8
    clamp = (mode == "monotone")
    out = [coords[0]]
    n = len(coords)
    for i in range(n - 1):
        p0 = coords[i - 1] if i > 0 else coords[i]
        p1 = coords[i]
        p2 = coords[i + 1]
        p3 = coords[i + 2] if i + 2 < n else coords[i + 1]
        lo, hi = (p1[1], p2[1]) if p1[1] <= p2[1] else (p2[1], p1[1])
        for step in range(1, SEG + 1):
            t = step / float(SEG)
            t2 = t * t
            t3 = t2 * t
            x = 0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t
                       + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                       + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
            y = 0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t
                       + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                       + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
            if clamp:
                y = min(hi, max(lo, y))
            out.append((x, y))
    return out


def _thin_evenly(points: list, cap: Any) -> list:
    """At most `cap` points, spread across the whole list.

    Dropping the tail would shorten the window; taking every Nth keeps the
    first and last reading and the shape between them. A cap that is absent,
    unparseable or larger than the data leaves the list alone.
    """
    try:
        n = int(float(cap))
    except (TypeError, ValueError):
        return points
    if n < 2 or len(points) <= n:
        return points
    step = len(points) / float(n)
    out = [points[min(len(points) - 1, int(i * step))] for i in range(n)]
    # The last reading is the one an operator looks for; keep it exactly.
    if out[-1] is not points[-1]:
        out[-1] = points[-1]
    return out


def _empty_chart_reason(section: dict[str, Any],
                        series_meta: list[dict[str, Any]] | None) -> str:
    """Why this chart has nothing to draw, in the operator's terms.

    2026-10-11: "we faced some problems when the chart for some reason was
    not printed in the pdf for not enough data etc". Every one of these cases
    printed the same sentence, and the real cause was usually neither the
    chart nor the data:

      * no series configured at all
      * a series with no tag chosen
      * a section trigger that never fired, so the window is _NEVER
      * the tag is genuinely not being collected in that window
    """
    if not series_meta:
        tagged = [sv for sv in (section.get("series") or [])
                  if isinstance(sv, dict) and str(sv.get("tag_name") or "").strip()]
        if not (section.get("series") or []):
            return "No series configured for this chart."
        if not tagged:
            return "No tag selected for this chart's series."
    rng = section.get("time_range") if isinstance(section.get("time_range"), dict) else {}
    frm = str((rng or {}).get("from_utc") or "")
    if frm.startswith(_NEVER[:4]):
        return ("The section trigger never became true in the report window, "
                "so there is no period to chart.")
    window = ("%s to %s" % (frm[:19], str((rng or {}).get("to_utc") or "")[:19])
              if frm else "the selected range")
    return "No readings stored for these tags in %s." % window


def _section_chart(section: dict[str, Any], styles, story: list) -> None:
    """Render a multi-series chart with optional dual Y axes and limit lines.

    Section options honored:
      - series[]: one or more {gateway_id, tag_name, label, color, axis,
        chart_type ("line"|"area"|"bar"), unit, multiplier, offset}
      - x_axis_label, y_axis_label, y_axis_unit, y_axis_right_label, y_axis_right_unit
      - y_min, y_max, y_right_min, y_right_max (optional explicit ranges)
      - limit_lines[]: [{value, axis, label, color, dash}]
      - show_legend (default True), legend_align ("left"|"center"|"right")
      - y_tick_count: how many intervals the Y scale is divided into
      - line_width: default stroke for every series; a series may override
      - value_format ("auto"|"int"|"2dp"|"3dp") for axis tick labels
    """
    # Collected, then emitted as ONE flowable. Appending the title separately
    # let a page break fall between the heading and its chart - which is most
    # of the white space in a long report.
    block: list[Any] = []
    title = str(section.get("title") or "").strip()
    if title:
        block.append(Paragraph(title, styles["section_title"]))

    # 240 rows is a sensible budget for a TABLE and far too small for a plot:
    # it decided how much of the window a chart could see. Data tables keep
    # their own limits.
    #
    # 2026-10-11, requested: "we need to make sure thae samples colleted to
    # be the same if the triggers are the same". Two sections on the same
    # run window disagreed because each carried its own Sample count, and
    # that number shrank the FETCH - so a section capped at 200 read a
    # different slice of the window than one capped at 1999. The window now
    # decides what is read; the cap only decides how many of those points
    # are drawn, applied evenly across the window afterwards. Same window
    # and same cap therefore always give the same count.
    _cap = section.get("readings_count")
    _fetch_section = dict(section)
    _fetch_section.pop("readings_count", None)
    series_meta, aligned = _fetch_multi_series(_fetch_section, default_limit=5000)
    aligned = [_thin_evenly(pts, _cap) for pts in aligned]
    if not series_meta or all(not pts for pts in aligned):
        # 2026-10-11, reported: "the chart for some reason was not printed in
        # the pdf for not enough data". One message covered four different
        # causes, so there was no way to tell a mis-set trigger from a tag
        # that is simply not being collected. Name the cause.
        block.append(Paragraph(_empty_chart_reason(section, series_meta),
                               styles["section_caption"]))
        _emit_block(section, block, story)
        return

    # Aggregate numeric values per axis for range computation.
    # _fetch_multi_series returns 3-tuples (ts, value, value_text) — the third
    # element carries the original string for text-typed tags. Charts only
    # care about the numeric value column.
    axis_values: dict[str, list[float]] = {a: [] for a in AXES4}
    max_len = 0
    for meta, pts in zip(series_meta, aligned):
        nums = [p[1] for p in pts if p[1] is not None and math.isfinite(p[1])]
        max_len = max(max_len, len(pts))
        axis_values[norm_axis4(meta.get("axis"))].extend(nums)

    if not any(axis_values.values()):
        block.append(Paragraph("No numeric values available.", styles["section_caption"]))
        _emit_block(section, block, story)
        return

    show_legend = section.get("show_legend") is not False
    assigned = {norm_axis4(m.get("axis")) for m in series_meta}
    # An axis is drawn when something is actually on it. left1 is the fallback
    # so a chart always has at least one scale.
    used = {a for a in AXES4 if axis_values[a] or a in assigned}
    if not used:
        used = {"left1"}
    has_right_axis = bool(used & {"right1", "right2"})

    # Everything under the plot gets its OWN band. Before this they shared the
    # same 10pt: the timestamp ticks were drawn at chart_bottom-10 and the
    # legend swatches at chart_bottom-18, so on every chart in every report the
    # legend sat on top of the axis labels.
    x_title = str(section.get("x_axis_label") or "").strip()

    def _axis_title(axis: str) -> str:
        """Label plus its unit, for one axis. The unit belongs on the axis it
        describes - appending it to the top tick produced labels like
        "31.66C" where a number should be."""
        keys = _AXIS_KEYS[axis]
        label = str(section.get(keys["label"]) or "").strip()
        unit = str(section.get(keys["unit"]) or "").strip()
        if unit:
            return ("%s (%s)" % (label, unit)).strip() if label else unit
        return label

    axis_title = {a: _axis_title(a) for a in AXES4}
    y_title = axis_title["left1"]
    y_right_title = axis_title["right1"]

    palette = _default_palette()

    # 2026-10-11, requested: "all charts we should be able to change the min
    # and max for axis and ticks too". ONE count for the chart, not one per
    # axis: the horizontal grid lines are shared, so differing counts would
    # draw each axis's labels between the other's grid lines.
    try:
        tick_count = int(float(section.get("y_tick_count") or 5))
    except (TypeError, ValueError):
        tick_count = 5
    tick_count = max(2, min(12, tick_count))

    fmt = str(section.get("value_format") or "auto")

    def _format_tick(v: float, preset: str = "") -> str:
        preset = preset or fmt
        try:
            if preset == "int":
                return f"{int(round(v))}"
            if preset == "2dp":
                return f"{v:.2f}"
            if preset == "3dp":
                return f"{v:.3f}"
            if abs(v) >= 1000:
                return f"{v:.0f}"
            return f"{v:.2f}"
        except Exception:
            return str(v)

    def _domain(values: list[float], explicit_min: Any, explicit_max: Any) -> tuple[float, float]:
        if not values:
            return (0.0, 1.0)
        lo = min(values)
        hi = max(values)
        # 2026-10-11: an axis Min of 900 produced a scale starting at 884,
        # because the headroom padding was applied to the operator's number
        # as well as to the measured one. A bound that was TYPED is the
        # bound; only a bound we inferred needs room above the data.
        fixed_lo = fixed_hi = False
        if explicit_min is not None and str(explicit_min) != "":
            try:
                lo = float(explicit_min)
                fixed_lo = True
            except Exception:
                pass
        if explicit_max is not None and str(explicit_max) != "":
            try:
                hi = float(explicit_max)
                fixed_hi = True
            except Exception:
                pass
        if hi == lo:
            pad = abs(hi or 1.0) * 0.05 or 0.5
            return (lo if fixed_lo else lo - pad, hi if fixed_hi else hi + pad)
        span = hi - lo
        pad = max(span * 0.08, 0.25)
        return (lo if fixed_lo else lo - pad, hi if fixed_hi else hi + pad)

    axis_domain = {
        a: _domain(axis_values[a],
                   section.get(_AXIS_KEYS[a]["min"]),
                   section.get(_AXIS_KEYS[a]["max"]))
        for a in AXES4
    }

    def _axis_domain_preview(axis: str) -> tuple[float, float]:
        return axis_domain[axis]

    X_TITLE_BAND = 12 if x_title else 0
    TICK_BAND = 11
    page_width = _avail_width(section)

    # --- horizontal gutters first ---------------------------------------
    # Each drawn axis needs its own: the tick numbers, plus a rotated title
    # when there is one. The second axis on a side sits OUTBOARD of the
    # first, so the widths add up rather than overlapping. This has to be
    # settled before the legend, because the legend wraps to the PLOT width.
    # 2026-10-11, requested: labels and units "respecting spaces, makking
    # sure they are not overlaping texts". A fixed 34pt gutter was enough for
    # "31.66" and not for "1013.250" or a negative number, so a wide scale ran
    # its tick labels into the rotated axis title (or off the page on the
    # outer axes). Measure the widest label this axis will actually print.
    TICK_FONT = ("Helvetica", 7)

    def _tick_gutter(axis: str) -> float:
        lo, hi = _axis_domain_preview(axis)
        widest = 0.0
        for i in range(0, tick_count + 1):
            txt = _format_tick(lo + (hi - lo) * i / float(tick_count))
            widest = max(widest, stringWidth(txt, TICK_FONT[0], TICK_FONT[1]))
        # 4pt of air between the plot edge and the label, 4pt after it.
        return min(70.0, max(20.0, widest + 8.0))

    axis_gutter = {a: _tick_gutter(a) for a in AXES4}

    def _side_width(axes: tuple[str, ...]) -> float:
        total = 0.0
        for a in axes:
            if a in used:
                total += axis_gutter[a] + (12 if axis_title[a] else 0)
        return total

    chart_left = max(18.0, _side_width(("left1", "left2"))) + 12
    chart_right = page_width - (max(18.0, _side_width(("right1", "right2"))) + 6)
    chart_w = max(60, chart_right - chart_left)

    # Where each axis's ticks and title sit, measured outwards from the plot.
    axis_x: dict[str, dict[str, float]] = {}
    cursor = chart_left
    for a in ("left1", "left2"):
        if a not in used:
            continue
        axis_x[a] = {"tick": cursor - 4,
                     "title": cursor - (axis_gutter[a] + 4), "anchor": -1}
        cursor -= axis_gutter[a] + (12 if axis_title[a] else 0)
    cursor = chart_right
    for a in ("right1", "right2"):
        if a not in used:
            continue
        axis_x[a] = {"tick": cursor + 4,
                     "title": cursor + (axis_gutter[a] + 4), "anchor": 1}
        cursor += axis_gutter[a] + (12 if axis_title[a] else 0)

    # --- legend, laid out BEFORE the canvas is sized ---------------------
    # It used to draw left-to-right and `break` on running out of room, so a
    # plotted series could simply be absent from the key - and with four
    # axes the plot is narrower, so that bit sooner. Wrap to as many rows as
    # it takes and pay for them in canvas height instead (2026-10-08).
    LEGEND_ROW_H = 13

    def _legend_label(meta: dict[str, Any]) -> str:
        txt = str(meta.get("label") or meta.get("tag_name") or "")
        ax = norm_axis4(meta.get("axis"))
        # Say WHICH scale a series is read against. With four axes "(R)" is
        # not enough to tell right1 from right2.
        if len(used) > 1 and ax != "left1":
            txt += " (%s)" % {"left2": "L2", "right1": "R", "right2": "R2"}[ax]
        unit = str(meta.get("unit") or "").strip()
        if unit:
            txt += " [%s]" % unit
        return txt[:48]

    legend_rows: list[list[tuple[str, str, float]]] = []
    if show_legend and series_meta:
        row: list[tuple[str, str, float]] = []
        used_w = 0.0
        for idx, meta in enumerate(series_meta):
            text = _legend_label(meta)
            colour = meta.get("color") or palette[idx % len(palette)]
            # Measured, not 4.4pt per character: a guess that runs short
            # overlaps the next entry and one that runs long wraps a row
            # that would have fitted.
            w = 12 + stringWidth(text, "Helvetica", 8) + 14
            if row and used_w + w > chart_w:
                legend_rows.append(row)
                row, used_w = [], 0.0
            row.append((text, colour, w))
            used_w += w
        if row:
            legend_rows.append(row)
    LEGEND_BAND = LEGEND_ROW_H * len(legend_rows)
    # 2026-10-11, requested: "legengs labels to be editables and also
    # aligments to be changed too". The label was already the series' own
    # `label`; the key itself always started hard against the Y axis.
    legend_align = str(section.get("legend_align") or "left").strip().lower()
    if legend_align not in ("left", "center", "centre", "right"):
        legend_align = "left"

    x_title_y = 4
    legend_y = x_title_y + X_TITLE_BAND
    ticks_y = legend_y + LEGEND_BAND
    chart_bottom = ticks_y + TICK_BAND
    # Grow the canvas by whatever the bands need so the plot keeps its size.
    height = 72 * mm + chart_bottom
    drawing = Drawing(page_width, height)
    chart_top = height - 12
    chart_h = max(50, chart_top - chart_bottom)

    left_min, left_max = axis_domain["left1"]
    right_min, right_max = axis_domain["right1"]

    left_min, left_max = axis_domain["left1"]
    right_min, right_max = axis_domain["right1"]

    def _y(v: float, axis: str) -> float:
        a = norm_axis4(axis)
        if a not in used:
            a = "left1"
        lo, hi = axis_domain[a]
        if hi == lo:
            return chart_bottom
        return chart_bottom + (v - lo) / (hi - lo) * chart_h

    def _x(idx: int) -> float:
        if max_len <= 1:
            # Centred, not flush against the Y axis: a single sample drawn at
            # chart_left sits half outside the plot.
            return chart_left + chart_w / 2.0
        return chart_left + (idx / (max_len - 1)) * chart_w

    # Grid + axes background
    drawing.add(Rect(chart_left, chart_bottom, chart_w, chart_h, fillColor=colors.HexColor("#f8fafc"),
                     strokeColor=colors.HexColor("#e2e8f0"), strokeWidth=0.4))
    # Y axis horizontal grid lines - one per tick interval.
    for i in range(1, tick_count):
        y = chart_bottom + (chart_h * i / float(tick_count))
        drawing.add(Line(chart_left, y, chart_right, y,
                         strokeColor=colors.HexColor("#e5e7eb"), strokeWidth=0.3, strokeDashArray=[1, 2]))

    # Limit lines (zone thresholds)
    limit_lines = section.get("limit_lines") or []
    if isinstance(limit_lines, list):
        for ll in limit_lines:
            if not isinstance(ll, dict):
                continue
            try:
                lv = float(ll.get("value"))
            except Exception:
                continue
            laxis = norm_axis4(ll.get("axis"))
            y = _y(lv, laxis)
            if y < chart_bottom - 1 or y > chart_top + 1:
                continue
            line_color = colors.HexColor(str(ll.get("color") or "#dc2626"))
            dash = [3, 3] if ll.get("dash", True) else None
            ln = Line(chart_left, y, chart_right, y, strokeColor=line_color, strokeWidth=0.8)
            if dash:
                ln.strokeDashArray = dash
            drawing.add(ln)
            lbl = str(ll.get("label") or "").strip()
            if lbl:
                drawing.add(String(chart_right - 3, y + 2, lbl,
                                   fontName="Helvetica-Bold", fontSize=7, textAnchor="end", fillColor=line_color))

    chart_kind_default = str(section.get("type") or "line_chart").replace("_chart", "")
    if chart_kind_default not in ("line", "area", "bar"):
        chart_kind_default = "line"

    # 2026-10-11, requested: "thickness of lines in th charts to be
    # changable". It was hardcoded at 1.4pt, which is too fine for a
    # projected report and too heavy for eight overlaid series. The section
    # sets the default; a series can differ (e.g. a thin setpoint under a
    # thick measurement).
    def _line_width(meta: dict[str, Any]) -> float:
        for src in (meta.get("line_width"), section.get("line_width")):
            if src is None or str(src).strip() == "":
                continue
            try:
                return max(0.2, min(6.0, float(src)))
            except (TypeError, ValueError):
                continue
        return 1.4

    # Draw series
    for idx, (meta, pts) in enumerate(zip(series_meta, aligned)):
        color_hex = meta.get("color") or palette[idx % len(palette)]
        try:
            stroke_color = colors.HexColor(color_hex)
        except Exception:
            stroke_color = colors.HexColor(palette[idx % len(palette)])
        axis = norm_axis4(meta.get("axis"))
        kind = meta.get("chart_type") or chart_kind_default
        lw = _line_width(meta)
        # A series may differ: a stepped setpoint over a smoothed measurement.
        interp = norm_interpolation(
            meta.get("interpolation"),
            norm_interpolation(section.get("interpolation")))
        coords = []
        # Iterate over 3-tuples (ts, value, value_text); the chart ignores
        # value_text and skips rows where the numeric value is missing.
        for i, point in enumerate(pts):
            v = point[1]
            if v is None or not math.isfinite(v):
                continue
            coords.append((_x(i), _y(v, axis)))
        if not coords:
            continue
        # 2026-10-11: "we faced some problems when the chart for some reason
        # was not printed in the pdf for not enough data". One of those cases
        # was literal: a series with a SINGLE sample produced no line at all,
        # because the segment loop starts at index 1. A lone reading is still
        # a reading - mark it.
        if len(coords) == 1 and kind != "bar":
            cx, cy = coords[0]
            drawing.add(Circle(cx, cy, max(1.5, lw * 1.6),
                               fillColor=stroke_color, strokeColor=stroke_color,
                               strokeWidth=0))
            continue
        if kind == "bar":
            # Bars are INSET from the axis: drawn flush at chart_left the first
            # bar straddled the Y axis and the last fell outside the plot.
            bar_w = max(1.5, (chart_w * 0.92) / max(1, len(pts) * len(series_meta)))
            for cx, cy in coords:
                # Clamp inside the plot. Centred on its x position, the first
                # bar sat half outside the Y axis and the last overhung the
                # right edge.
                x0 = min(max(cx - bar_w / 2, chart_left + 1),
                         chart_right - bar_w - 1)
                drawing.add(Rect(x0, chart_bottom, bar_w, max(0, cy - chart_bottom),
                                 fillColor=stroke_color, strokeColor=stroke_color, strokeWidth=0))
        else:
            # Interpolate ONCE, so the line and the area fill below cannot
            # disagree about where the curve runs.
            coords = _interpolate(coords, interp)
            # Polyline (manual): draw segment lines.
            if kind == "area":
                # Translucent fill: build polygon by hugging the X axis.
                from reportlab.graphics.shapes import Polygon
                pgon_pts: list[float] = []
                pgon_pts.append(coords[0][0]); pgon_pts.append(chart_bottom)
                for cx, cy in coords:
                    pgon_pts.append(cx); pgon_pts.append(cy)
                pgon_pts.append(coords[-1][0]); pgon_pts.append(chart_bottom)
                fill = colors.HexColor(color_hex)
                # ReportLab doesn't support alpha on Polygon directly; fake by mixing with white.
                try:
                    fill = colors.Color(fill.red * 0.35 + 0.65, fill.green * 0.35 + 0.65, fill.blue * 0.35 + 0.65, 1.0)
                except Exception:
                    pass
                drawing.add(Polygon(pgon_pts, fillColor=fill, strokeColor=None))
            for i in range(1, len(coords)):
                x0, y0 = coords[i - 1]
                x1, y1 = coords[i]
                drawing.add(Line(x0, y0, x1, y1, strokeColor=stroke_color, strokeWidth=lw))

    # Axes tick labels (left + right). _format_tick and fmt are defined
    # with the geometry above, because the tick gutter is measured from the
    # labels this will print.
    # One scale per drawn axis. The unit rides on the axis TITLE, not on the
    # ticks: appending it produced labels like "31.66C" where a number should
    # be.
    for axis, geom in axis_x.items():
        lo, hi = axis_domain[axis]
        anchor = "end" if geom["anchor"] < 0 else "start"
        for i in range(0, tick_count + 1):
            y = chart_bottom + (chart_h * i / float(tick_count))
            drawing.add(String(geom["tick"], y - 2,
                               _format_tick(lo + (hi - lo) * i / float(tick_count), fmt),
                               fontName="Helvetica", fontSize=7, textAnchor=anchor,
                               fillColor=colors.HexColor("#64748b")))
    # X axis a couple of timestamps
    if max_len >= 2:
        # Pick first and last timestamps from the series with the most points
        ref_pts = max(aligned, key=lambda p: len(p)) if aligned else []
        if ref_pts:
            xs = ref_pts[0][0]
            xe = ref_pts[-1][0]
            drawing.add(String(chart_left, ticks_y, xs[:19],
                               fontName="Helvetica", fontSize=7, textAnchor="start", fillColor=colors.HexColor("#64748b")))
            drawing.add(String(chart_right, ticks_y, xe[:19],
                               fontName="Helvetica", fontSize=7, textAnchor="end", fillColor=colors.HexColor("#64748b")))
    # Axis titles (resolved with their units further up)
    if x_title:
        drawing.add(String((chart_left + chart_right) / 2, x_title_y, x_title,
                           fontName="Helvetica-Bold", fontSize=8, textAnchor="middle", fillColor=colors.HexColor("#334155")))
    # ReportLab's String shape does NOT carry a transform attribute, so the
    # rotated axis title must live inside a Group. Older builds set
    # title_drawing.transform on the String directly, which raised
    # "illegal attribute 'transform' in class String" and made the WHOLE
    # chart section render as a "[error rendering section: ...]" string in
    # the PDF — the live preview was unaffected because it uses recharts.
    def _add_rotated_label(text: str, cx: float, cy: float) -> None:
        if not text:
            return
        label = String(0, 0, text,
                       fontName="Helvetica-Bold", fontSize=8,
                       textAnchor="middle", fillColor=colors.HexColor("#334155"))
        # transform matrix (a, b, c, d, e, f) where (a, b, c, d) is the
        # rotation/scale and (e, f) is the translation. (0, 1, -1, 0, x, y)
        # rotates 90 degrees CCW and places the origin at (x, y).
        group = Group(label, transform=(0, 1, -1, 0, cx, cy))
        drawing.add(group)

    for axis, geom in axis_x.items():
        if axis_title[axis]:
            _add_rotated_label(axis_title[axis], geom["title"],
                               (chart_top + chart_bottom) / 2)

    # Legend - rows were laid out before the canvas was sized, so every
    # series has a key entry.
    for r_idx, row in enumerate(legend_rows):
        row_w = sum(w for _t, _c, w in row)
        if legend_align == "right":
            lx = chart_right - row_w
        elif legend_align in ("center", "centre"):
            lx = chart_left + max(0.0, (chart_w - row_w) / 2.0)
        else:
            lx = chart_left
        ly = legend_y + (len(legend_rows) - 1 - r_idx) * LEGEND_ROW_H
        for text, colour, w in row:
            try:
                box_color = colors.HexColor(colour)
            except Exception:
                box_color = colors.HexColor("#64748b")
            drawing.add(Rect(lx, ly + 1, 8, 8, fillColor=box_color, strokeColor=box_color))
            drawing.add(String(lx + 12, ly + 2, text, fontName="Helvetica",
                               fontSize=8, fillColor=colors.HexColor("#334155")))
            lx += w

    block.append(drawing)
    total_samples = sum(len(p) for p in aligned)
    n_series = len([s for s in series_meta if s.get("tag_name")])
    # 2026-10-11, asked: "I have two differen section that are using the same
    # main schedule report trigger but are loggin different number os samples
    # why?" - they were not. Both had ten readings per series; one had three
    # series and the other two, and this line reported the TOTAL, so the same
    # data read as "30 samples" beside "20 samples". Say per series.
    per = [len(p) for p in aligned if p]
    if per and min(per) == max(per):
        caption = (f"{per[0]} samples per series, {n_series} series "
                   f"({total_samples} readings)")
    else:
        caption = (f"{total_samples} readings across {n_series} series "
                   f"({min(per) if per else 0}-{max(per) if per else 0} each)")
    if total_samples < 3:
        caption += " - too few readings for a meaningful trend"
    if section.get("_trigger_fell_back"):
        # Set by apply_section_triggers when the section's own trigger never
        # became true: the chart then covers the SCHEDULE's window, and saying
        # so is the difference between a trusted report and a wrong one.
        caption += " (section trigger did not fire; showing the report window)"
    block.append(Paragraph(caption, styles["section_caption"]))
    _emit_block(section, block, story)
    story.append(Spacer(1, 4 * mm))


def _section_pie(section: dict[str, Any], styles, story: list) -> None:
    title = str(section.get("title") or "").strip()
    if title:
        story.append(Paragraph(title, styles["section_title"]))
    data = _fetch_pie_data(section)
    if not data:
        story.append(Paragraph("No pie data available.", styles["section_caption"]))
        return
    # Filter out negative/NaN, keep zero rows so the legend stays stable.
    cleaned = [(lbl, max(0.0, val), color) for (lbl, val, color) in data if math.isfinite(val)]
    total = sum(v for _, v, _ in cleaned)
    page_width = _avail_width(section)
    height = 75 * mm
    drawing = Drawing(page_width, height)
    pie = Pie()
    pie.x = 20
    pie.y = 8
    pie.width = height - 20
    pie.height = height - 20
    # Recharts-style: zero-total fallback so the donut still renders.
    if total <= 0:
        pie.data = [1] * max(1, len(cleaned))
    else:
        pie.data = [v if v > 0 else 0.0001 for _, v, _ in cleaned]
    pie.labels = [lbl[:18] for lbl, _, _ in cleaned]
    pie.slices.strokeColor = colors.white
    pie.slices.strokeWidth = 1
    for idx, (_, _, color_hex) in enumerate(cleaned):
        try:
            pie.slices[idx].fillColor = colors.HexColor(color_hex)
        except Exception:
            pie.slices[idx].fillColor = colors.HexColor(_default_palette()[idx % 8])
    drawing.add(pie)

    legend = Legend()
    legend.x = height + 10
    legend.y = height - 12
    legend.deltay = 12
    legend.fontSize = 9
    legend.colorNamePairs = [
        (
            colors.HexColor(c) if str(c).startswith("#") else colors.HexColor(_default_palette()[i % 8]),
            f"{lbl}  ({v:.2f})",
        )
        for i, (lbl, v, c) in enumerate(cleaned)
    ]
    drawing.add(legend)
    story.append(drawing)
    story.append(Paragraph(f"Total: {total:.2f}", styles["section_caption"]))
    story.append(Spacer(1, 4 * mm))


def build_data_table_rows(section: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    """Public alias used by HTTP CSV/TXT exporters."""
    return _build_data_table_rows(section)


def build_chart_section_rows(section: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    """Render a chart section as a tabular preview (timestamp + per-series cols).

    Used so scheduled reports can attach the chart data as a CSV/TXT companion
    file. Returns `(header, body_rows)`. The first column is always the
    timestamp from the longest series; subsequent columns are one per series.
    """
    series_meta, aligned = _fetch_multi_series(section)
    if not series_meta or all(not pts for pts in aligned):
        return [], []
    # Two leading columns: human-friendly local time + the raw UTC. Tools
    # downstream (Excel, BI imports) can keep using ts_utc as the canonical
    # timeline while operators reading the file see their wall-clock time.
    header = ["Timestamp (local)", "Timestamp (UTC)"]
    for s in series_meta:
        label = (s.get("label") or s.get("tag_name") or "Value").strip()
        unit = (s.get("unit") or "").strip()
        header.append(f"{label} [{unit}]" if unit else label)
    longest = max(range(len(aligned)), key=lambda i: len(aligned[i]))
    timeline = aligned[longest]
    body: list[list[Any]] = []
    for i in range(len(timeline)):
        ts_utc_raw = str(timeline[i][0] or "")
        ts_local = _utc_str_to_local_iso(ts_utc_raw)[:23]
        row: list[Any] = [ts_local, ts_utc_raw[:23]]
        for j, _meta in enumerate(series_meta):
            pts = aligned[j]
            if i < len(pts):
                v = pts[i][1]
                row.append("-" if v is None else f"{float(v):.3f}")
            else:
                row.append("-")
        body.append(row)
    return header, body


def build_template_render_data(template: dict[str, Any]) -> dict[str, Any]:
    """Render a saved template to a JSON structure suitable for an HTML
    preview in the dashboard. Walks the same sections the PDF renderer
    walks, but instead of emitting reportlab flowables it emits plain
    dicts the React layer can render directly.

    Section shape (one per template section):
      { type: "header" | "text" | "kpi_grid" | "line_chart" | "area_chart"
              | "bar_chart" | "pie_chart" | "table" | "image" | "spacer"
              | "page_break",
        title: str, subtitle: str (header only),
        ...type-specific keys (rows, items, slices, series, body, ...) }
    """
    definition = template.get("definition") if isinstance(template, dict) else None
    if not isinstance(definition, dict):
        definition = {}
    sections = definition.get("sections")
    if not isinstance(sections, list):
        sections = []
    out_sections: list[dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        stype = str(section.get("type") or "").strip().lower()
        title = str(section.get("title") or "").strip()
        try:
            if stype == "header":
                out_sections.append({
                    "type": "header",
                    "title": title or str(template.get("name") or "Report"),
                    "subtitle": str(section.get("subtitle") or "").strip(),
                    "show_generated_at": section.get("show_generated_at") is not False,
                })
            elif stype == "text":
                out_sections.append({
                    "type": "text",
                    "title": title,
                    "text": str(section.get("text") or section.get("html") or ""),
                })
            elif stype == "kpi_grid":
                items = section.get("items") or []
                rendered = []
                # A KPI grid carries a time_range like every other section.
                kpi_from, kpi_to = _resolve_time_range(section.get("time_range"))
                for it in items if isinstance(items, list) else []:
                    if not isinstance(it, dict):
                        continue
                    value, count, unit = _fetch_kpi_value(it, kpi_from, kpi_to)
                    rendered.append({
                        "label": str(it.get("label") or it.get("tag_name") or "—"),
                        "value": None if value is None else float(value),
                        "unit": str(unit or ""),
                        "sample_count": int(count or 0),
                        "aggregation": str(it.get("aggregation") or "avg"),
                    })
                cols = section.get("columns")
                try:
                    cols_n = int(cols) if cols is not None else 4
                except Exception:
                    cols_n = 4
                out_sections.append({
                    "type": "kpi_grid",
                    "title": title,
                    "columns": max(1, min(6, cols_n)),
                    "items": rendered,
                })
            elif stype in ("line_chart", "area_chart", "bar_chart"):
                series_meta, aligned = _fetch_multi_series(section)
                series = []
                for i, meta in enumerate(series_meta):
                    pts = aligned[i] if i < len(aligned) else []
                    series.append({
                        "label": str(meta.get("label") or meta.get("tag_name") or f"Series {i + 1}"),
                        "unit": str(meta.get("unit") or ""),
                        "color": str(meta.get("color") or ""),
                        # Compact point list: [ts_utc, value]. Cap to 500
                        # samples per series so the JSON payload stays
                        # reasonable when an operator picks a broad range.
                        "points": [
                            [str(p[0] or ""), (None if p[1] is None else float(p[1]))]
                            for p in pts[-500:]
                        ],
                    })
                out_sections.append({
                    "type": stype,
                    "title": title,
                    "series": series,
                })
            elif stype == "pie_chart":
                slices = _fetch_pie_data(section)
                out_sections.append({
                    "type": "pie_chart",
                    "title": title,
                    "slices": [
                        {"label": str(s[0] or ""), "value": float(s[1] or 0), "color": str(s[2] or "")}
                        for s in slices
                    ],
                })
            elif stype == "table":
                header, body = _build_data_table_rows(section)
                out_sections.append({
                    "type": "table",
                    "title": title,
                    "header": [str(h) for h in header],
                    "rows": [[("" if c is None else str(c)) for c in r] for r in body[:200]],
                    "row_count": len(body),
                })
            elif stype == "segment_summary":
                # Rows are the tests/cycles the run produced. The segments are
                # attached to the section by apply_window_override; a manual
                # render (no trigger, no segments) has nothing to summarise and
                # renders an empty table rather than a misleading one.
                s_header, s_rows = build_segment_summary(
                    section, section.get("_segments") or [])
                out_sections.append({
                    "type": "table",
                    "title": title,
                    "header": [str(h) for h in s_header],
                    "rows": [[("" if c is None else str(c)) for c in r] for r in s_rows[:500]],
                    "row_count": len(s_rows),
                })
            elif stype == "static_table":
                # A table whose header + rows are supplied literally by the
                # caller (KPIs, pass/fail, limits) — no historian query. Renders
                # identically to a data table.
                s_header = [str(h) for h in (section.get("header") or [])]
                s_rows = [[("" if c is None else str(c)) for c in r]
                          for r in (section.get("rows") or [])[:500]]
                out_sections.append({
                    "type": "table",
                    "title": title,
                    "header": s_header,
                    "rows": s_rows,
                    "row_count": len(s_rows),
                })
            elif stype == "image":
                # Image sections carry a data URL or a relative path. The
                # data URL transports cleanly through the JSON envelope;
                # path-based images can't be embedded here (we'd need to
                # serve them from the backend) so we surface just the
                # filename and let the operator know.
                data_url = str(section.get("data_url") or "").strip()
                path = str(section.get("path") or "").strip()
                out_sections.append({
                    "type": "image",
                    "title": title,
                    "caption": str(section.get("caption") or ""),
                    "data_url": data_url if data_url.startswith("data:") else "",
                    "path": path,
                    "align": str(section.get("align") or "center"),
                    "width_mm": section.get("width_mm") or 0,
                })
            elif stype == "spacer":
                out_sections.append({"type": "spacer", "height": section.get("height") or 8})
            elif stype == "page_break":
                out_sections.append({"type": "page_break"})
            else:
                out_sections.append({"type": "unknown", "raw_type": stype, "title": title})
        except Exception as exc:
            out_sections.append({
                "type": "error",
                "title": title,
                "section_type": stype,
                "error": str(exc),
            })
    return {
        "name": str(template.get("name") or "Report"),
        "description": str(template.get("description") or ""),
        "sections": out_sections,
    }


def build_template_dataset_files(template: dict[str, Any], output_dir: Path, base_name: str) -> dict[str, Path]:
    """Write CSV and TXT companion files for a template's data sections.

    Walks every `table`, `line_chart`, `area_chart`, `bar_chart` section in
    declaration order and concatenates each one's rows into a single CSV (and
    a pipe-delimited TXT) under `output_dir`. The two files share the base
    name so emails can attach them side-by-side with the PDF.

    Returns a dict like `{"csv": Path, "txt": Path}` containing only the
    formats that had data; missing keys mean no data section produced output.
    """
    import csv
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    definition = template.get("definition") if isinstance(template, dict) else None
    if not isinstance(definition, dict):
        definition = {}
    sections = definition.get("sections")
    if not isinstance(sections, list):
        sections = []

    blocks: list[tuple[str, list[str], list[list[Any]]]] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        stype = str(section.get("type") or "").strip().lower()
        title = str(section.get("title") or stype or "data").strip()
        try:
            if stype == "table":
                header, body = build_data_table_rows(section)
            elif stype == "static_table":
                header = [str(h) for h in (section.get("header") or [])]
                body = [list(r) for r in (section.get("rows") or [])]
            elif stype in {"line_chart", "area_chart", "bar_chart"}:
                header, body = build_chart_section_rows(section)
            else:
                continue
        except Exception:
            continue
        if header and body:
            blocks.append((title, header, body))

    out: dict[str, Path] = {}
    if not blocks:
        return out

    # CSV: each block separated by a "# <title>" marker row so a single file
    # can carry several sections without fighting the spreadsheet importers.
    csv_path = output_dir / f"{base_name}.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        for i, (title, header, body) in enumerate(blocks):
            if i > 0:
                writer.writerow([])  # blank line separates sections
            writer.writerow([f"# {title}"])
            writer.writerow(header)
            for row in body:
                writer.writerow(row)
    out["csv"] = csv_path

    # TXT: pipe-delimited mirror, friendlier for plain-text viewers.
    txt_path = output_dir / f"{base_name}.txt"
    with txt_path.open("w", encoding="utf-8") as f:
        for i, (title, header, body) in enumerate(blocks):
            if i > 0:
                f.write("\n")
            f.write(f"# {title}\n")
            f.write(" | ".join(str(h) for h in header) + "\n")
            for row in body:
                f.write(" | ".join(str(c) for c in row) + "\n")
    out["txt"] = txt_path

    return out


def _build_data_table_rows(section: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    """Compute the visible rows for a data-table section.

    Supports multi-series so multiple tags share one timestamp column. Each
    column descriptor in section.columns is one of:
      {key:"ts"}                                            -> timestamp
      {key:"tag", series_id?:"s0"}                          -> tag name
      {key:"gateway", series_id?:"s0"}                      -> gateway id
      {key:"quality", series_id?:"s0"}                      -> quality label
      {key:"value", series_id:"s0", title?, format?, unit?} -> raw value
      {key:"calc",  series_ids:["s0","s1"], expr:"a-b",      -> computed
                    title, format?, unit?}

    `expr` supports the variables `a, b, c, ...` mapped to series_ids in order,
    plus the usual arithmetic operators and python's `math` namespace exposed
    as `math.*`. Anything else is rejected and rendered as `-`.
    """
    series_meta, aligned = _fetch_multi_series(section, default_limit=int(section.get("row_limit") or 30))
    if not series_meta or all(not pts for pts in aligned):
        return [], []
    row_limit = max(1, min(int(section.get("row_limit") or 30), 500))
    # Choose an index dimension: longest series (treated as the reference timeline).
    longest_idx = max(range(len(aligned)), key=lambda i: len(aligned[i]))
    timeline = aligned[longest_idx][-row_limit:]
    series_by_id = {s["id"]: i for i, s in enumerate(series_meta)}
    # Trim every series to the same final N samples for alignment.
    trimmed = [pts[-row_limit:] if pts else [] for pts in aligned]
    # Default columns: timestamp + one value column per series.
    columns = section.get("columns")
    if not isinstance(columns, list) or not columns:
        columns = [{"key": "ts", "title": "Timestamp"}]
        for s in series_meta:
            columns.append({
                "key": "value",
                "series_id": s["id"],
                "title": s.get("label") or s.get("tag_name") or "Value",
                "format": "3dp",
                "unit": s.get("unit"),
            })

    header = [str(c.get("title") or c.get("key") or "") for c in columns]

    def _fmt(v: Any, preset: str | None, unit: str | None) -> str:
        if v is None:
            return "-"
        try:
            n = float(v)
        except Exception:
            return str(v)
        p = (preset or "3dp").lower()
        if p == "int":
            out = f"{int(round(n))}"
        elif p == "2dp":
            out = f"{n:.2f}"
        elif p == "3dp":
            out = f"{n:.3f}"
        elif p == "scientific":
            out = f"{n:.2e}"
        else:
            out = f"{n:.3f}"
        if unit:
            out = f"{out} {unit}"
        return out

    import math as _math
    safe_builtins = {"abs": abs, "min": min, "max": max, "round": round}

    def _eval_calc(expr: str, var_values: dict[str, float | None]) -> float | None:
        if not expr:
            return None
        # Reject obviously dangerous tokens.
        bad = ("__", "import", "open", "exec", "eval", "lambda", "compile", ";", "\n")
        if any(tok in expr for tok in bad):
            return None
        clean_vars = {k: v for k, v in var_values.items() if v is not None and math.isfinite(v)}
        if len(clean_vars) != len(var_values):
            return None  # any missing input -> dash
        try:
            return float(eval(expr, {"__builtins__": safe_builtins, "math": _math}, clean_vars))
        except Exception:
            return None

    body: list[list[Any]] = []
    for i in range(len(timeline)):
        cells: list[Any] = []
        for col in columns:
            key = str(col.get("key") or "").lower()
            if key == "ts":
                ts_val = timeline[i][0]
                # Reports should show the operator's local time. We still
                # accept ts_utc as the source of truth in the historian.
                local = _utc_str_to_local_iso(str(ts_val or ""))
                cells.append(local[:23] if local else str(ts_val or "")[:23])
                continue
            if key in ("tag", "gateway", "quality"):
                # The original historian row isn't carried into the aligned
                # tuples, so we re-derive from the referenced series metadata.
                sid = str(col.get("series_id") or "")
                idx = series_by_id.get(sid, longest_idx)
                meta = series_meta[idx]
                if key == "tag":
                    cells.append(str(meta.get("tag_name") or ""))
                elif key == "gateway":
                    cells.append(str(meta.get("gateway_id") or ""))
                else:
                    cells.append("-")
                continue
            if key == "value":
                sid = str(col.get("series_id") or "")
                idx = series_by_id.get(sid, longest_idx)
                pts = trimmed[idx] if idx < len(trimmed) else []
                if i >= len(pts):
                    cells.append("-")
                    continue
                # Prefer the text value when present (string-typed tag).
                point = pts[i]
                text_val = point[2] if len(point) > 2 else None
                if text_val:
                    cells.append(str(text_val))
                else:
                    cells.append(_fmt(point[1], col.get("format"), col.get("unit")))
                continue
            if key == "calc":
                expr = str(col.get("expr") or "")
                ids = col.get("series_ids") or []
                if not isinstance(ids, list):
                    ids = []
                var_values: dict[str, float | None] = {}
                for var_idx, sid in enumerate(ids):
                    var_name = chr(ord("a") + var_idx)
                    s_idx = series_by_id.get(str(sid), -1)
                    if s_idx < 0 or s_idx >= len(trimmed):
                        var_values[var_name] = None
                        continue
                    pts = trimmed[s_idx]
                    var_values[var_name] = pts[i][1] if i < len(pts) else None
                result = _eval_calc(expr, var_values)
                cells.append(_fmt(result, col.get("format"), col.get("unit")))
                continue
            # Unknown column key
            cells.append("-")
        body.append(cells)
    return header, body


def _section_table(section: dict[str, Any], styles, story: list) -> None:
    header, body = _build_data_table_rows(section)
    _draw_table(section, header, body, styles, story)


def _section_static_table(section: dict[str, Any], styles, story: list) -> None:
    """A table whose header and rows are given literally.

    2026-09-30: static_table was understood by the JSON payload and the CSV
    export but NOT by the PDF story, so an asset-identity or checklist section
    rendered as "Unknown section type" in the actual report. It is the section
    type a test report leans on hardest.
    """
    header = [str(h) for h in (section.get("header") or [])]
    body = [[("" if c is None else str(c)) for c in r]
            for r in (section.get("rows") or [])[:500]]
    _draw_table(section, header, body, styles, story)


def _section_segment_summary(section: dict[str, Any], styles, story: list) -> None:
    """One row per test/cycle/phase, with an optional aggregate footer."""
    header, body = build_segment_summary(section, section.get("_segments") or [])
    _draw_table(section, header, body, styles, story,
                empty="No test segments were recorded for this run.")


#: Operator-facing alignment names -> reportlab's.
_ALIGNMENTS = {"left": "LEFT", "centre": "CENTER", "center": "CENTER",
               "right": "RIGHT"}


def _flatten_cell(items: list) -> list:
    """Unwrap grouping flowables so a table cell can measure its content.

    KeepTogether (and anything else deriving from reportlab's _ContainerSpace)
    reports an effectively infinite height inside a cell, and reportlab then
    refuses the entire table with "tallest row 16777230". The row container is
    what keeps a column together, so the wrappers are redundant here.
    Recursive, because a container can hold another.
    """
    out: list = []
    for item in items or []:
        inner = getattr(item, "_content", None)
        if isinstance(inner, (list, tuple)):
            out.extend(_flatten_cell(list(inner)))
        else:
            out.append(item)
    return out


def _emit_block(section: dict[str, Any], block: list, story: list) -> None:
    """Append a section's flowables, glued unless we are inside a column.

    KeepTogether measures as infinitely tall inside a table cell and reportlab
    rejects the whole table, so a section drawn into a `row` column emits its
    parts plainly - the row itself is what keeps them together.
    """
    if section.get("_in_column"):
        story.extend(block)
    else:
        story.append(KeepTogether(block))


def _section_row(section: dict[str, Any], styles, story: list,
                 draw_one=None) -> None:
    """Two (or more) sections side by side on one line.

    2026-10-01, requested: "add option to add two sections in the same row
    (chart, chart) (chart, table), (KPI, data)". It is also the honest answer
    to the white space: a chart that will not fit the rest of a page pushes a
    whole page break, and half-width charts simply fit more often.

    Each child is rendered into its own story and the results are placed in
    one borderless table, so every existing section type works in a column
    without knowing it is in one.
    """
    children = [c for c in (section.get("columns") or []) if isinstance(c, dict)]
    if not children:
        return
    n = min(len(children), 3)
    children = children[:n]
    gap = 6
    width = (_FULL_WIDTH - gap * (n - 1)) / n

    title = str(section.get("title") or "").strip()
    block: list[Any] = []
    if title:
        block.append(Paragraph(title, styles["section_title"]))

    cells: list[Any] = []
    for child in children:
        sub: list[Any] = []
        try:
            draw_one(dict(child, _width=width, _in_column=True), styles, sub)
        except Exception as exc:                       # noqa: BLE001
            sub = [Paragraph("[error in column: %s]" % exc, styles["section_caption"])]
        cells.append(_flatten_cell(sub) or [Spacer(1, 1)])

    table = Table([cells], colWidths=[width] * n)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    block.append(table)
    story.append(KeepTogether(block))
    story.append(Spacer(1, 3 * mm))


def _draw_table(section: dict[str, Any], header: list, body: list, styles,
                story: list, empty: str = "No rows for the selected range.") -> None:
    """Draw a table, keeping its heading attached to it.

    `align` sets every cell; `align_columns` is a per-column list that wins
    where it is given, so a label column can stay left while its numbers are
    right-aligned.
    """
    block: list[Any] = []
    title = str(section.get("title") or "").strip()
    if title:
        block.append(Paragraph(title, styles["section_title"]))
    if not header or not body:
        block.append(Paragraph(empty, styles["section_caption"]))
        _emit_block(section, block, story)
        return
    page_width = _avail_width(section)
    n = max(1, len(header))
    col_widths = [page_width / n] * n

    # 2026-10-09, requested: "we should also be able to format the text in
    # tables and card to be centralized, to be a pill inside of the table
    # color formated, font size and color too".
    #
    # Styled PER COLUMN rather than per cell: a check list has one column of
    # labels and one of results, and the operator wants "every result is a
    # green pill", not to colour forty cells by hand. `column_styles` is a
    # list positioned by column; anything missing leaves that column alone.
    col_styles = section.get("column_styles")
    col_styles = col_styles if isinstance(col_styles, list) else []

    def _col_style(c):
        return col_styles[c] if c < len(col_styles) and isinstance(col_styles[c], dict) else {}

    try:
        _cell_pt = float(section.get("table_font_size") or 0) or 8.5
    except (TypeError, ValueError):
        _cell_pt = 8.5

    shaped_body = []
    for row in body:
        out_row = []
        for c, cell in enumerate(row if isinstance(row, list) else [row]):
            cs = _col_style(c)
            # A dict cell styles ITSELF and wins over the column - that is
            # how an API-built template marks one result red among greens.
            if isinstance(cell, dict):
                text = str(cell.get("text") or "")
                cs = {**cs, **{k: v for k, v in cell.items() if k != "text"}}
            else:
                text = "" if cell is None else str(cell)
            if cs.get("pill"):
                out_row.append(pill_band(
                    text, styles,
                    background=cs.get("background") or "#14a89a",
                    color=cs.get("color") or "#ffffff",
                    corners=cs.get("corners") or "both",
                    radius=cs.get("radius"),
                    align=cs.get("align") or "center",
                    font_size=cs.get("font_size") or _cell_pt,
                    bold=bool(cs.get("bold", True)),
                    width=0,
                ))
            else:
                out_row.append(text)
        shaped_body.append(out_row)

    rows = [header] + shaped_body
    table = Table(rows, colWidths=col_widths, repeatRows=1)
    # 2026-10-09, requested: "borders for tables". These were fixed at a
    # hairline grid in one grey. A printed test report often wants a heavier
    # outline, horizontal rules only, or no lines at all, and a density that
    # matches the rest of the document.
    #   table_borders : all (default) | outline | rows | columns | none
    #   border_color  : #rrggbb      border_width : points
    #   table_font_size, zebra
    border_mode = str(section.get("table_borders") or "all").strip().lower()
    rule = _hex_colour(section.get("border_color"), RULE_COLOR)
    try:
        bw = float(section.get("border_width") or 0) or None
    except (TypeError, ValueError):
        bw = None
    outer_w = bw if bw is not None else 0.3
    inner_w = bw if bw is not None else 0.2
    try:
        tfs = float(section.get("table_font_size") or 0) or 8.5
    except (TypeError, ValueError):
        tfs = 8.5
    tfs = max(5.0, min(24.0, tfs))

    cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), TABLE_HEAD_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), tfs),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 1), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
    ]
    # Zebra striping is a border-ish choice: with no rules at all it is what
    # keeps a wide row readable, so it stays on unless turned off.
    if section.get("zebra") is not False:
        cmds.append(("ROWBACKGROUNDS", (0, 1), (-1, -1),
                     [colors.white, TABLE_ALT_BG]))
    if border_mode in ("all", "outline"):
        cmds.append(("BOX", (0, 0), (-1, -1), outer_w, rule))
    if border_mode == "all":
        cmds.append(("INNERGRID", (0, 0), (-1, -1), inner_w, rule))
    elif border_mode == "rows":
        cmds.append(("LINEBELOW", (0, 0), (-1, -2), inner_w, rule))
    elif border_mode == "columns":
        cmds.append(("LINEAFTER", (0, 0), (-2, -1), inner_w, rule))
    style = TableStyle(cmds)
    # Column-level text colour and alignment for the cells that are NOT
    # pills (a pill carries its own).
    for c in range(n):
        cs = _col_style(c)
        if cs.get("pill"):
            continue
        a = _ALIGNMENTS.get(str(cs.get("align") or "").strip().lower())
        if a:
            style.add("ALIGN", (c, 1), (c, -1), a)
        ink = _hex_colour(cs.get("color"), None)
        if ink is not None:
            style.add("TEXTCOLOR", (c, 1), (c, -1), ink)
        if cs.get("bold"):
            style.add("FONTNAME", (c, 1), (c, -1), "Helvetica-Bold")
    # A colour for the whole body, applied before the per-column ones above
    # would be overridden by them - so this is the default, not the winner.
    _body_ink = _hex_colour(section.get("table_font_color"), None)
    if _body_ink is not None:
        style.add("TEXTCOLOR", (0, 1), (-1, -1), _body_ink)

    whole = _ALIGNMENTS.get(str(section.get("align") or "").strip().lower())
    if whole:
        style.add("ALIGN", (0, 1), (-1, -1), whole)
    per_col = section.get("align_columns")
    if isinstance(per_col, list):
        for i, want in enumerate(per_col):
            name = _ALIGNMENTS.get(str(want or "").strip().lower())
            if name and i < len(header):
                style.add("ALIGN", (i, 1), (i, -1), name)
    table.setStyle(style)
    block.append(table)
    # A table longer than a page cannot be kept whole - repeatRows already
    # carries its header onto each page, so only short ones are glued.
    if len(rows) <= 25:
        _emit_block(section, block, story)
    else:
        story.extend(block)
    story.append(Spacer(1, 4 * mm))


# --------------------------------------------------------------------------- #
# top-level render
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# per-section triggers
#
# 2026-09-30, requested: "for each section of my report I want to be able to
# pick different start and end trigger where the data inside of the section
# would be loaded, I want to be able to select from the gateways tags".
#
# A segment needs the scheduler to have been watching while the run happened.
# A section TRIGGER does not: it names two tag conditions and the window is
# found by reading back what those tags actually did, inside whatever window
# encloses the section. That also makes it work on a manual re-render of an
# old run.
# --------------------------------------------------------------------------- #
def _cond_true(value: Any, operator: str, v1: Any, v2: Any = None) -> bool:
    """Evaluate one condition against a historian reading."""
    op = str(operator or "==").strip().lower()
    if value is None:
        return False
    # A text tag (a phase name, a recipe step) compares as text for equality.
    if op in {"==", "=", "eq", "!=", "<>", "ne"}:
        same = str(value).strip().lower() == str(v1).strip().lower()
        try:
            same = float(value) == float(v1)
        except (TypeError, ValueError):
            pass
        return same if op in {"==", "=", "eq"} else not same
    try:
        num = float(value)
        t1 = float(v1)
    except (TypeError, ValueError):
        return False
    if op in {">", "gt"}:
        return num > t1
    if op in {">=", "gte"}:
        return num >= t1
    if op in {"<", "lt"}:
        return num < t1
    if op in {"<=", "lte"}:
        return num <= t1
    if op == "between":
        try:
            lo, hi = sorted([t1, float(v2)])
        except (TypeError, ValueError):
            return False
        return lo <= num <= hi
    return False


#: A window no reading can fall inside. Used where a trigger or a segment did
#: not happen - "" would read as "no filter" and show everything.
_NEVER = "9999-01-01 00:00:00"


def _row_ts(row: dict[str, Any]) -> str:
    """The timestamp, whichever spelling this fetcher uses.

    get_historian_rows_range returns "ts"; other paths carry "ts_utc". Reading
    only one of them silently matched nothing.
    """
    return str(row.get("ts") or row.get("ts_utc") or "")


def _reading_value(row: dict[str, Any]) -> Any:
    v = row.get("value")
    return row.get("value_text") if v is None else v


def _widen(ts: str, seconds: int) -> str:
    """Shift a 'YYYY-MM-DD HH:MM:SS...' stamp by whole seconds."""
    try:
        base = datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(ts or "")
    return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


def _trigger_rows(cond: dict[str, Any], from_utc: str, to_utc: str) -> list[dict[str, Any]]:
    """Readings for one condition's tag, oldest first.

    The range is widened by a second at each end and trimmed again in
    _within(). The store normalises a range filter to 'SS.fff', and a stored
    stamp written WITHOUT milliseconds then sorts BEFORE it - so the reading
    exactly on the boundary was dropped. For a trigger that reading is the
    whole point: a cycle-start pulse sits precisely at the window's start.
    """
    rows = _app_store().get_historian_rows_range(
        from_utc=_widen(from_utc, -1) if from_utc else from_utc,
        to_utc=_widen(to_utc, 1) if to_utc else to_utc,
        limit=5000, offset=0,
        gateway=str(cond.get("gateway_id") or ""),
        tag=str(cond.get("tag_name") or ""),
        prefer_cloud_reads=False,
    )
    return list(reversed(rows or []))


def _within(ts: str, lo: str, hi: str) -> bool:
    """Is this reading inside the window? Compared to the SECOND.

    The widened fetch above can return a neighbour; comparing on seconds also
    sidesteps the millisecond mismatch that caused the widening.
    """
    t = str(ts or "")[:19]
    if lo and t < str(lo)[:19]:
        return False
    if hi and t > str(hi)[:19]:
        return False
    return True


def _trigger_conditions(trigger: dict[str, Any], side: str) -> tuple[list[dict], str]:
    """The conditions for one side of a trigger, and their logic.

    2026-10-11, requested: "make sure the section trigger also consider
    multiples conditions". Until then each side was a SINGLE dict, so a
    section could not say "while Cycle_Active = 1 AND Phase = Heating" - it
    had to pick one and chart the wrong window.

    Both shapes are accepted, because every template already saved carries
    the single-dict form and must keep rendering identically:

      {"start": {...}}                                   (one condition)
      {"start_conditions": [{...}, {...}], "start_logic": "all"}

    Logic is "all" (every condition true at the same moment) or "any".
    """
    many = trigger.get("%s_conditions" % side)
    conds: list[dict] = []
    if isinstance(many, list):
        conds = [c for c in many if isinstance(c, dict)
                 and str(c.get("tag_name") or "").strip()]
    if not conds:
        one = trigger.get(side)
        if isinstance(one, dict) and str(one.get("tag_name") or "").strip():
            conds = [one]
    logic = str(trigger.get("%s_logic" % side) or "all").strip().lower()
    return conds, ("any" if logic == "any" else "all")


def _cond_timeline(conds: list[dict], from_utc: str, to_utc: str,
                   fetch) -> list[tuple[str, list[Any]]]:
    """Every reading of every condition's tag, merged onto one clock.

    Each entry is (timestamp, [value per condition]) carrying the LAST KNOWN
    value of each tag at that moment - sample-and-hold, which is how a PLC bit
    behaves between scans. Without the hold, "all" could never be satisfied:
    two tags are almost never written in the same millisecond, so at any one
    timestamp only one of them has a fresh reading.
    """
    per_cond: list[list[tuple[str, Any]]] = []
    stamps: set[str] = set()
    for c in conds:
        rows = [r for r in (fetch(c, from_utc, to_utc) or [])
                if _within(_row_ts(r), from_utc, to_utc)]
        seq = [(_row_ts(r), _reading_value(r)) for r in rows if _row_ts(r)]
        seq.sort(key=lambda kv: kv[0])
        per_cond.append(seq)
        stamps.update(ts for ts, _v in seq)
    if not stamps:
        return []
    cursors = [0] * len(per_cond)
    held: list[Any] = [None] * len(per_cond)
    out: list[tuple[str, list[Any]]] = []
    for ts in sorted(stamps):
        for k, seq in enumerate(per_cond):
            while cursors[k] < len(seq) and seq[cursors[k]][0] <= ts:
                held[k] = seq[cursors[k]][1]
                cursors[k] += 1
        out.append((ts, list(held)))
    return out


def _logic_true(conds: list[dict], values: list[Any], logic: str) -> bool:
    """Is the condition set satisfied by this set of held values?"""
    results = [_cond_true(v, c.get("operator"), c.get("value"), c.get("value2"))
               for c, v in zip(conds, values)]
    if not results:
        return False
    return all(results) if logic == "all" else any(results)


def resolve_trigger_window(trigger: dict[str, Any], outer_from: str, outer_to: str,
                           fetch=None) -> tuple[str, str]:
    """The window between a section's start and stop conditions.

    The START is the first moment inside the enclosing window at which the
    start condition set is satisfied. The STOP is the first moment after that
    at which the stop set is satisfied; with no stop set the window closes
    when the start set stops being true, which is what an operator means by
    "while it is heating".

    Either side may carry SEVERAL conditions combined with all/any - see
    _trigger_conditions.

    When the start never fires:

      2026-10-11, requested: "the data will be collected within the triggers
      the main schedule report start and end trigger when the report section
      do no clear do not use different trigger". So the section falls back to
      the window that ENCLOSES it - the schedule's own triggered window - and
      the caller marks the section so the report says so. It does not invent a
      different range, and it does not silently print nothing.

      Pass trigger["on_miss"] = "empty" for the older behaviour (an impossible
      window, so the section renders as "no data"), which is right for a
      section that exists only to document one optional phase.
    """
    fetch = fetch or _trigger_rows
    start_conds, start_logic = _trigger_conditions(trigger, "start")
    if not start_conds:
        return outer_from, outer_to
    stop_conds, stop_logic = _trigger_conditions(trigger, "stop")

    timeline = _cond_timeline(start_conds, outer_from, outer_to, fetch)
    t0 = ""
    for ts, values in timeline:
        if _logic_true(start_conds, values, start_logic):
            t0 = ts
            break
    if not t0:
        if str(trigger.get("on_miss") or "window").strip().lower() == "empty":
            return _NEVER, _NEVER
        return outer_from, outer_to

    if stop_conds:
        for ts, values in _cond_timeline(stop_conds, t0, outer_to, fetch):
            if ts <= t0:
                continue
            if _logic_true(stop_conds, values, stop_logic):
                return t0, ts
        # Still running when the report was made: close at the outer end, so
        # the readings taken since t0 are never dropped.
        return t0, outer_to

    for ts, values in timeline:
        if ts <= t0:
            continue
        if not _logic_true(start_conds, values, start_logic):
            return t0, ts
    return t0, outer_to


# --------------------------------------------------------------------------- #
# segments - the sub-periods a triggered run was divided into
#
# 2026-09-30, requested: "a test report that runs two different types of test,
# the second repeated based on the number of cycles defined in the process".
# The scheduler watches ONE tag while the cycle is open (e.g. Test_Number or
# Cycle_Number); every time its value changes it closes the current segment
# and opens the next. A segment is just {name, index, from_utc, to_utc}, so
# everything downstream is the window machinery that already existed.
# --------------------------------------------------------------------------- #
def _segment_matches(seg: dict[str, Any], name: str) -> bool:
    """Does this segment answer to `name`?

    A phase is identified by the VALUE its tag held - "Heating", "Cooling" -
    which is how an operator names it and how a recipe writes it. The first
    version compared only the stream's name, so a section asking for "Heating"
    matched nothing at all; that is the "no options when selecting a different
    trigger based on a tag value" reported on 2026-09-30. Value, stream and
    label all answer, so any of them can be typed or picked.
    """
    if not name:
        return True
    want = name.strip().lower()
    for key in ("value", "stream", "name", "label"):
        if str(seg.get(key) or "").strip().lower() == want:
            return True
    return False


def select_segments(segments: list[dict[str, Any]], name: str = "",
                    index: Any = None, stream: str = "") -> list[dict[str, Any]]:
    """The segments a section refers to.

    `stream` narrows to one level of the run - "test", "cycle", "phase" - and
    `name` picks a value within it. A TCU test needs all three at once: the
    heating phase of the third cycle of the final test.

    `index` is 1-based (an operator counts cycles from one), or "last"/"first",
    or None for every matching segment.
    """
    pool = [sg for sg in (segments or []) if isinstance(sg, dict)
            and _segment_matches(sg, name)
            and (not stream
                 or str(sg.get("stream") or sg.get("name") or "").strip().lower()
                 == stream.strip().lower())]
    if index is None or str(index).strip() == "":
        return pool
    key = str(index).strip().lower()
    if key == "last":
        return pool[-1:]
    if key == "first":
        return pool[:1]
    try:
        i = int(key)
    except Exception:
        return pool
    if i < 1 or i > len(pool):
        return []
    return [pool[i - 1]]


def _segment_row_label(seg: dict[str, Any], pool: list[dict[str, Any]]) -> str:
    """How a segment names itself in a table or a repeated section title.

    The occurrence number is only added when the VALUE repeats. Cycle 1, 2, 3
    are already distinct, so "1 1" was noise; Heating happens once per cycle,
    so "Heating 1" and "Heating 2" are what an operator needs to tell apart.
    """
    label = str(seg.get("label") or seg.get("value") or seg.get("name") or "")
    value = str(seg.get("value") or "")
    repeats = sum(1 for other in pool if str(other.get("value") or "") == value)
    idx = seg.get("index")
    if repeats > 1 and idx:
        return ("%s %s" % (label, idx)).strip()
    return label


def _bind_segment_range(section: dict[str, Any],
                        segments: list[dict[str, Any]]) -> dict[str, Any]:
    """Turn {preset:'segment', ...} into the concrete window it names.

    Resolved HERE rather than in _resolve_time_range because a segment only
    exists for the duration of a run - the fetchers deeper down take a plain
    (from, to) and must stay that way.
    """
    tr = section.get("time_range")
    if not isinstance(tr, dict):
        return section
    if str(tr.get("preset") or "").strip().lower() != "segment":
        return section
    picked = select_segments(segments, str(tr.get("segment") or ""),
                             tr.get("index"), str(tr.get("stream") or ""))
    if not picked:
        # Naming a segment the run never produced must not silently widen to
        # "all time" - that reads as a real measurement. Give it an empty
        # window so the section renders as "no data".
        return {**section, "time_range": {"preset": "custom",
                                          "from_utc": _NEVER, "to_utc": _NEVER}}
    return {**section, "time_range": {"preset": "custom",
                                      "from_utc": str(picked[0].get("from_utc") or ""),
                                      "to_utc": str(picked[0].get("to_utc") or "")}}


def _expand_for_segments(section: dict[str, Any],
                         segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One section becomes N - one per segment - when it asks to repeat.

    `repeat_for: {segment: "test2"}` on any section type. The expansion happens
    before anything is fetched or drawn, so charts, tables and KPI grids all
    gain repetition without a single change to the render path.
    """
    rep = section.get("repeat_for")
    if not isinstance(rep, dict):
        return [section]
    picked = select_segments(segments, str(rep.get("segment") or ""), None,
                             str(rep.get("stream") or ""))
    if not picked:
        return []
    out: list[dict[str, Any]] = []
    base_title = str(section.get("title") or "").strip()
    for sg in picked:
        suffix = _segment_row_label(sg, picked)
        copy = dict(section)
        copy.pop("repeat_for", None)
        copy["title"] = ("%s - %s" % (base_title, suffix)).strip(" -") if base_title else suffix
        copy["time_range"] = {"preset": "custom",
                              "from_utc": str(sg.get("from_utc") or ""),
                              "to_utc": str(sg.get("to_utc") or "")}
        out.append(copy)
    return out


def _fmt_number(v: Any, preset: str | None, unit: str | None) -> str:
    """Format a number the way the data table does (int/2dp/3dp/scientific)."""
    if v is None:
        return "-"
    try:
        n = float(v)
    except Exception:
        return str(v)
    p = (preset or "3dp").lower()
    if p == "int":
        out = "%d" % int(round(n))
    elif p == "2dp":
        out = "%.2f" % n
    elif p == "scientific":
        out = "%.2e" % n
    else:
        out = "%.3f" % n
    return ("%s %s" % (out, unit)) if unit else out


_FOOTER_AGGS = {"avg", "min", "max", "sum", "count", "median", "last", "first"}


def _reduce(values: list[float], how: str) -> float | None:
    """Aggregate a column of per-segment results into one number."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    how = (how or "avg").strip().lower()
    if how == "min":
        return min(vals)
    if how == "max":
        return max(vals)
    if how == "sum":
        return sum(vals)
    if how == "count":
        return float(len(vals))
    if how == "first":
        return vals[0]
    if how == "last":
        return vals[-1]
    if how == "median":
        ordered = sorted(vals)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2.0
    return sum(vals) / float(len(vals))


def build_segment_summary(section: dict[str, Any],
                          segments: list[dict[str, Any]]) -> tuple[list[str], list[list[Any]]]:
    """One row per segment, one column per configured tag, optional footer.

    2026-09-30, requested: "a KPI or a table that for example can show the
    average, min or max or any other aggregation ... for each tag defined, in
    each different test, or the main/final results in each test then calculate
    average".

    So: rows are the tests/cycles, columns are (tag, aggregation) pairs, and
    the footer applies a SECOND aggregation down each column - the average of
    the per-test results.

    Returns (header, rows) in the shape `static_table` already renders, so no
    new drawing code exists anywhere.
    """
    columns = [c for c in (section.get("columns") or []) if isinstance(c, dict)]
    picked = select_segments(segments, str(section.get("segment") or ""), None,
                             str(section.get("stream") or ""))
    header = [str(section.get("segment_header") or "Test")]
    for c in columns:
        header.append(str(c.get("title")
                          or "%s (%s)" % (c.get("tag_name") or "?",
                                          c.get("aggregation") or "avg")))
    rows: list[list[Any]] = []
    per_column: list[list[float]] = [[] for _ in columns]
    fmt = str(section.get("format") or "3dp")
    for sg in picked:
        row: list[Any] = [_segment_row_label(sg, picked)]
        for i, c in enumerate(columns):
            value, _count, text = _fetch_kpi_value(
                {
                    "label": c.get("title") or "",
                    "gateway_id": c.get("gateway_id") or section.get("gateway_id") or "",
                    "tag_name": c.get("tag_name") or "",
                    "operator": c.get("operator") or "any",
                    "value1": c.get("value1"),
                    "value2": c.get("value2"),
                    "aggregation": c.get("aggregation") or "avg",
                    "ramp_from": c.get("ramp_from"),
                    "ramp_to": c.get("ramp_to"),
                },
                str(sg.get("from_utc") or ""), str(sg.get("to_utc") or ""),
            )
            if value is None:
                row.append(text if text else "-")
            else:
                row.append(_fmt_number(float(value), fmt, c.get("unit")))
                per_column[i].append(float(value))
        rows.append(row)

    footer = str(section.get("footer_aggregation") or "").strip().lower()
    if footer in _FOOTER_AGGS and rows:
        foot: list[Any] = [str(section.get("footer_label") or footer.upper())]
        for i, c in enumerate(columns):
            v = _reduce(per_column[i], footer)
            foot.append("-" if v is None else _fmt_number(v, fmt, c.get("unit")))
        rows.append(foot)
    return header, rows


#: Presets that describe a window RELATIVE to "now". In a triggered run they
#: are meaningless — a cycle that ended at 09:12 must not show "the last 24
#: hours" — so the run's own window replaces them. Presets that name an
#: explicit window (custom dates, a batch, a segment) are deliberate and are
#: left exactly as the operator set them.
_RELATIVE_PRESETS = {"", "none", "run", "5m", "15m", "1h", "6h", "24h", "7d", "30d"}


def section_uses_run_window(section: dict[str, Any]) -> bool:
    """True when this section should be pinned to the run's window."""
    tr = section.get("time_range")
    if not isinstance(tr, dict):
        return True          # no range at all -> the run's window
    return str(tr.get("preset") or "").strip().lower() in _RELATIVE_PRESETS


#: {tag:Name}, {avg:Name}, {max:Name}, {min:Name}, {last:Name} - a value read
#: from the historian and dropped into text.
_TOKEN_RE = re.compile(r"\{(tag|last|avg|max|min|first):([^}]+)\}")

# A BARE {Name} is shorthand for {tag:Name}. 2026-10-07: the operator set a
# file name of "{R1_Work_Order_string}" and got a file called exactly that -
# the prefixed form is what the resolver matched, and anything else passed
# through untouched. Writing the tag name on its own is the obvious thing to
# try, so it now works.
_BARE_TOKEN_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_./-]*(?:\|[^}]*)?)\}")
# ...except these, which are filled in later by _fill_name_placeholders from
# the run itself. Consuming them here as "a tag called template" would break
# every existing pattern.
_RESERVED_TOKENS = frozenset({
    "template", "id", "start", "end", "date", "time", "initial",
})


class _BareMatch:
    """Minimal match-like object so a bare {Name} can reuse the {tag:Name} path."""

    __slots__ = ("_kind", "_ref")

    def __init__(self, kind: str, ref: str) -> None:
        self._kind, self._ref = kind, ref

    def group(self, n: int) -> str:
        return self._kind if n == 1 else self._ref


def resolve_text_tokens(text: str, from_utc: str, to_utc: str,
                        gateway: str = "", fetch=None) -> str:
    """Replace {tag:Name} style tokens with values from the window.

    2026-10-01, requested: "even when we want to add some text on the report
    they also can be linked from a tag value". A part number, an operator name
    or a measured result can then be written inline:

        Part number: {tag:Work_Order}   peak: {max:TCU_Temp} C

    A token that resolves to nothing becomes "-" rather than vanishing, so a
    missing reading is visible instead of silently reading as prose.
    `Gateway/Tag` picks a specific gateway; a bare name matches on the tag.
    """
    raw = str(text or "")
    if "{" not in raw:
        return raw
    fetch = fetch or _trigger_rows

    def _one(match):
        kind = match.group(1).lower()
        ref = match.group(2).strip()
        # 2026-10-09, requested: "we should also have the text mask
        # condition to any text for all sections". A mask rides on the
        # token itself - {R1_Pass|1=PASS,0=FAIL} - so it works anywhere a
        # token works: a heading, a sentence, a table cell, a pill, a file
        # name. The structured value_map on pills and KPI cells stays; this
        # is the same idea for free text.
        mask_txt = ""
        if "|" in ref:
            ref, _, mask_txt = ref.partition("|")
            ref = ref.strip()
        mask = parse_value_map(mask_txt) if mask_txt else {}
        gw, _, tag = ref.rpartition("/")
        cond = {"gateway_id": (gw or gateway).strip(), "tag_name": tag.strip()}
        try:
            rows = fetch(cond, from_utc, to_utc) or []
        except Exception:
            return "-"
        values = []
        texts = []
        for r in rows:
            v = r.get("value")
            if v is None:
                t = r.get("value_text")
                if t not in (None, ""):
                    texts.append(str(t))
                continue
            try:
                values.append(float(v))
            except (TypeError, ValueError):
                continue
        def _masked(num, txt):
            """The mask wins when it matches; otherwise the raw reading."""
            if mask:
                hit = apply_value_map(mask, num, txt)
                if hit is not None:
                    return hit
            if num is not None:
                return _fmt_number(num, "3dp", None)
            return txt if txt not in (None, "") else "-"

        if kind in ("tag", "last"):
            if values:
                return _masked(values[-1], texts[-1] if texts else None)
            return _masked(None, texts[-1] if texts else None)
        if kind == "first":
            if values:
                return _masked(values[0], texts[0] if texts else None)
            return _masked(None, texts[0] if texts else None)
        if not values:
            return _masked(None, None)
        if kind == "avg":
            return _masked(sum(values) / len(values), None)
        if kind == "max":
            return _masked(max(values), None)
        return _masked(min(values), None)

    out = _TOKEN_RE.sub(_one, raw)

    def _bare(match):
        name = match.group(1)
        # Only the NAME decides whether this is a reserved run token; a
        # mask after the pipe never changes that.
        bare_name = name.split("|", 1)[0].strip()
        if bare_name.lower() in _RESERVED_TOKENS:
            return match.group(0)        # left for _fill_name_placeholders
        # Re-use the prefixed path so a bare name behaves identically to
        # {tag:Name} - same gateway rule, same "-" for a missing reading.
        return _one(_TOKEN_RE.fullmatch("{tag:%s}" % name)
                    or _BareMatch("tag", name))

    return _BARE_TOKEN_RE.sub(_bare, out)


def fill_run_tokens(text: str, from_utc: str = "", to_utc: str = "",
                    template_name: str = "", report_id: str = "") -> str:
    """{start} {end} {date} {time} {template} {id} inside SECTION TEXT.

    2026-10-09, reported with a screenshot: a report subtitle read
    "Work order - - {start} to {end}" - the braces printed literally.

    These names are deliberately RESERVED in resolve_text_tokens so a file
    name can keep them until the run is finished and
    _fill_name_placeholders can substitute them. Section text has no such
    later pass, so they simply survived onto the page. They are filled here
    instead, in the READABLE form - _fill_name_placeholders strips the
    colons out of a timestamp because a file name cannot contain them, and
    "2026-10-09 07:54:15" is what belongs in a sentence.
    """
    out = str(text or "")
    if "{" not in out:
        return out
    started = str(from_utc or "").replace("T", " ")
    ended = str(to_utc or "").replace("T", " ")
    stamp = started or ended
    for key, value in (("{start}", started),
                       ("{end}", ended),
                       ("{date}", stamp[:10]),
                       ("{time}", stamp[11:19]),
                       ("{template}", str(template_name or "")),
                       ("{id}", str(report_id or ""))):
        if key in out:
            out = out.replace(key, value or "-")
    return out


def apply_text_tokens(template: dict[str, Any], fetch=None) -> dict[str, Any]:
    """Resolve {tag:...} tokens in the text an operator typed.

    Runs after the windows are pinned, so a token inside a section that is
    bound to one phase reads THAT phase's values.
    """
    out = dict(template or {})
    _tpl_name = str(out.get("name") or "")
    _tpl_id = str(out.get("id") or "")
    definition = dict(out.get("definition") or {})
    sections = definition.get("sections")
    if not isinstance(sections, list):
        return template

    def _one(sec):
        if not isinstance(sec, dict):
            return sec
        if str(sec.get("type") or "").strip().lower() == "row":
            return {**sec, "columns": [_one(c) for c in (sec.get("columns") or [])]}
        f, t = _resolve_time_range(sec.get("time_range"))
        gw = str(sec.get("gateway_id") or "")
        copy = dict(sec)
        # 2026-10-11, requested: "we should be able to add the axis lables
        # for different Y axis and X axis tht also cab be a tag value". The
        # four Y titles, their units and the X title were the one group of
        # operator-typed strings this pass skipped, so "{tag:Unit_Name}" on
        # an axis printed literally.
        _axis_fields = tuple(
            k for a in AXES4 for k in (_AXIS_KEYS[a]["label"], _AXIS_KEYS[a]["unit"])
        ) + ("x_axis_label",)
        for field in ("title", "subtitle", "text") + _axis_fields:
            if isinstance(copy.get(field), str) and "{" in copy[field]:
                copy[field] = fill_run_tokens(
                    resolve_text_tokens(copy[field], f, t, gw, fetch),
                    f, t, _tpl_name, _tpl_id)
        items = copy.get("items")
        if isinstance(items, list):
            copy["items"] = [
                {**it, "label": resolve_text_tokens(it.get("label"), f, t, gw, fetch)}
                if isinstance(it, dict) and isinstance(it.get("label"), str)
                and "{" in it["label"] else it
                for it in items
            ]
        rows = copy.get("rows")
        if isinstance(rows, list):
            # A static table is a list of literal cells, and the operator
            # wants any of them to be able to carry a reading: "Serial
            # number | {Serial_Tag}" beside "Operator | J. Smith". Same
            # token vocabulary as the text sections, so there is one thing
            # to learn (2026-10-09).
            def _cell(c):
                if isinstance(c, str):
                    return fill_run_tokens(
                        resolve_text_tokens(c, f, t, gw, fetch),
                        f, t, _tpl_name, _tpl_id)
                # A styled cell carries its text under "text"; it must get
                # the same tokens and masks as a plain one, or a pill in a
                # table would be the one place they did not work.
                if isinstance(c, dict) and isinstance(c.get("text"), str):
                    return {**c, "text": fill_run_tokens(
                        resolve_text_tokens(c["text"], f, t, gw, fetch),
                        f, t, _tpl_name, _tpl_id)}
                return c

            copy["rows"] = [
                [_cell(c) for c in row] if isinstance(row, list) else row
                for row in rows
            ]
        header = copy.get("header")
        if isinstance(header, list):
            copy["header"] = [
                fill_run_tokens(resolve_text_tokens(c, f, t, gw, fetch),
                                f, t, _tpl_name, _tpl_id)
                if isinstance(c, str) else c for c in header
            ]
        return copy

    definition["sections"] = [_one(sec) for sec in sections]
    out["definition"] = definition
    return out


def apply_section_triggers(template: dict[str, Any], fetch=None) -> dict[str, Any]:
    """Resolve every section's own start/stop trigger into a concrete window.

    Runs on EVERY render, not only a cycle one. 2026-09-30: "all of this
    should be able to be reused when using schedule reports" - so a daily
    schedule can hold a chart that covers only the heating phase inside the
    last 24 hours. The section's existing range is what ENCLOSES the search:
    for a cycle run it has already been pinned to the run, for a scheduled run
    it is whatever the section asks for (24h, a batch, a segment).
    """
    out = dict(template or {})
    definition = dict(out.get("definition") or {})
    sections = definition.get("sections")
    if not isinstance(sections, list):
        return template
    def _one(sec):
        if not isinstance(sec, dict):
            return sec
        # A row holds sections; its children carry triggers of their own and
        # were skipped entirely until 2026-10-01, so two charts sharing a line
        # both rendered the whole run.
        if str(sec.get("type") or "").strip().lower() == "row":
            return {**sec, "columns": [_one(c) for c in (sec.get("columns") or [])]}
        if not isinstance(sec.get("trigger"), dict):
            return sec
        outer_from, outer_to = _resolve_time_range(sec.get("time_range"))
        try:
            f, t = resolve_trigger_window(sec["trigger"], outer_from, outer_to, fetch=fetch)
        except Exception:
            # A trigger that cannot be evaluated must not take the section's
            # data with it - fall back to the enclosing window.
            f, t = outer_from, outer_to
        # 2026-10-11: a section whose trigger did not fire now covers the
        # enclosing (schedule) window rather than rendering empty. That is
        # only honest if the section SAYS the window is not its own.
        fell_back = bool(_trigger_conditions(sec["trigger"], "start")[0]
                         and f == outer_from and t == outer_to)
        return {**sec, "_trigger_fell_back": fell_back,
                "time_range": {"preset": "custom", "from_utc": f, "to_utc": t}}

    definition["sections"] = [_one(sec) for sec in sections]
    out["definition"] = definition
    return out


def apply_window_override(template: dict[str, Any], from_utc: str, to_utc: str,
                          segments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A copy of `template` pinned to the run's window, and expanded per segment.

    2026-09-29, requested: a report generated when a cycle ends must cover
    "the start and end time of this trigger". A template's sections each carry
    their own range (24h, custom, a batch), which is right for a scheduled
    summary and wrong for a cycle: the report would fire at the end of the
    cycle and then show the last 24 hours.

    2026-09-30, extended: "the entire report would have a start and end
    trigger, then I would have charts, telemetry, KPI and other that can use a
    different start and end time". So this no longer flattens EVERY section.
    A section anchored to explicit dates, a batch or a segment keeps its own
    window; only the relative ones (and sections with no range) follow the run.

    `segments` are the sub-periods the run was divided into - one per value of
    the schedule's segment tag, e.g. each cycle of a repeated test. They
    resolve {preset:"segment"} ranges and drive `repeat_for`.
    """
    out = dict(template or {})
    definition = dict(out.get("definition") or {})
    sections = definition.get("sections")
    if isinstance(sections, list):
        window = {"preset": "custom", "from_utc": from_utc, "to_utc": to_utc}
        expanded: list[Any] = []
        for sec in sections:
            if not isinstance(sec, dict):
                expanded.append(sec)
                continue
            if str(sec.get("type") or "").strip().lower() == "row":
                inner = []
                for child in (sec.get("columns") or []):
                    if not isinstance(child, dict):
                        continue
                    if section_uses_run_window(child):
                        inner.append({**child, "time_range": dict(window)})
                    else:
                        inner.append(_bind_segment_range(child, segments or []))
                expanded.append({**sec, "columns": inner})
                continue
            for copy in _expand_for_segments(sec, segments or []):
                if str(copy.get("type") or "").strip().lower() == "segment_summary":
                    # Carries the run's segments so it can build a row each.
                    expanded.append({**copy, "_segments": list(segments or [])})
                    continue
                if section_uses_run_window(copy):
                    copy = {**copy, "time_range": dict(window)}
                else:
                    copy = _bind_segment_range(copy, segments or [])
                expanded.append(copy)
        definition["sections"] = expanded
    out["definition"] = definition
    return out


# The FOUR Y axes a chart can carry, in draw order outwards from the plot.
# Same model and the same config key names the dashboard widgets already use
# (y_axis_*/y_min for left1, y_axis_right_*/y_right_* for right1, y_left2_*
# and y_right2_* for the outer pair), so a chart configured on a dashboard
# reads the same way in a report.
#
# 2026-10-08, requested: "on the reports templates when we are creating the
# charts we still need the mutple Y axis as we had for the dashboard wdigets
# to be print more than 2 unis of readings". Report charts had exactly two,
# and anything that was not "right" was forced onto the left - so a third
# unit (bar, °C, l/min) silently shared someone else's scale and looked
# wrong rather than missing.
AXES4 = ("left1", "left2", "right1", "right2")
_AXIS_KEYS = {
    "left1":  {"label": "y_axis_label",       "unit": "y_axis_unit",
               "min": "y_min",                "max": "y_max"},
    "left2":  {"label": "y_left2_label",      "unit": "y_left2_unit",
               "min": "y_left2_min",          "max": "y_left2_max"},
    "right1": {"label": "y_axis_right_label", "unit": "y_axis_right_unit",
               "min": "y_right_min",          "max": "y_right_max"},
    "right2": {"label": "y_right2_label",     "unit": "y_right2_unit",
               "min": "y_right2_min",         "max": "y_right2_max"},
}


def norm_axis4(value: Any) -> str:
    """One of AXES4. Legacy configs stored only "left"/"right"."""
    a = str(value or "left1").strip().lower()
    if a == "left2":
        return "left2"
    if a == "right2":
        return "right2"
    if a in ("right", "right1"):
        return "right1"
    return "left1"


def _fill_name_placeholders(text: str, template_name: str, report_id: str,
                            started: str, ended: str) -> str:
    """Replace {template} {id} {start} {end} {date} {time} in a name or path."""
    stamp = (started or ended or "").replace("T", " ")
    for key, value in (("{template}", template_name), ("{id}", report_id),
                       ("{start}", (started or "").replace(":", "").replace(" ", "_")),
                       ("{end}", (ended or "").replace(":", "").replace(" ", "_")),
                       ("{date}", stamp[:10].replace("-", "")),
                       ("{time}", stamp[11:19].replace(":", ""))):
        text = text.replace(key, str(value or ""))
    return text


def _safe_name_part(text: str, extension: str = ".pdf") -> str:
    """One path SEGMENT, safe to write.

    A file name is not a path: anything that could redirect the write - a
    separator, a parent reference - is stripped rather than trusted. Used for
    the last segment only, so a configured directory is left intact.
    """
    out = re.sub(r"[^A-Za-z0-9._-]+", "_", str(text or "")).strip("._") or "report"
    if not out.lower().endswith(extension):
        out += extension
    return out[:140]


def resolve_output_path(pattern: str, *, template_name: str = "",
                        report_id: str = "", started: str = "", ended: str = "",
                        extension: str = ".pdf", gateway: str = "",
                        fetch=None) -> str:
    """A full destination path from an operator's pattern.

    2026-10-01, requested: choose where the file is saved - a local folder or
    a synced OneDrive folder - with a customisable path such as

        C:/{tag:Work_Order}/{initial}/{tag:Work_Order}.pdf

    A synced folder is just a local path, so nothing special is needed for
    OneDrive. {tag:...} tokens are read from the run's own window, so a report
    can file itself under its work order.

    Only the FILE NAME is sanitised; the directories are the operator's to
    choose. Returns "" when no pattern is set, which keeps the default
    reports folder.
    """
    text = str(pattern or "").strip()
    if not text:
        return ""
    text = resolve_text_tokens(text, started, ended, gateway, fetch)
    text = _fill_name_placeholders(text, template_name, report_id, started, ended)
    text = text.replace("\\", "/")
    head, _, tail = text.rpartition("/")
    name = _safe_name_part(tail, extension)
    if not head:
        return name
    # Collapse "..", so a pattern resolves to one predictable folder rather
    # than wandering up the tree when a token expands to something unexpected.
    head = os.path.normpath(head).replace("\\", "/")
    return "%s/%s" % (head, name)


def build_report_file_name(pattern: str, *, template_name: str = "",
                           report_id: str = "", started: str = "",
                           ended: str = "", extension: str = ".pdf",
                           gateway: str = "", fetch=None) -> str:
    """Turn a naming pattern into a safe file name.

    Requested 2026-09-29: save the PDF "with custom name, using tag name or
    other special names, automatically". Every report was <uuid>.pdf, which is
    unreadable in a folder of three thousand of them.

    Placeholders: {template} {id} {start} {end} {date} {time}, plus a tag
    value as {TagName} or {tag:TagName}.

    2026-10-07: the tag half was MISSING here. resolve_output_path() resolved
    tags, this did not, so a File name of "{R1_Work_Order_string}" produced a
    file called exactly that - braces and all - while the same token in the
    Save to box worked. Two fields, two different languages, no error either
    way.

    An empty pattern returns "", which keeps the previous uuid behaviour, so
    existing schedules are unaffected.
    """
    text = str(pattern or "").strip()
    if not text:
        return ""
    text = resolve_text_tokens(text, started, ended, gateway, fetch)
    text = _fill_name_placeholders(text, template_name, report_id, started, ended)
    return _safe_name_part(text, extension)


def resolve_output_target(output_path: str, file_pattern: str, *,
                          template_name: str = "", report_id: str = "",
                          started: str = "", ended: str = "",
                          extension: str = ".pdf", gateway: str = "",
                          fetch=None) -> str:
    """Where this report goes, from the operator's TWO settings.

    "Save to" and "File name" are separate boxes in the schedule editor, and
    the obvious reading is "this folder, under this name". The code did not
    do that: any Save to value won outright and the File name was discarded.
    Worse, Save to was then split on its last separator, so a plain folder

        C:/Users/Lucas/OneDrive/Trustnode

    was read as the FILE "Trustnode.pdf" inside OneDrive - every run of every
    schedule overwriting one file (2026-10-07, reported as reports not
    generating properly).

    Now: a Save to that names a file is still a complete destination. A Save
    to that names a FOLDER is joined with the file name - and with a default
    one when no pattern is set, so the folder choice alone is enough.
    """
    folder = str(output_path or "").strip()
    name = build_report_file_name(
        file_pattern, template_name=template_name, report_id=report_id,
        started=started, ended=ended, extension=extension,
        gateway=gateway, fetch=fetch)
    if not folder:
        return name                       # "" keeps the uuid name

    text = resolve_text_tokens(folder, started, ended, gateway, fetch)
    text = _fill_name_placeholders(text, template_name, report_id, started, ended)
    text = text.replace("\\", "/").rstrip()

    last = text.rsplit("/", 1)[-1]
    # A trailing separator, or a last segment with no extension, means the
    # operator pointed at a folder. "report.pdf" means they named the file.
    names_a_file = bool(last) and "." in last and not text.endswith("/")
    if names_a_file:
        return resolve_output_path(
            folder, template_name=template_name, report_id=report_id,
            started=started, ended=ended, extension=extension,
            gateway=gateway, fetch=fetch)

    base = text.rstrip("/")
    if not name:
        # A folder with no name pattern still needs a readable file name;
        # a folder of uuids is what the naming feature existed to fix.
        name = build_report_file_name(
            "{template}_{date}_{time}", template_name=template_name,
            report_id=report_id, started=started, ended=ended,
            extension=extension, gateway=gateway, fetch=fetch)
    return "%s/%s" % (base, name)


def group_side_by_side(sections: list) -> list:
    """Fold sections marked `side_by_side` into `row` containers.

    2026-10-01, requested: an option to put two sections on one line. Doing it
    with a per-section toggle rather than a nested editor keeps the designer
    simple - the operator ticks "place beside the previous section" and the
    renderer pairs them here. An explicit `row` section still works for
    templates written by hand or by the API.
    """
    out: list = []
    for sec in sections or []:
        if not isinstance(sec, dict):
            out.append(sec)
            continue
        beside = bool(sec.get("side_by_side"))
        prev = out[-1] if out else None
        if beside and isinstance(prev, dict):
            if str(prev.get("type") or "").strip().lower() == "row":
                if len(prev.get("columns") or []) < 3:
                    prev["columns"].append(dict(sec, side_by_side=False))
                    continue
            elif str(prev.get("type") or "").strip().lower() not in {
                    "header", "page_break", "spacer"}:
                out[-1] = {"type": "row",
                           "columns": [prev, dict(sec, side_by_side=False)]}
                continue
        out.append(sec)
    return out


def _draw_section(section: dict[str, Any], styles, story: list) -> None:
    """Draw ONE section into `story`.

    Lifted out of the render loop so a `row` can draw its own children
    through exactly the same path - a column must not be a second,
    drifting copy of the dispatch.
    """
    stype = str(section.get("type") or "").strip().lower()
    try:
        if stype == "header":
            _section_header(section, styles, story)
        elif stype == "text":
            _section_text(section, styles, story)
        elif stype == "kpi_grid":
            _section_kpi_grid(section, styles, story)
        elif stype in {"pill_card", "pills"}:
            _section_pill_card(section, styles, story)
        elif stype in {"line_chart", "area_chart", "bar_chart"}:
            _section_chart(section, styles, story)
        elif stype == "pie_chart":
            _section_pie(section, styles, story)
        elif stype == "table":
            _section_table(section, styles, story)
        elif stype == "row":
            _section_row(section, styles, story, draw_one=_draw_section)
        elif stype == "static_table":
            _section_static_table(section, styles, story)
        elif stype == "segment_summary":
            _section_segment_summary(section, styles, story)
        elif stype == "image":
            _section_image(section, styles, story)
        elif stype == "page_break":
            story.append(PageBreak())
        elif stype == "spacer":
            story.append(Spacer(1, max(2, int(section.get("height") or 6)) * mm))
        else:
            story.append(Paragraph(f"Unknown section type: {stype}", styles["section_caption"]))
    except Exception as exc:  # noqa: BLE001
        story.append(Paragraph("[error rendering section: %s]" % exc,
                               styles["section_caption"]))


def render_template_to_pdf(
    template: dict[str, Any],
    *,
    output_path: Path | str | None = None,
    branding_title: str = "TrustNode",
    window: tuple[str, str] | None = None,
    segments: list[dict[str, Any]] | None = None,
) -> tuple[Path, int, str]:
    """Render a template (already-loaded dict, not an id) to PDF on disk.

    Returns `(path, bytes_written, sha256_hex)`. The PDF is named `<uuid>.pdf`
    by default and written under `TRUSTNODE_DATA_DIR/reports/`.
    """
    if window and window[0] and window[1]:
        template = apply_window_override(template, window[0], window[1],
                                         segments=segments)
    # Section triggers resolve on every render - a scheduled report may carry
    # them just as a cycle-triggered one does.
    template = apply_section_triggers(template)
    # Text tokens last: a {max:...} inside a triggered section must read that
    # section's resolved window, not the whole run.
    template = apply_text_tokens(template)
    definition = template.get("definition") if isinstance(template, dict) else None
    if not isinstance(definition, dict):
        definition = {}
    sections = definition.get("sections")
    if not isinstance(sections, list):
        sections = []
    if not sections:
        # Provide a minimal default so empty templates still produce something.
        sections = [
            {"type": "header", "title": template.get("name") or "Report",
             "subtitle": template.get("description") or ""},
            {"type": "text", "text": "This template has no configured sections. "
             "Open the Reporting page and add sections to control the layout."},
        ]

    if output_path is None:
        output_path = _resolve_reports_dir() / f"{uuid.uuid4().hex}.pdf"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    styles = _build_styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        # Top margin clears the logo (~12mm tall starting at 4mm) plus the
        # accent rule that sits below it (~18mm from top), with a few mm of
        # breathing room before the first content block.
        topMargin=24 * mm,
        bottomMargin=15 * mm,
        title=str(template.get("name") or "TrustNode Report"),
        author=branding_title,
    )

    story: list = []
    has_header = False
    sections = group_side_by_side(sections)
    for section in sections:
        if not isinstance(section, dict):
            continue
        if str(section.get("type") or "").strip().lower() == "header":
            has_header = True
        _draw_section(section, styles, story)

    if not has_header:
        # Always lead with a title so the PDF feels finished.
        title = str(template.get("name") or "TrustNode Report").strip() or "TrustNode Report"
        story.insert(0, Spacer(1, 2 * mm))
        story.insert(0, Paragraph(title, styles["title"]))

    def _hdr(canvas, _doc):
        _on_page(canvas, _doc, branding_title=branding_title)

    doc.build(story, onFirstPage=_hdr, onLaterPages=_hdr)

    pdf_bytes = buffer.getvalue()
    buffer.close()
    output_path.write_bytes(pdf_bytes)
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    return output_path, len(pdf_bytes), sha
