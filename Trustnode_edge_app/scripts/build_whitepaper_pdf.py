"""Build a branded, modern PDF of the TrustNode Edge security whitepaper.

Reads:
  docs/TRUSTNODE_SECURITY_AND_ARCHITECTURE_WHITEPAPER.md
  docs/diagrams/*.png

Writes:
  docs/TrustNode_Security_Whitepaper.pdf

We do NOT try to render arbitrary Markdown with full fidelity. Instead, we
take the well-known structure of the whitepaper (top-level numbered sections,
tables, callout blockquotes, fenced ASCII code blocks) and lay it out using
ReportLab Platypus with a TrustNode visual identity:

  - Navy + teal brand palette
  - Sans-serif body, headings in navy
  - Soft-paper callout boxes for the plain-language explanations
  - First page is a styled cover; section dividers between top-level sections
  - Footer with document name + page number
  - PNG architecture diagrams inserted in the corresponding sections

This is deliberately deterministic — re-running produces the same PDF.
"""
from __future__ import annotations

import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    Image,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)

# --------------------------------------------------------------------- brand --
NAVY = colors.HexColor("#0e1a3a")
TEAL = colors.HexColor("#14b8a6")
SLATE = colors.HexColor("#2b3548")
PAPER = colors.white
SOFT = colors.HexColor("#f4f6fa")
BORDER = colors.HexColor("#d7dce5")
INK = colors.HexColor("#0e1116")
MUTED = colors.HexColor("#5b6473")
ALERT = colors.HexColor("#c25b35")
GOOD = colors.HexColor("#2d7a4f")
CALLOUT_BG = colors.HexColor("#eef3fb")
CALLOUT_BORDER = colors.HexColor("#b7c5dd")
TABLE_HEAD = NAVY
TABLE_ROW_ALT = colors.HexColor("#f9fafc")

# --------------------------------------------------------------------- paths --
ROOT = Path(__file__).resolve().parents[1]
MD_PATH = ROOT / "docs" / "TRUSTNODE_SECURITY_AND_ARCHITECTURE_WHITEPAPER.md"
DIAG_DIR = ROOT / "docs" / "diagrams"
OUT_PATH = ROOT / "docs" / "TrustNode_Security_Whitepaper.pdf"

# Where in the document each diagram lives once. We avoid duplicating.
# Section ids are the leading "N" in "# N. ..." top-level markdown headings.
DIAGRAM_SEQUENCE = [
    ("3", "architecture_purdue.png", "Figure 1 — Purdue model with TrustNode."),
    ("5", "architecture_three_role.png", "Figure 2 — Three roles enforced server-side."),
    ("7", "architecture_store_forward.png", "Figure 3 — Store-and-forward resilience."),
    ("8", "architecture_multi_tenant.png", "Figure 4 — Multi-tenant isolation."),
    # §13 deployment topologies — five diagrams
    ("13", "deployment_plant_pc.png",
     "Figure A — Plant PC / desktop install."),
    ("13", "deployment_ipc_panel.png",
     "Figure B — Industrial PC (IPC) in the electrical panel."),
    ("13", "deployment_customer_server.png",
     "Figure C — Customer server in their datacenter."),
    ("13", "deployment_cloud_bridged.png",
     "Figure D — Cloud-bridged (the reference topology)."),
    ("13", "deployment_multi_plant.png",
     "Figure E — Multi-plant central historian."),
    # §14 storage options
    ("14", "storage_options.png", "Figure F — Five storage options at a glance."),
    # §17 architecture diagrams — keep the single-customer overview here
    ("17", "architecture_single_customer.png",
     "Figure G — Full single-customer deployment overview."),
]


