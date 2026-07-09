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
                            summaries: list[dict[str, Any]],
                            manual_entries: list[dict[str, Any]] | None = None) -> bytes:
    """Build a one-page PDF for a single batch. Uses reportlab's
    PlatypusBuilder for clean tables; falls back gracefully if a row
    is missing data.

    Operator 2026-07-09: now includes the batch PASS/FAIL result, per-tag spec
    limits + in-spec %, and operator manual entries when present. manual_entries
    is optional so every existing caller keeps working unchanged."""
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
    # Operator 2026-07-09: overall PASS/FAIL result (green/red) when limits ran.
    _res = str(batch.get("result") or "").lower()
    if _res in ("pass", "fail"):
        _col = "#16a34a" if _res == "pass" else "#dc2626"
        story.append(Paragraph(
            f'<b>Result:</b> <font color="{_col}"><b>{_res.upper()}</b></font>'
            f" &nbsp;({batch.get('pass_tag_count') or 0} passed · {batch.get('fail_tag_count') or 0} failed)",
            styles["Normal"]))
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

    # Summaries (now with limits + in-spec% + per-tag result)
    story.append(Paragraph("<b>Tag Summary</b>", styles["Heading2"]))
    _has_limits = any((s.get("lower_limit") is not None or s.get("upper_limit") is not None) for s in (summaries or []))
    if _has_limits:
        sum_data = [["Tag", "Min", "Max", "Avg", "σ", "Count", "Limits", "In-spec%", "Result"]]
        for s in summaries or []:
            lo, up = s.get("lower_limit"), s.get("upper_limit")
            lim = f"{lo if lo is not None else '−inf'}..{up if up is not None else '+inf'}" if (lo is not None or up is not None) else "—"
            pf = str(s.get("pass_fail") or "").upper()
            pf = pf if pf in ("PASS", "FAIL") else "—"
            insp = s.get("in_spec_pct")
            sum_data.append([
                s.get("tag_name", ""),
                f"{(s.get('min_value') or 0):.3f}", f"{(s.get('max_value') or 0):.3f}",
                f"{(s.get('avg_value') or 0):.3f}", f"{(s.get('stdev_value') or 0):.3f}",
                str(s.get("sample_count") or 0), lim,
                (f"{insp}%" if insp is not None else "—"), pf,
            ])
        _ncol = 9
    else:
        sum_data = [["Tag", "Min", "Max", "Avg", "First", "Last", "σ", "Count"]]
        for s in summaries or []:
            sum_data.append([
                s.get("tag_name", ""),
                f"{(s.get('min_value') or 0):.3f}", f"{(s.get('max_value') or 0):.3f}",
                f"{(s.get('avg_value') or 0):.3f}", f"{(s.get('first_value') or 0):.3f}",
                f"{(s.get('last_value') or 0):.3f}", f"{(s.get('stdev_value') or 0):.3f}",
                str(s.get("sample_count") or 0),
            ])
        _ncol = 8
    if len(sum_data) == 1:
        sum_data.append(["(no data)"] + [""] * (_ncol - 1))
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

    # Operator 2026-07-09: manual operator entries (if any).
    if manual_entries:
        story.append(Paragraph("<b>Manual Entries</b>", styles["Heading2"]))
        me_rows = [["Field", "Value"]]
        for m in manual_entries:
            val = m.get("value_text")
            if val is None and m.get("value_num") is not None:
                val = m.get("value_num")
            me_rows.append([str(m.get("field_label") or m.get("field_key") or ""), str(val if val is not None else "")])
        me_t = Table(me_rows, repeatRows=1, colWidths=[200, 260])
        me_t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0e7a78")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#999")),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
        ]))
        story.append(me_t)
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


