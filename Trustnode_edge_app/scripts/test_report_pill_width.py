# -*- coding: utf-8 -*-
"""A pill can fit its text, and then be placed left, centre or right.

2026-10-17, reported for the second time:

  "the pill report section it is still not working / we need to have option
   that when we use it as a header, it also can be set as full width or not,
   so if we have 1 per row the width would be in full page width if not,
   only to fit the text and be aligned center, left and right, now it is
   only working for left"

Two separate faults behind that one sentence:

  1. "Fit to content" was implemented by leaving colWidths unset and
     trusting ReportLab to size the column from its contents. It does not,
     for a Paragraph: measured here, a 140pt string in a colWidths=None
     table still wrapped to 510.2pt - the whole text block. So the table was
     page-wide either way and hAlign had nothing to move. The fit is now
     measured with stringWidth.

  2. The header band (`header` + as_pill) read NEITHER key. The editor had
     offered "Pill width" and a position for a week while _section_header
     passed width=_avail_width() and no alignment at all, so a pill heading
     was always full width, always on the left margin.

Also pinned here: a header pill prints at font_size * 1.35, so measuring it
at font_size under-sizes the column and the heading wraps to two lines.

Writes into a scratch workspace; never the live store.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
_TMP = tempfile.mkdtemp(prefix="tn-pillw-")
os.environ.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=_TMP,
                  TRUSTNODE_APP_STORE_PATH=os.path.join(_TMP, "s.db"),
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)


from app.services.report_renderer import (                        # noqa: E402
    _section_pill_card, _section_header, _build_styles, _avail_width,
    pill_position)
from reportlab.platypus import Table                              # noqa: E402

print("TrustNode - a pill fits its text, and then it can be placed")
print()

STYLES = _build_styles()
AVAIL = _avail_width({})
TEXT = "INITIAL TEST OF THE UNIT"


def geom(section, renderer):
    story: list = []
    renderer(dict(section), STYLES, story)
    tables = [f for f in story if isinstance(f, Table)]
    if not tables:
        return None
    table = tables[0]
    width, height = table.wrap(AVAIL, 1000)
    return table.hAlign, round(width, 1), round(height, 1)


def pills(**extra):
    sec = {"type": "pill_card", "columns": 1, "pills": [{"text": TEXT}]}
    sec.update(extra)
    return geom(sec, _section_pill_card)


def placed(section):
    """Where each fitted pill actually lands on the page.

    2026-10-17, third pass: a fitted pill is its own small table inside a
    full-width column, so the thing to measure is the PILL, not the outer
    table. Returns (alignment, left edge, right edge) per pill, in points
    from the left margin. The grid is captured before wrap() because wrap
    replaces the cell values.
    """
    story: list = []
    _section_pill_card(dict(section), STYLES, story)
    table = [f for f in story if isinstance(f, Table)][0]
    grid = [list(r) for r in table._cellvalues]
    cols = len(grid[0])
    col_w = AVAIL / cols
    out = []
    for row in grid:
        for c, cell in enumerate(row):
            if not isinstance(cell, Table):
                continue
            inner = cell.wrap(col_w, 999)[0]
            if cell.hAlign == "LEFT":
                x = 0.0
            elif cell.hAlign == "CENTER":
                x = (col_w - inner) / 2
            else:
                x = col_w - inner
            out.append((cell.hAlign, round(c * col_w + x, 1),
                        round(c * col_w + x + inner, 1)))
    return out


def band(**extra):
    sec = {"type": "header", "title": TEXT, "as_pill": True,
           "show_generated_at": False}
    sec.update(extra)
    return geom(sec, _section_header)


print("[1. the page width this is measured against]")
check("the text block is the usual A4 width", 400 < AVAIL < 600,
      "%.1fpt" % AVAIL)

print()
print("[2. pill cards: full width is unchanged]")
a, w, _h = pills()
check("an untouched section still fills the row", w == round(AVAIL, 1),
      "%.1f of %.1f - existing templates must not move" % (w, AVAIL))
check("  and still sits on the left", a == "LEFT", a)

print()
print("[3. pill cards: fit to the text, then place it]")
for want, kw in (("LEFT", {}), ("CENTER", {"pill_align": "center"}),
                 ("RIGHT", {"pill_align": "right"})):
    got = placed({"type": "pill_card", "columns": 1, "pill_fit": "content",
                  "pills": [{"text": TEXT}], **kw})
    check("  one fitted pill carries the section position",
          len(got) == 1 and got[0][0] == want, got)
    if got:
        _al, x0, x1 = got[0]
        check("    %-6s and is sized to its text" % want, (x1 - x0) < AVAIL * 0.6,
              "%.1fpt wide out of %.1f - colWidths=None does NOT shrink a "
              "Paragraph column, it fills the page" % (x1 - x0, AVAIL))
        if want == "LEFT":
            check("      hard against the left margin", x0 < 0.6, x0)
        elif want == "RIGHT":
            check("      hard against the right margin",
                  abs(x1 - AVAIL) < 0.6, "ends at %.1f of %.1f" % (x1, AVAIL))

print()
print("[3b. many pills on one row, each placed in its own column]")
# 2026-10-17, requested: "I should be able to add many pills in the same row
# and decide their positions ... the left one aligned on the left, the right
# one on the right and the two centre ones centralised in the columns."
four = placed({"type": "pill_card", "columns": 4, "pill_fit": "content",
               "pills": [{"text": "LEFT", "align": "left"},
                         {"text": "MIDDLE ONE", "align": "center"},
                         {"text": "MIDDLE TWO", "align": "center"},
                         {"text": "RIGHT", "align": "right"}]})
check("all four pills are placed", len(four) == 4, four)
if len(four) == 4:
    quarter = AVAIL / 4
    check("  the first is hard left on the page", four[0][1] < 0.6, four[0])
    check("  the last is hard right on the page",
          abs(four[3][2] - AVAIL) < 0.6,
          "ends at %.1f of %.1f - one position for the whole group could "
          "never do this" % (four[3][2], AVAIL))
    for i in (1, 2):
        al, x0, x1 = four[i]
        mid = (x0 + x1) / 2
        want_mid = (i + 0.5) * quarter
        check("  pill %d is centred in ITS column" % (i + 1),
              al == "CENTER" and abs(mid - want_mid) < 1.0,
              "centre %.1f, column centre %.1f" % (mid, want_mid))
    check("  and they do not overlap",
          all(four[i][2] <= four[i + 1][1] + 0.01 for i in range(3)), four)

two = placed({"type": "pill_card", "columns": 2, "pill_fit": "content",
              "pills": [{"text": "Result PASS", "align": "left"},
                        {"text": "Result PASS", "align": "right"}]})
check("two pills, one hard left and one hard right, on ONE row",
      len(two) == 2 and two[0][1] < 0.6 and abs(two[1][2] - AVAIL) < 0.6,
      two)

inherit = placed({"type": "pill_card", "columns": 2, "pill_fit": "content",
                  "pill_align": "center",
                  "pills": [{"text": "A"}, {"text": "B"}]})
check("  a pill with no position of its own follows the section's",
      len(inherit) == 2 and all(p[0] == "CENTER" for p in inherit), inherit)

print()
print("[4. the header BAND honours both keys]")
a, w, _h = band()
check("a plain pill heading still fills the width", w == round(AVAIL, 1),
      "%.1f of %.1f" % (w, AVAIL))
check("  and still sits left", a == "LEFT", a)
for want, kw in (("LEFT", {}), ("CENTER", {"pill_align": "center"}),
                 ("RIGHT", {"pill_align": "right"})):
    a, w, _h = band(pill_fit="content", **kw)
    check("  %-6s band hugs its text" % want, w < AVAIL * 0.6,
          "%.1fpt - _section_header used to pass width=_avail_width() "
          "unconditionally" % w)
    check("    and is placed there", a == want, a)

print()
print("[5. a fitted band is still ONE line]")
_a, _w, h_fill = band()
_a, _w, h_fit = band(pill_fit="content")
check("fitting does not make the heading wrap", h_fit == h_fill,
      "%.1fpt tall vs %.1f full width - a column measured at the base size "
      "instead of the printed 1.35x wraps onto a second line"
      % (h_fit, h_fill))
_a, _w, h_hdr_fill = pills(as_header=True)
_a, _w, h_hdr_fit = pills(as_header=True, pill_fit="content")
check("  nor a header pill, which prints 1.35x larger",
      h_hdr_fit == h_hdr_fill, "%.1f vs %.1f" % (h_hdr_fit, h_hdr_fill))

print()
print("[6. a bigger face needs a wider band]")
_a, w_small, _h = band(pill_fit="content", title_font_size=13)
_a, w_big, _h = band(pill_fit="content", title_font_size=20)
check("20pt text gets a wider band than 13pt", w_big > w_small * 1.3,
      "%.1f vs %.1f - a fixed guess would clip or waste space"
      % (w_big, w_small))

print()
print("[7. it can never run off the page]")
a, w, _h = pills(pill_fit="content", pills=[{"text": "X" * 200}])
check("a pill longer than the page is capped", w <= round(AVAIL, 1) + 0.1,
      "%.1fpt against a %.1fpt page" % (w, AVAIL))
a, w, _h = pills(pill_fit="content", columns=4,
                 pills=[{"text": "A" * 90}, {"text": "B" * 90},
                        {"text": "C" * 90}, {"text": "D" * 90}])
check("  and so is a row of four long ones", w <= round(AVAIL, 1) + 0.1,
      "%.1fpt" % w)

print()
print("[8. the position key]")
check("pill_align decides the position",
      pill_position({"pill_align": "right"}) == "RIGHT")
check("  `align` is still honoured for templates saved before it",
      pill_position({"align": "center"}) == "CENTER",
      "the layout card writes `align`; nobody's saved report may shift")
check("  pill_align wins when both are set",
      pill_position({"align": "center", "pill_align": "right"}) == "RIGHT")
check("  and nothing set is still LEFT", pill_position({}) == "LEFT")
check("  as is a value that means nothing",
      pill_position({"pill_align": "sideways"}) == "LEFT")

print()
print("[9. the editor exposes both, on both sections]")
DES = io.open(os.path.join(ROOT, "frontend", "src", "components", "Reports",
                           "ReportTemplateDesigner.jsx"), encoding="utf-8").read()
_fit = DES.count("onChange={(e) => onChange({ pill_fit: e.target.value })}")
_pos = DES.count("onChange={(e) => onChange({ pill_align: e.target.value })}")
check("there are two Pill width controls", _fit == 2,
      "the pill-card editor had a position but no width, which is why its "
      "Alignment did nothing: found %d" % _fit)
check("  and two position controls on the new key", _pos == 2, _pos)
check("  both show a value saved under the old key",
      DES.count("section.pill_align || section.align") == 2,
      "otherwise an existing template looks unset and the next save loses "
      "its placement")
# `align` is still a legitimate key elsewhere - the layout card's "Text
# alignment" and the image section both write it. What must not come back is
# a PILL control writing it, because the layout card writes the same key and
# whichever rendered last won: that is half of "only working for left".
_pill_block = DES[DES.index('{type === "pill_card" ?'):]
_pill_block = _pill_block[:_pill_block.index('{type === "image"')]
check("the pill-card editor no longer writes `align`",
      "onChange({ align: e.target.value })" not in _pill_block,
      "the layout card writes that key too, and the last one rendered won")
_hdr_block = DES[DES.index("{section.as_pill ? ("):]
_hdr_block = _hdr_block[:_hdr_block.index("FontControls")]
check("  nor does the header's pill block",
      "onChange({ align: e.target.value })" not in _hdr_block)

print()
print("[10. two title pills can share one row]")
# 2026-10-17, reported: "two pill titles that are one aligned on the left and
# other on the right from different sections but on the same row, using the
# feature to share the width of the previous". They came out STACKED, because
# group_side_by_side listed "header" among the types that may not START a row
# - so ticking "place beside the previous section" after a title did nothing
# at all, silently.
from app.services.report_renderer import group_side_by_side              # noqa: E402


def band_section(title, **kw):
    sec = {"type": "header", "title": title, "as_pill": True,
           "show_generated_at": False}
    sec.update(kw)
    return sec


grouped = group_side_by_side([
    band_section("LEFT ONE", pill_fit="content", pill_align="left"),
    band_section("RIGHT ONE", pill_fit="content", pill_align="right",
                 side_by_side=True)])
check("a title section can be the FIRST member of a row",
      len(grouped) == 1 and grouped[0].get("type") == "row",
      "%s - two pill titles came out on separate lines"
      % [g.get("type") for g in grouped])
if grouped and grouped[0].get("type") == "row":
    cols = grouped[0].get("columns") or []
    check("  both titles are in it", len(cols) == 2,
          [c.get("title") for c in cols])
    check("  and each keeps its own position",
          [c.get("pill_align") for c in cols] == ["left", "right"],
          [c.get("pill_align") for c in cols])
    # The column width has to reach the band, or a half-width title draws
    # itself at full page width and overflows its cell.
    story: list = []
    _section_header(dict(cols[1], _width=(AVAIL - 6) / 2, _in_column=True),
                    STYLES, story)
    band = [f for f in story if isinstance(f, Table)][0]
    width = band.wrap((AVAIL - 6) / 2, 999)[0]
    check("  a title in a column is sized to that column, not the page",
          width < AVAIL / 2, "%.1fpt in a %.1fpt column" % (width, (AVAIL - 6) / 2))
check("  a page break still may not start a row",
      len(group_side_by_side([{"type": "page_break"},
                              {"type": "text", "side_by_side": True}])) == 2,
      "a break and a spacer are not content")

print()
print("[11. the editor offers a position per pill]")
check("each pill row has its own position control",
      "updatePill(pill.id, { align: e.target.value })" in DES,
      "one position for the whole section cannot put one pill left and "
      "another right")
check("  blank means 'follow the section'",
      '<option value="">Position: section</option>' in DES)
check("  and the CSS row has a column for it",
      io.open(os.path.join(ROOT, "frontend", "src", "styles.css"),
              encoding="utf-8").read().count(
                  "minmax(84px, .9fr) 38px 38px minmax(92px, .9fr) auto auto") == 1,
      "a grid with fewer columns than children silently wraps every pill "
      "onto two lines")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