# --------------------------------------------------------------- text styles --
def build_styles():
    ss = getSampleStyleSheet()
    base = ss["BodyText"]
    body = ParagraphStyle(
        "Body",
        parent=base,
        fontName="Helvetica",
        fontSize=10.5,
        leading=15,
        textColor=INK,
        spaceAfter=4,
    )
    body_small = ParagraphStyle(
        "BodySmall", parent=body, fontSize=9, leading=13, textColor=MUTED
    )
    h1 = ParagraphStyle(
        "H1",
        parent=base,
        fontName="Helvetica-Bold",
        fontSize=22,
        leading=26,
        textColor=NAVY,
        spaceBefore=18,
        spaceAfter=10,
    )
    h2 = ParagraphStyle(
        "H2",
        parent=base,
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=18,
        textColor=NAVY,
        spaceBefore=12,
        spaceAfter=6,
    )
    h3 = ParagraphStyle(
        "H3",
        parent=base,
        fontName="Helvetica-Bold",
        fontSize=11.5,
        leading=15,
        textColor=NAVY,
        spaceBefore=8,
        spaceAfter=4,
    )
    bullet = ParagraphStyle(
        "Bullet",
        parent=body,
        leftIndent=14,
        bulletIndent=2,
        spaceAfter=2,
    )
    callout = ParagraphStyle(
        "Callout",
        parent=body,
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=NAVY,
        leftIndent=8,
        rightIndent=8,
    )
    callout_label = ParagraphStyle(
        "CalloutLabel",
        parent=callout,
        fontName="Helvetica-Bold",
        textColor=TEAL,
    )
    mono = ParagraphStyle(
        "Mono",
        parent=body,
        fontName="Courier",
        fontSize=9,
        leading=11.5,
        textColor=INK,
    )
    figcap = ParagraphStyle(
        "FigCap",
        parent=body,
        fontName="Helvetica-Oblique",
        fontSize=9,
        leading=12,
        textColor=MUTED,
        alignment=TA_CENTER,
        spaceAfter=10,
    )
    cover_title = ParagraphStyle(
        "CoverTitle",
        parent=base,
        fontName="Helvetica-Bold",
        fontSize=34,
        leading=40,
        textColor=PAPER,
        alignment=TA_LEFT,
    )
    cover_sub = ParagraphStyle(
        "CoverSub",
        parent=base,
        fontName="Helvetica",
        fontSize=15,
        leading=20,
        textColor=colors.HexColor("#cdd5e0"),
        alignment=TA_LEFT,
        spaceBefore=12,
    )
    cover_meta = ParagraphStyle(
        "CoverMeta",
        parent=base,
        fontName="Helvetica",
        fontSize=11,
        leading=14,
        textColor=colors.HexColor("#9aa3b3"),
        alignment=TA_LEFT,
    )
    return {
        "body": body,
        "body_small": body_small,
        "h1": h1,
        "h2": h2,
        "h3": h3,
        "bullet": bullet,
        "callout": callout,
        "callout_label": callout_label,
        "mono": mono,
        "figcap": figcap,
        "cover_title": cover_title,
        "cover_sub": cover_sub,
        "cover_meta": cover_meta,
    }


# ------------------------------------------------------------ markdown parse --
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
TABLE_ROW_RE = re.compile(r"^\|(.+)\|\s*$")
TABLE_SEP_RE = re.compile(r"^\|\s*:?-{2,}.*$")
FENCE_RE = re.compile(r"^```")
BLOCKQUOTE_RE = re.compile(r"^>\s?(.*)$")
ORDERED_RE = re.compile(r"^(\d+)\.\s+(.*)$")
BULLET_RE = re.compile(r"^[\*\-]\s+(.*)$")


def _md_inline(text: str) -> str:
    """Convert inline Markdown to ReportLab-flavoured HTML.

    Order matters and we need to make sure emphasis substitution does NOT
    look inside `code` spans (which often contain `_` and `*`).
    Strategy: pull code spans out into placeholders, run emphasis on the
    rest, then put the formatted code spans back.
    """
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    placeholders: list[str] = []

    def _stash_code(match):
        token = f"\x00CODE{len(placeholders)}\x00"
        placeholders.append(
            f'<font face="Courier" size="9.5">{match.group(1)}</font>'
        )
        return token

    text = re.sub(r"`([^`]+)`", _stash_code, text)
    # Bold first (longer match), then italic
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", text)
    # Links — we drop the URL because this is print, but keep label
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", text)
    # Restore code spans
    for i, p in enumerate(placeholders):
        text = text.replace(f"\x00CODE{i}\x00", p)
    return text