def render_parent_rollup_pdf(parent: dict[str, Any],
                             parent_type: dict[str, Any] | None,
                             rollup: dict[str, Any],
                             children: list[dict[str, Any]],
                             service: Any) -> bytes:
    """Multi-section PDF for a parent batch:
      Page 1: cover with status totals + per-tag aggregated stats
      Page N: one section per child batch (reuses single-batch shape)

    Operator 2026-06-30: lets a supervisor email/print one document for an
    entire shift or production run instead of N separate child PDFs.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
    )
    import io as _io

    buf = _io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title=f"Parent batch {parent.get('identifier') or parent.get('id')}")
    styles = getSampleStyleSheet()
    story: list[Any] = []

    # ----- cover ------------------------------------------------------
    story.append(Paragraph(f"<b>Parent Batch Report</b> — {parent.get('identifier') or parent.get('id')}", styles["Title"]))
    story.append(Spacer(1, 8))
    head_rows = [
        ["Type",       (parent_type or {}).get("name") or "—"],
        ["Status",     parent.get("status") or "—"],
        ["Product",    parent.get("product") or "—"],
        ["Recipe",     parent.get("recipe") or "—"],
        ["Operator",   parent.get("operator") or "—"],
        ["Started",    parent.get("started_utc") or "—"],
        ["Ended",      parent.get("ended_utc") or "—"],
    ]
    ht = Table(head_rows, colWidths=[100, 380])
    ht.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#0e7a78")),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#999")),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(ht)
    story.append(Spacer(1, 14))

    totals = rollup.get("totals") or {}
    story.append(Paragraph(
        f"<b>Children:</b> {totals.get('child_count', 0)} total · "
        f"{totals.get('completed', 0)} completed · "
        f"{totals.get('validated', 0)} validated · "
        f"{totals.get('failed', 0)} failed · "
        f"{totals.get('cancelled', 0)} cancelled · "
        f"{totals.get('running', 0)} running",
        styles["Normal"]))
    story.append(Spacer(1, 10))

    # Aggregated stats table
    story.append(Paragraph("<b>Aggregated tag statistics</b> (weighted avg across children)", styles["Heading3"]))
    tag_rows: list[list[Any]] = [["Tag", "Gateway", "Children", "Samples", "Min", "Max", "Avg"]]
    for t in (rollup.get("tags") or []):
        def _f(v):
            try: return f"{float(v):.2f}"
            except Exception: return "—"
        tag_rows.append([
            t.get("tag_name") or "—",
            t.get("gateway_id") or "—",
            t.get("contributing_children") or 0,
            t.get("sample_count") or 0,
            _f(t.get("min_value")),
            _f(t.get("max_value")),
            _f(t.get("avg_value")),
        ])
    if len(tag_rows) == 1:
        tag_rows.append(["(no aggregated tag data)"] + [""] * 6)
    tt = Table(tag_rows, repeatRows=1, colWidths=[110, 90, 55, 60, 60, 60, 60])
    tt.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0e7a78")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#999")),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
    ]))
    story.append(tt)
    story.append(Spacer(1, 12))

    # Children list
    story.append(Paragraph("<b>Child batches</b>", styles["Heading3"]))
    ch_rows: list[list[Any]] = [["Identifier", "Status", "Started", "Ended", "Operator"]]
    for ch in children:
        ch_rows.append([
            ch.get("identifier") or ch.get("id") or "—",
            ch.get("status") or "—",
            ch.get("started_utc") or "—",
            ch.get("ended_utc") or "—",
            ch.get("operator") or "—",
        ])
    if len(ch_rows) == 1:
        ch_rows.append(["(no child batches)"] + [""] * 4)
    ct = Table(ch_rows, repeatRows=1, colWidths=[140, 70, 110, 110, 90])
    ct.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0e7a78")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#999")),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
    ]))
    story.append(ct)

    # ----- per-child sections ---------------------------------------
    if children:
        story.append(PageBreak())
        for ch in children:
            ch_type = service.get_batch_type(ch.get("batch_type_id") or "") if ch.get("batch_type_id") else None
            ch_events = service.list_events(ch["id"], limit=200)
            ch_summaries = service.list_summaries(ch["id"])
            story.append(Paragraph(
                f"<b>Child:</b> {ch.get('identifier') or ch.get('id')} "
                f"<font color='#666'>({ch.get('status') or '—'})</font>",
                styles["Heading2"]))
            story.append(Spacer(1, 6))
            # Reuse single-batch sections inline (light copy: just the
            # summaries table) to keep the file size bounded.
            if ch_summaries:
                rows = [["Tag", "Samples", "Min", "Max", "Avg", "Stdev"]]
                for s in ch_summaries:
                    def _f(v):
                        try: return f"{float(v):.2f}"
                        except Exception: return "—"
                    rows.append([
                        s.get("tag_name") or "—",
                        s.get("sample_count") or 0,
                        _f(s.get("min_value")), _f(s.get("max_value")),
                        _f(s.get("avg_value")), _f(s.get("stdev_value")),
                    ])
                st = Table(rows, repeatRows=1, colWidths=[150, 60, 60, 60, 60, 60])
                st.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0e7a78")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#999")),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                ]))
                story.append(st)
            else:
                story.append(Paragraph("(no tag summaries)", styles["Normal"]))
            story.append(Spacer(1, 14))

    doc.build(story)
    return buf.getvalue()
