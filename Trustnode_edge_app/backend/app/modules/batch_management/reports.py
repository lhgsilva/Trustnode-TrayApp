"""Single-batch and parent-summary PDF reports for the Batch Management
module. Built on reportlab (same dependency the existing reports use).

Kept intentionally simple — one function per report type, returning raw
PDF bytes. The router wraps them in a Response. No integration with the
scheduled-report system yet; that lives behind a separate ticket since
it involves the per-tenant template store + Lite mirror.
"""
from __future__ import annotations

import io
from datetime import datetime
from typing import Any


def _fmt(ts: Any) -> str:
    if not ts:
        return "—"
    try:
        s = str(ts).replace("Z", "+00:00")
        if " " in s and "T" not in s:
            s = s.replace(" ", "T")
        d = datetime.fromisoformat(s)
        return d.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def render_single_batch_pdf(batch: dict[str, Any],
                            batch_type: dict[str, Any] | None,
                            events: list[dict[str, Any]],
                            summaries: list[dict[str, Any]]) -> bytes:
    """Build a one-page PDF for a single batch. Uses reportlab's
    PlatypusBuilder for clean tables; falls back gracefully if a row
    is missing data."""
    from reportlab.lib.pagesizes import A4  # type: ignore
    from reportlab.lib import colors  # type: ignore
    from reportlab.lib.styles import getSampleStyleSheet  # type: ignore
    from reportlab.platypus import (  # type: ignore
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title=f"Batch {batch.get('identifier') or batch.get('id')}")
    styles = getSampleStyleSheet()
    story: list = []

    story.append(Paragraph(f"<b>Batch Report</b>", styles["Title"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"<b>Identifier:</b> {batch.get('identifier') or batch.get('id')}", styles["Normal"]))
    story.append(Paragraph(f"<b>Status:</b> {batch.get('status', '')}", styles["Normal"]))
    if batch_type:
        story.append(Paragraph(f"<b>Type:</b> {batch_type.get('name', '')}", styles["Normal"]))
    story.append(Paragraph(f"<b>Product:</b> {batch.get('product') or '—'}", styles["Normal"]))
    story.append(Paragraph(f"<b>Recipe:</b> {batch.get('recipe') or '—'}", styles["Normal"]))
    story.append(Paragraph(f"<b>Operator:</b> {batch.get('operator') or '—'}", styles["Normal"]))
    story.append(Paragraph(f"<b>Gateway:</b> {batch.get('gateway_id') or '—'}", styles["Normal"]))
    story.append(Paragraph(f"<b>Started:</b> {_fmt(batch.get('started_utc'))}", styles["Normal"]))
    story.append(Paragraph(f"<b>Ended:</b> {_fmt(batch.get('ended_utc'))}", styles["Normal"]))
    if batch.get("notes"):
        story.append(Paragraph(f"<b>Notes:</b> {batch.get('notes')}", styles["Normal"]))
    story.append(Spacer(1, 12))

    # Summaries
    story.append(Paragraph("<b>Tag Summary</b>", styles["Heading2"]))
    sum_data = [["Tag", "Min", "Max", "Avg", "First", "Last", "σ", "Count"]]
    for s in summaries or []:
        sum_data.append([
            s.get("tag_name", ""),
            f"{(s.get('min_value') or 0):.3f}",
            f"{(s.get('max_value') or 0):.3f}",
            f"{(s.get('avg_value') or 0):.3f}",
            f"{(s.get('first_value') or 0):.3f}",
            f"{(s.get('last_value') or 0):.3f}",
            f"{(s.get('stdev_value') or 0):.3f}",
            str(s.get("sample_count") or 0),
        ])
    if len(sum_data) == 1:
        sum_data.append(["(no data)"] + [""] * 7)
    t = Table(sum_data, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0e7a78")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#999")),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
    ]))
    story.append(t)
    story.append(Spacer(1, 12))

    # Events
    story.append(Paragraph("<b>Event Log</b>", styles["Heading2"]))
    ev_data = [["Time", "Kind", "Severity", "Actor", "Message"]]
    for e in events or []:
        ev_data.append([
            _fmt(e.get("ts_utc")),
            str(e.get("kind") or ""),
            str(e.get("severity") or ""),
            str(e.get("actor") or ""),
            (str(e.get("message") or ""))[:80],
        ])
    if len(ev_data) == 1:
        ev_data.append(["(no events)"] + [""] * 4)
    et = Table(ev_data, repeatRows=1, colWidths=[100, 100, 60, 70, 200])
    et.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0e7a78")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#999")),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
    ]))
    story.append(et)

    doc.build(story)
    return buf.getvalue()