def parse_markdown(md: str):
    """Iterate the markdown as a sequence of typed blocks.

    Each yield is a tuple (kind, payload).
      kind = "h1"|"h2"|"h3"|"para"|"bullet"|"ordered"|"table"|"code"|"callout"|"hr"
    """
    lines = md.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.rstrip()

        if not stripped:
            i += 1
            continue

        if stripped == "---":
            yield ("hr", None)
            i += 1
            continue

        # Headings
        m = HEADING_RE.match(stripped)
        if m:
            level = len(m.group(1))
            txt = m.group(2).strip()
            if level == 1:
                yield ("h1", txt)
            elif level == 2:
                yield ("h2", txt)
            else:
                yield ("h3", txt)
            i += 1
            continue

        # Fenced code
        if FENCE_RE.match(stripped):
            i += 1
            buf = []
            while i < n and not FENCE_RE.match(lines[i]):
                buf.append(lines[i])
                i += 1
            if i < n:
                i += 1  # consume closing fence
            yield ("code", "\n".join(buf))
            continue

        # Blockquote / callout
        if BLOCKQUOTE_RE.match(stripped):
            buf = []
            while i < n:
                m = BLOCKQUOTE_RE.match(lines[i].rstrip())
                if not m:
                    break
                buf.append(m.group(1))
                i += 1
            # join paragraphs, keep > "  " (blank quote) as paragraph break
            joined = "\n".join(buf)
            yield ("callout", joined)
            continue

        # Table
        if TABLE_ROW_RE.match(stripped):
            buf = []
            while i < n and TABLE_ROW_RE.match(lines[i].rstrip()):
                buf.append(lines[i].rstrip())
                i += 1
            yield ("table", buf)
            continue

        # Ordered list
        if ORDERED_RE.match(stripped):
            buf = []
            while i < n:
                m = ORDERED_RE.match(lines[i].rstrip())
                if not m:
                    break
                buf.append(m.group(2))
                i += 1
            yield ("ordered", buf)
            continue

        # Bullet list
        if BULLET_RE.match(stripped):
            buf = []
            while i < n:
                m = BULLET_RE.match(lines[i].rstrip())
                if not m:
                    break
                buf.append(m.group(1))
                i += 1
            yield ("bullet", buf)
            continue

        # Paragraph -- collect until blank line, fence, heading, table, list
        buf = [stripped]
        i += 1
        while i < n:
            nxt = lines[i].rstrip()
            if not nxt:
                break
            if HEADING_RE.match(nxt) or FENCE_RE.match(nxt) or TABLE_ROW_RE.match(nxt):
                break
            if BLOCKQUOTE_RE.match(nxt) or ORDERED_RE.match(nxt) or BULLET_RE.match(nxt):
                break
            if nxt == "---":
                break
            buf.append(nxt)
            i += 1
        yield ("para", " ".join(buf))


# ------------------------------------------------------ block -> flowables --
def make_callout_table(text_inline: str, styles) -> Table:
    """Render a callout (blockquote) as a soft-paper box with a coloured left edge."""
    paragraphs = [p for p in text_inline.split("\n") if p.strip()]
    flow = []
    for j, p in enumerate(paragraphs):
        flow.append(Paragraph(_md_inline(p), styles["callout"]))
        if j < len(paragraphs) - 1:
            flow.append(Spacer(1, 4))
    inner = Table([[flow]], colWidths=[16.0 * cm])
    inner.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), CALLOUT_BG),
                ("BOX", (0, 0), (-1, -1), 0.5, CALLOUT_BORDER),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LINEBEFORE", (0, 0), (0, 0), 3, TEAL),
            ]
        )
    )
    return inner


