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
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
from reportlab.graphics.shapes import Drawing, Line, Rect, String
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


def _resolve_time_range(time_range: Any) -> tuple[str, str]:
    """Return (from_utc, to_utc) text suitable for historian filters."""
    if not isinstance(time_range, dict):
        return "", ""
    preset = str(time_range.get("preset") or "none").strip().lower()
    if preset in {"none", ""}:
        return "", ""
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
    """
    series_list = section.get("series")
    if isinstance(series_list, list) and series_list:
        out = []
        for s in series_list:
            if not isinstance(s, dict):
                continue
            out.append({
                "id": str(s.get("id") or uuid.uuid4().hex[:8]),
                "label": str(s.get("label") or s.get("tag_name") or ""),
                "gateway_id": str(s.get("gateway_id") or section.get("gateway_id") or ""),
                "tag_name": str(s.get("tag_name") or ""),
                "color": str(s.get("color") or "").strip() or None,
                "axis": "right" if str(s.get("axis") or "left").lower() == "right" else "left",
                "chart_type": str(s.get("chart_type") or "").lower() or None,
                "unit": str(s.get("unit") or ""),
                "multiplier": float(s.get("multiplier") if s.get("multiplier") is not None else 1.0),
                "offset": float(s.get("offset") if s.get("offset") is not None else 0.0),
            })
        if out:
            return out
    # Single-series fallback
    return [{
        "id": "s0",
        "label": str(section.get("series_label") or section.get("tag_name") or ""),
        "gateway_id": str(section.get("gateway_id") or ""),
        "tag_name": str(section.get("tag_name") or ""),
        "color": None,
        "axis": "left",
        "chart_type": None,
        "unit": str(section.get("unit") or ""),
        "multiplier": 1.0,
        "offset": 0.0,
    }]


def _fetch_multi_series(section: dict[str, Any], default_limit: int = 240) -> tuple[list[dict[str, Any]], list[list[tuple[str, float | None]]]]:
    """Fetch each series independently then align by index.

    Returns:
      series_meta: list of normalized series dicts (length N)
      aligned: list of N lists; each inner list is [(ts, value), ...] oldest-first
    """
    from_utc, to_utc = _resolve_time_range(section.get("time_range"))
    limit = max(20, min(int(section.get("readings_count") or default_limit), 5000))
    series_meta = _normalize_series_list(section)
    aligned: list[list[tuple[str, float | None]]] = []
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
        points: list[tuple[str, float | None]] = []
        for r in rows:
            try:
                raw = r.get("value")
                val = (float(raw) * mult + off) if raw is not None else None
            except Exception:
                val = None
            points.append((str(r.get("ts") or ""), val))
        aligned.append(points)
    return series_meta, aligned


def _fetch_kpi_value(item: dict[str, Any]) -> tuple[float | None, int]:
    """Return (value, sample_count) for a single KPI cell."""
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
    if not rows:
        return (None, 0)
    row = rows[0] or {}
    v = row.get("value")
    return (float(v) if v is not None else None, int(row.get("count") or 0))


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


def _on_page(canvas, doc, *, branding_title: str = "TrustNode") -> None:
    canvas.saveState()

    # Top-right: brand logo image (same trustnode_logo.png used by the app
    # header). Sized so the bottom of the logo sits just above the accent rule
    # below. Graceful fallback to a text wordmark only when the file is missing.
    logo_path = _resolve_logo_path()
    logo_h_mm = 12.0
    logo_top_mm = 4.0  # distance from page top to top of logo
    drew_logo = False
    if logo_path is not None:
        try:
            from reportlab.lib.utils import ImageReader
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
        value, sample_count = _fetch_kpi_value(item)
        label = str(item.get("label") or item.get("tag_name") or "")
        value_text = "-" if value is None else (f"{value:.2f}" if abs(value) < 1000 else f"{value:.0f}")
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
    left_values: list[float] = []
    right_values: list[float] = []
    max_len = 0
    for meta, pts in zip(series_meta, aligned):
        nums = [v for (_t, v) in pts if v is not None and math.isfinite(v)]
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
        for i, (_t, v) in enumerate(pts):
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
    if y_title:
        title_drawing = String(chart_left - 30, (chart_top + chart_bottom) / 2, y_title,
                               fontName="Helvetica-Bold", fontSize=8, textAnchor="middle", fillColor=colors.HexColor("#334155"))
        title_drawing.transform = (0, 1, -1, 0, title_drawing.x, title_drawing.y)
        drawing.add(title_drawing)
    if y_right_title and has_right_axis:
        title_drawing = String(chart_right + 30, (chart_top + chart_bottom) / 2, y_right_title,
                               fontName="Helvetica-Bold", fontSize=8, textAnchor="middle", fillColor=colors.HexColor("#334155"))
        title_drawing.transform = (0, 1, -1, 0, title_drawing.x, title_drawing.y)
        drawing.add(title_drawing)

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
    header = ["Timestamp"]
    for s in series_meta:
        label = (s.get("label") or s.get("tag_name") or "Value").strip()
        unit = (s.get("unit") or "").strip()
        header.append(f"{label} [{unit}]" if unit else label)
    longest = max(range(len(aligned)), key=lambda i: len(aligned[i]))
    timeline = aligned[longest]
    body: list[list[Any]] = []
    for i in range(len(timeline)):
        row: list[Any] = [str(timeline[i][0] or "")[:23]]
        for j, _meta in enumerate(series_meta):
            pts = aligned[j]
            if i < len(pts):
                v = pts[i][1]
                row.append("-" if v is None else f"{float(v):.3f}")
            else:
                row.append("-")
        body.append(row)
    return header, body


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
                cells.append(str(ts_val or "")[:23])
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
                cells.append(_fmt(pts[i][1], col.get("format"), col.get("unit")))
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
