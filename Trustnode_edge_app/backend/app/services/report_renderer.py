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
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.graphics.shapes import Drawing, Group, Line, Rect, String
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


def _resolve_batch(batch_id: str = "", batch_of_type_id: str = "") -> dict[str, Any] | None:
    """Return a batch dict for an explicit id, or the latest batch of a type."""
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
        "axis": "right" if str(s.get("axis") or "left").lower() == "right" else "left",
        "chart_type": str(s.get("chart_type") or "").lower() or None,
        "unit": str(s.get("unit") or section.get("unit") or ""),
        "multiplier": _safe_float(s.get("multiplier"), 1.0),
        "offset": _safe_float(s.get("offset"), 0.0),
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
        bk_ts = datetime.fromtimestamp(bk, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append((bk_ts, reduced, None))
    return out


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
        rows = _app_store().get_historian_rows_range(
            from_utc=from_utc,
            to_utc=to_utc,
            limit=limit,
            offset=0,
            gateway=gw,
            tag=tag,
            prefer_cloud_reads=False,
        )
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


def _fetch_kpi_value(item: dict[str, Any]) -> tuple[float | None, int, str | None]:
    """Return (value, sample_count, text_value) for a single KPI cell.

    For numeric tags `text_value` is None and `value` holds the aggregate.
    For string-typed tags `value` is None and `text_value` carries the most
    recent text reading; the KPI cell renders the text directly.
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
    rows = _app_store().get_historian_rule_stats(rules=[rule], gateway=rule["gateway_id"], prefer_cloud_reads=False)
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
    if numeric_val is not None:
        return (numeric_val, sample_count, None)
    # Text-tag fallback: look up the most recent text value via the historian
    # range query. If none, return None so the KPI cell renders "-".
    try:
        rows_text = _app_store().get_historian_rows_range(
            gateway=rule["gateway_id"],
            tag=rule["tag_name"],
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
def _section_header(section: dict[str, Any], styles, story: list) -> None:
    title = str(section.get("title") or "Report").strip() or "Report"
    story.append(Paragraph(title, styles["title"]))
    subtitle = str(section.get("subtitle") or "").strip()
    if subtitle:
        story.append(Paragraph(subtitle, styles["subtitle"]))
    if section.get("show_generated_at") is not False:
        story.append(Paragraph(
            datetime.now(timezone.utc).strftime("Generated %Y-%m-%d %H:%M:%S UTC"),
            styles["section_caption"],
        ))
    story.append(Spacer(1, 4 * mm))


def _section_text(section: dict[str, Any], styles, story: list) -> None:
    title = str(section.get("title") or "").strip()
    if title:
        story.append(Paragraph(title, styles["section_title"]))
    text = str(section.get("text") or section.get("html") or "").strip()
    if not text:
        return
    # Treat as plain text; reportlab Paragraph escapes for safety.
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")
    story.append(Paragraph(safe, styles["body"]))
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

    page_width = A4[0] - 30 * mm
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
    cols = max(1, min(int(section.get("columns") or 4), 6))
    for item in items:
        value, sample_count, text_val = _fetch_kpi_value(item)
        label = str(item.get("label") or item.get("tag_name") or "")
        if text_val:
            # Text-typed tag: show the latest string verbatim. Trim long
            # values so they fit a KPI cell — full text stays in the data
            # table sections if needed.
            display = text_val if len(text_val) <= 24 else text_val[:23] + "…"
            value_text = display
        elif value is None:
            value_text = "-"
        else:
            value_text = (f"{value:.2f}" if abs(value) < 1000 else f"{value:.0f}")
        cell = [
            Paragraph(value_text, styles["kpi_value"]),
            Paragraph(label, styles["kpi_label"]),
        ]
        row.append(cell)
        if len(row) == cols:
            cells.append(row)
            row = []
    if row:
        while len(row) < cols:
            row.append("")
        cells.append(row)
    page_width = A4[0] - 30 * mm
    col_w = page_width / cols
    table = Table(cells, colWidths=[col_w] * cols)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("BOX", (0, 0), (-1, -1), 0.4, RULE_COLOR),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, RULE_COLOR),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(table)
    story.append(Spacer(1, 4 * mm))


def _section_chart(section: dict[str, Any], styles, story: list) -> None:
    """Render a multi-series chart with optional dual Y axes and limit lines.

    Section options honored:
      - series[]: one or more {gateway_id, tag_name, label, color, axis,
        chart_type ("line"|"area"|"bar"), unit, multiplier, offset}
      - x_axis_label, y_axis_label, y_axis_unit, y_axis_right_label, y_axis_right_unit
      - y_min, y_max, y_right_min, y_right_max (optional explicit ranges)
      - limit_lines[]: [{value, axis, label, color, dash}]
      - show_legend (default True)
      - value_format ("auto"|"int"|"2dp"|"3dp") for axis tick labels
    """
    title = str(section.get("title") or "").strip()
    if title:
        story.append(Paragraph(title, styles["section_title"]))

    series_meta, aligned = _fetch_multi_series(section)
    if not series_meta or all(not pts for pts in aligned):
        story.append(Paragraph("No data points in selected range.", styles["section_caption"]))
        return

    # Aggregate numeric values per axis for range computation.
    # _fetch_multi_series returns 3-tuples (ts, value, value_text) — the third
    # element carries the original string for text-typed tags. Charts only
    # care about the numeric value column.
    left_values: list[float] = []
    right_values: list[float] = []
    max_len = 0
    for meta, pts in zip(series_meta, aligned):
        nums = [p[1] for p in pts if p[1] is not None and math.isfinite(p[1])]
        max_len = max(max_len, len(pts))
        if meta.get("axis") == "right":
            right_values.extend(nums)
        else:
            left_values.extend(nums)

    if not left_values and not right_values:
        story.append(Paragraph("No numeric values available.", styles["section_caption"]))
        return

    page_width = A4[0] - 30 * mm
    height = 80 * mm
    drawing = Drawing(page_width, height)

    show_legend = section.get("show_legend") is not False
    has_right_axis = bool(right_values) or any(s.get("axis") == "right" for s in series_meta)
    has_left_axis = bool(left_values) or not has_right_axis

    chart_left = 46
    chart_right = page_width - (46 if has_right_axis else 18)
    chart_bottom = 28 if show_legend else 22
    chart_top = height - 12
    chart_w = max(60, chart_right - chart_left)
    chart_h = max(50, chart_top - chart_bottom)

    palette = _default_palette()

    def _domain(values: list[float], explicit_min: Any, explicit_max: Any) -> tuple[float, float]:
        if not values:
            return (0.0, 1.0)
        lo = min(values)
        hi = max(values)
        if explicit_min is not None and str(explicit_min) != "":
            try:
                lo = float(explicit_min)
            except Exception:
                pass
        if explicit_max is not None and str(explicit_max) != "":
            try:
                hi = float(explicit_max)
            except Exception:
                pass
        if hi == lo:
            pad = abs(hi or 1.0) * 0.05 or 0.5
            return (lo - pad, hi + pad)
        span = hi - lo
        pad = max(span * 0.08, 0.25)
        return (lo - pad, hi + pad)

    left_min, left_max = _domain(left_values, section.get("y_min"), section.get("y_max"))
    right_min, right_max = _domain(right_values, section.get("y_right_min"), section.get("y_right_max"))

    def _y(v: float, axis: str) -> float:
        if axis == "right" and has_right_axis:
            lo, hi = right_min, right_max
        else:
            lo, hi = left_min, left_max
        if hi == lo:
            return chart_bottom
        return chart_bottom + (v - lo) / (hi - lo) * chart_h

    def _x(idx: int) -> float:
        if max_len <= 1:
            return chart_left
        return chart_left + (idx / (max_len - 1)) * chart_w

    # Grid + axes background
    drawing.add(Rect(chart_left, chart_bottom, chart_w, chart_h, fillColor=colors.HexColor("#f8fafc"),
                     strokeColor=colors.HexColor("#e2e8f0"), strokeWidth=0.4))
    # Y axis horizontal grid lines (5 ticks).
    for i in range(1, 5):
        y = chart_bottom + (chart_h * i / 5.0)
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
            laxis = "right" if str(ll.get("axis") or "left").lower() == "right" else "left"
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

    # Draw series
    for idx, (meta, pts) in enumerate(zip(series_meta, aligned)):
        color_hex = meta.get("color") or palette[idx % len(palette)]
        try:
            stroke_color = colors.HexColor(color_hex)
        except Exception:
            stroke_color = colors.HexColor(palette[idx % len(palette)])
        axis = "right" if meta.get("axis") == "right" else "left"
        kind = meta.get("chart_type") or chart_kind_default
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
        if kind == "bar":
            bar_w = max(1.5, chart_w / max(1, len(pts) * len(series_meta)))
            for cx, cy in coords:
                drawing.add(Rect(cx - bar_w / 2, chart_bottom, bar_w, max(0, cy - chart_bottom),
                                 fillColor=stroke_color, strokeColor=stroke_color, strokeWidth=0))
        else:
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
                drawing.add(Line(x0, y0, x1, y1, strokeColor=stroke_color, strokeWidth=1.4))

    # Axes tick labels (left + right)
    def _format_tick(v: float, preset: str) -> str:
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

    fmt = str(section.get("value_format") or "auto")
    for i in range(0, 6):
        y = chart_bottom + (chart_h * i / 5.0)
        lv = left_min + (left_max - left_min) * i / 5.0
        unit_l = str(section.get("y_axis_unit") or "")
        drawing.add(String(chart_left - 4, y - 2, _format_tick(lv, fmt) + (unit_l if i == 5 else ""),
                           fontName="Helvetica", fontSize=7, textAnchor="end", fillColor=colors.HexColor("#64748b")))
        if has_right_axis:
            rv = right_min + (right_max - right_min) * i / 5.0
            unit_r = str(section.get("y_axis_right_unit") or "")
            drawing.add(String(chart_right + 4, y - 2, _format_tick(rv, fmt) + (unit_r if i == 5 else ""),
                               fontName="Helvetica", fontSize=7, textAnchor="start", fillColor=colors.HexColor("#64748b")))
    # X axis a couple of timestamps
    if max_len >= 2:
        # Pick first and last timestamps from the series with the most points
        ref_pts = max(aligned, key=lambda p: len(p)) if aligned else []
        if ref_pts:
            xs = ref_pts[0][0]
            xe = ref_pts[-1][0]
            drawing.add(String(chart_left, chart_bottom - 10, xs[-19:] if len(xs) > 19 else xs,
                               fontName="Helvetica", fontSize=7, textAnchor="start", fillColor=colors.HexColor("#64748b")))
            drawing.add(String(chart_right, chart_bottom - 10, xe[-19:] if len(xe) > 19 else xe,
                               fontName="Helvetica", fontSize=7, textAnchor="end", fillColor=colors.HexColor("#64748b")))
    # Axis titles
    x_title = str(section.get("x_axis_label") or "").strip()
    y_title = str(section.get("y_axis_label") or "").strip()
    y_right_title = str(section.get("y_axis_right_label") or "").strip()
    if x_title:
        drawing.add(String((chart_left + chart_right) / 2, 4, x_title,
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

    if y_title:
        _add_rotated_label(y_title, chart_left - 30, (chart_top + chart_bottom) / 2)
    if y_right_title and has_right_axis:
        _add_rotated_label(y_right_title, chart_right + 30, (chart_top + chart_bottom) / 2)

    # Legend
    if show_legend and len(series_meta) > 0:
        lx = chart_left
        ly = chart_bottom - 18
        for idx, meta in enumerate(series_meta):
            color_hex = meta.get("color") or palette[idx % len(palette)]
            try:
                box_color = colors.HexColor(color_hex)
            except Exception:
                box_color = colors.HexColor(palette[idx % len(palette)])
            drawing.add(Rect(lx, ly + 1, 8, 8, fillColor=box_color, strokeColor=box_color))
            label_txt = f"{meta.get('label') or meta.get('tag_name')}"
            if meta.get("axis") == "right":
                label_txt += " (R)"
            unit = (meta.get("unit") or "").strip()
            if unit:
                label_txt += f" [{unit}]"
            drawing.add(String(lx + 12, ly + 2, label_txt[:48],
                               fontName="Helvetica", fontSize=8, fillColor=colors.HexColor("#334155")))
            lx += min(180, max(80, len(label_txt) * 4.5)) + 12
            if lx > chart_right - 60:
                break

    story.append(drawing)
    caption = f"{sum(len(p) for p in aligned)} samples across {len([s for s in series_meta if s.get('tag_name')])} series"
    story.append(Paragraph(caption, styles["section_caption"]))
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
    page_width = A4[0] - 30 * mm
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
                for it in items if isinstance(items, list) else []:
                    if not isinstance(it, dict):
                        continue
                    value, count, unit = _fetch_kpi_value(it)
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
    title = str(section.get("title") or "").strip()
    if title:
        story.append(Paragraph(title, styles["section_title"]))
    header, body = _build_data_table_rows(section)
    if not header or not body:
        story.append(Paragraph("No rows for the selected range.", styles["section_caption"]))
        return
    rows = [header] + body
    page_width = A4[0] - 30 * mm
    n = max(1, len(header))
    col_widths = [page_width / n] * n
    table = Table(rows, colWidths=col_widths, repeatRows=1)
    style = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), TABLE_HEAD_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, TABLE_ALT_BG]),
        ("BOX", (0, 0), (-1, -1), 0.3, RULE_COLOR),
        ("INNERGRID", (0, 0), (-1, -1), 0.2, RULE_COLOR),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 1), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
    ])
    table.setStyle(style)
    story.append(table)
    story.append(Spacer(1, 4 * mm))


# --------------------------------------------------------------------------- #
# top-level render
# --------------------------------------------------------------------------- #
def render_template_to_pdf(
    template: dict[str, Any],
    *,
    output_path: Path | str | None = None,
    branding_title: str = "TrustNode",
) -> tuple[Path, int, str]:
    """Render a template (already-loaded dict, not an id) to PDF on disk.

    Returns `(path, bytes_written, sha256_hex)`. The PDF is named `<uuid>.pdf`
    by default and written under `TRUSTNODE_DATA_DIR/reports/`.
    """
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
    for section in sections:
        if not isinstance(section, dict):
            continue
        stype = str(section.get("type") or "").strip().lower()
        try:
            if stype == "header":
                has_header = True
                _section_header(section, styles, story)
            elif stype == "text":
                _section_text(section, styles, story)
            elif stype == "kpi_grid":
                _section_kpi_grid(section, styles, story)
            elif stype in {"line_chart", "area_chart", "bar_chart"}:
                _section_chart(section, styles, story)
            elif stype == "pie_chart":
                _section_pie(section, styles, story)
            elif stype == "table":
                _section_table(section, styles, story)
            elif stype == "image":
                _section_image(section, styles, story)
            elif stype == "page_break":
                story.append(PageBreak())
            elif stype == "spacer":
                story.append(Spacer(1, max(2, int(section.get("height") or 6)) * mm))
            else:
                story.append(Paragraph(f"Unknown section type: {stype}", styles["section_caption"]))
        except Exception as exc:
            story.append(Paragraph(f"[error rendering section: {exc}]", styles["section_caption"]))

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