def make_table(rows: list[str], styles) -> Table:
    """Render a pipe-delimited markdown table as a styled ReportLab table."""
    # First row = header. Drop separator if present.
    cells = []
    for r in rows:
        if TABLE_SEP_RE.match(r):
            continue
        parts = [c.strip() for c in r.strip("|").split("|")]
        cells.append(parts)
    if not cells:
        return None  # type: ignore[return-value]
    n_cols = max(len(r) for r in cells)
    # Make cell paragraphs
    data: list[list] = []
    for ri, row in enumerate(cells):
        wrapped = []
        for ci in range(n_cols):
            txt = row[ci] if ci < len(row) else ""
            style = styles["body"] if ri > 0 else styles["body"]
            if ri == 0:
                wrapped.append(
                    Paragraph(
                        f"<font color='white'><b>{_md_inline(txt)}</b></font>",
                        styles["body"],
                    )
                )
            else:
                wrapped.append(Paragraph(_md_inline(txt), styles["body"]))
        data.append(wrapped)

    # Even col widths within 16 cm
    col_w = [16.0 * cm / n_cols] * n_cols
    t = Table(data, colWidths=col_w, repeatRows=1)
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), TABLE_HEAD),
        ("TEXTCOLOR", (0, 0), (-1, 0), PAPER),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, NAVY),
        ("LINEBELOW", (0, 1), (-1, -1), 0.3, BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]
    for ri in range(2, len(data), 2):
        style_cmds.append(("BACKGROUND", (0, ri), (-1, ri), TABLE_ROW_ALT))
    t.setStyle(TableStyle(style_cmds))
    return t


def make_code_block(text: str, styles) -> Table:
    """Render fenced ASCII (architecture diagrams in code form) as a mono block."""
    pre = Preformatted(text, styles["mono"])
    box = Table([[pre]], colWidths=[16.0 * cm])
    box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), SOFT),
                ("BOX", (0, 0), (-1, -1), 0.4, BORDER),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return box


def make_diagram(png_path: Path, caption: str, styles) -> list:
    if not png_path.exists():
        return []
    img = Image(str(png_path), width=16.0 * cm, height=9.0 * cm)
    cap = Paragraph(caption, styles["figcap"])
    return [Spacer(1, 6), img, cap, Spacer(1, 6)]


# --------------------------------------------------------------- page deco --
def cover_page(canvas, doc, styles):
    canvas.saveState()
    # Navy band top 2/3
    w, h = A4
    canvas.setFillColor(NAVY)
    canvas.rect(0, h * 0.45, w, h * 0.55, fill=1, stroke=0)
    # Teal accent line
    canvas.setFillColor(TEAL)
    canvas.rect(0, h * 0.45 - 6, w, 6, fill=1, stroke=0)
    canvas.restoreState()


def regular_page(canvas, doc):
    canvas.saveState()
    w, h = A4
    # Top accent bar
    canvas.setFillColor(NAVY)
    canvas.rect(0, h - 12 * mm, w, 12 * mm, fill=1, stroke=0)
    canvas.setFillColor(TEAL)
    canvas.rect(0, h - 13 * mm, w, 1 * mm, fill=1, stroke=0)
    canvas.setFillColor(PAPER)
    canvas.setFont("Helvetica-Bold", 9.5)
    canvas.drawString(20 * mm, h - 8.5 * mm, "TrustNode Edge — Security & Architecture Whitepaper")
    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(colors.HexColor("#cdd5e0"))
    canvas.drawRightString(w - 20 * mm, h - 8.5 * mm, "v 2026-05-15 r2")

    # Footer
    canvas.setStrokeColor(BORDER)
    canvas.setLineWidth(0.4)
    canvas.line(20 * mm, 15 * mm, w - 20 * mm, 15 * mm)
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 8.5)
    canvas.drawString(20 * mm, 10 * mm, "© TrustNode — share freely with customers and prospects")
    canvas.drawRightString(w - 20 * mm, 10 * mm, f"Page {doc.page}")
    canvas.restoreState()


# --------------------------------------------------------------- build PDF --
def build_pdf():
    if not MD_PATH.exists():
        raise SystemExit(f"Whitepaper markdown not found: {MD_PATH}")
    md = MD_PATH.read_text(encoding="utf-8")

    styles = build_styles()

    # --- Cover flowables ----------------------------------------------------
    cover_flow = [
        Spacer(1, 4.5 * cm),
        Paragraph("TrustNode Edge", styles["cover_title"]),
        Paragraph("Security &amp; Architecture Whitepaper", styles["cover_title"]),
        Paragraph(
            "Built for industrial operators. Read-only on the plant floor, "
            "outbound-only on the network, and tenant-isolated end-to-end.",
            styles["cover_sub"],
        ),
        Spacer(1, 6 * cm),
        Paragraph("Audience: Plant Managers · IT Security · OT Engineering · Compliance",
                  styles["cover_meta"]),
        Paragraph("Document version: 2026-05-15 (rev. 2)", styles["cover_meta"]),
        Paragraph("Maintained by the TrustNode engineering team", styles["cover_meta"]),
    ]

    # --- Body flowables, walking the parser ---------------------------------
    flow: list = []
    flow.append(NextPageTemplate("regular"))
    flow.append(PageBreak())

    current_h1_num = None  # tracks "# N. ..." section number
    diagrams_inserted = set()

    for kind, payload in parse_markdown(md):
        if kind == "hr":
            # ignore the document's --- separators — visual layout handles section breaks
            continue
        if kind == "h1":
            # Markdown uses "# " for top-level section like "1. Trust at a glance".
            # We bump page on big sections after the first one and add a teal bar.
            if current_h1_num is not None:
                # insert any diagram destined for the just-finished section
                for sec, png, cap in DIAGRAM_SEQUENCE:
                    if sec == current_h1_num and png not in diagrams_inserted:
                        flow.extend(make_diagram(DIAG_DIR / png, cap, styles))
                        diagrams_inserted.add(png)
                flow.append(Spacer(1, 6))
                flow.append(HRFlowable(width="100%", thickness=0.5, color=BORDER))
                flow.append(Spacer(1, 12))
            # parse number
            num_match = re.match(r"^(\d+)\.\s+(.*)$", payload)
            current_h1_num = num_match.group(1) if num_match else None
            flow.append(Paragraph(_md_inline(payload), styles["h1"]))
            continue
        if kind == "h2":
            flow.append(Paragraph(_md_inline(payload), styles["h2"]))
            continue
        if kind == "h3":
            flow.append(Paragraph(_md_inline(payload), styles["h3"]))
            continue
        if kind == "para":
            flow.append(Paragraph(_md_inline(payload), styles["body"]))
            continue
        if kind == "bullet":
            for item in payload:
                flow.append(Paragraph(f"•&nbsp;&nbsp;{_md_inline(item)}", styles["bullet"]))
            continue
        if kind == "ordered":
            for j, item in enumerate(payload, 1):
                flow.append(Paragraph(f"<b>{j}.</b>&nbsp;&nbsp;{_md_inline(item)}", styles["bullet"]))
            continue
        if kind == "callout":
            flow.append(Spacer(1, 4))
            flow.append(make_callout_table(payload, styles))
            flow.append(Spacer(1, 4))
            continue
        if kind == "table":
            t = make_table(payload, styles)
            if t is not None:
                flow.append(Spacer(1, 4))
                flow.append(KeepTogether(t))
                flow.append(Spacer(1, 6))
            continue
        if kind == "code":
            # Replace ASCII diagrams in §2 with the rendered PNG inline.
            # Keep all other code blocks (e.g. diagrams 3-layer scope) as mono.
            flow.append(make_code_block(payload, styles))
            continue

    # Insert any remaining diagrams at the end of the document body
    for sec, png, cap in DIAGRAM_SEQUENCE:
        if png not in diagrams_inserted:
            flow.append(Spacer(1, 8))
            flow.append(Paragraph(f"Figure for §{sec}", styles["h3"]))
            flow.extend(make_diagram(DIAG_DIR / png, cap, styles))
            diagrams_inserted.add(png)

    # --- Document templates -------------------------------------------------
    doc = BaseDocTemplate(
        str(OUT_PATH),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=22 * mm,
        bottomMargin=20 * mm,
        title="TrustNode Edge — Security & Architecture Whitepaper",
        author="TrustNode",
    )
    cover_frame = Frame(20 * mm, 20 * mm, A4[0] - 40 * mm, A4[1] - 40 * mm, id="cover", showBoundary=0)
    body_frame = Frame(20 * mm, 18 * mm, A4[0] - 40 * mm, A4[1] - 38 * mm, id="body", showBoundary=0)
    doc.addPageTemplates(
        [
            PageTemplate(id="cover", frames=[cover_frame],
                          onPage=lambda c, d: cover_page(c, d, styles)),
            PageTemplate(id="regular", frames=[body_frame], onPage=regular_page),
        ]
    )

    final = cover_flow + flow
    doc.build(final)
    print(OUT_PATH)


if __name__ == "__main__":
    build_pdf()
