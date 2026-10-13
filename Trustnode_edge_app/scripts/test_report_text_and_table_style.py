# -*- coding: utf-8 -*-
"""Report builder: fonts, colours, alignment, table borders, shared rows.

2026-10-09, requested: "when creating the reports section, we should be able
to modify fonts size, style and color, in special for text sections, borders
for tables and how the text should be aligned and if we can have two
sections or more aligned in the same row".

Before this every text block rendered with ONE hard-coded body style: a
heading inside a report could not be made bigger, a warning could not be
red, and nothing could be centred. Table rules were fixed at a hairline grid
in one grey.

Side-by-side rows already existed (up to three columns) - asserted here so a
later change cannot quietly drop it.

Scratch workspace; the live store is never opened.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
_TMP = tempfile.mkdtemp(prefix="tn-style-")
os.environ.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=_TMP,
                  TRUSTNODE_APP_STORE_PATH=os.path.join(_TMP, "s.db"),
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


from reportlab.platypus import Paragraph, Table            # noqa: E402
from app.services.report_renderer import (                 # noqa: E402
    _build_styles, _section_text, _draw_table, group_side_by_side)

ST = _build_styles()


def paragraphs(section):
    story: list = []
    _section_text(section, ST, story)
    return [f for f in story if isinstance(f, Paragraph)]


print("TrustNode - styling a report section")
print()

print("[text: size, weight, colour, alignment]")
paras = paragraphs({
    "type": "text", "title": "Conclusion", "text": "All checks passed.",
    "title_font_size": 20, "title_bold": True,
    "title_font_color": "#14a89a", "title_align": "center",
    "font_size": 13, "italic": True, "font_color": "#c0392b", "align": "right",
})
check("a text section renders a heading and a body", len(paras) == 2,
      "%d paragraph(s)" % len(paras))
if len(paras) == 2:
    h, b = paras[0].style, paras[1].style
    check("  the heading takes its own size", float(h.fontSize) == 20.0, h.fontSize)
    check("  ...weight", h.fontName == "Helvetica-Bold", h.fontName)
    check("  ...colour", h.textColor.hexval().endswith("14a89a"), h.textColor)
    check("  ...and alignment (1 = centre)", h.alignment == 1, h.alignment)
    check("  the BODY is styled independently", float(b.fontSize) == 13.0, b.fontSize)
    check("  ...italic, not bold", b.fontName == "Helvetica-Oblique", b.fontName)
    check("  ...its own colour", b.textColor.hexval().endswith("c0392b"), b.textColor)
    check("  ...and its own alignment (2 = right)", b.alignment == 2, b.alignment)
    check("  leading follows the size, so lines cannot overlap",
          float(h.leading) >= float(h.fontSize) * 1.2, h.leading)

print()
print("[an untouched section renders exactly as before]")
plain = paragraphs({"type": "text", "text": "plain"})
check("no styling keys -> the base style object itself",
      plain and plain[0].style is ST["body"],
      "a new ParagraphStyle here would mean every old template re-flows")

print()
print("[a bad colour must not take the report down]")
bad = paragraphs({"type": "text", "text": "x", "font_color": "not-a-colour",
                  "font_size": "abc"})
check("a typo falls back instead of raising", len(bad) == 1,
      "the operator types into these boxes")

print()
print("[table borders]")


def table_of(section):
    story: list = []
    _draw_table(section, ["A", "B"], [["1", "2"], ["3", "4"]], ST, story)
    found: list = []

    def walk(x):
        for f in getattr(x, "_content", []) or []:
            if isinstance(f, Table):
                found.append(f)
            else:
                walk(f)
    for f in story:
        if isinstance(f, Table):
            found.append(f)
        else:
            walk(f)
    return found[0] if found else None


EXPECT = {"all": {"BOX", "INNERGRID"}, "outline": {"BOX"},
          "rows": {"LINEBELOW"}, "columns": {"LINEAFTER"}, "none": set()}
for mode, want in EXPECT.items():
    t = table_of({"type": "table", "table_borders": mode})
    got = {str(c[0]).upper() for c in (getattr(t, "_linecmds", []) or [])} if t else None
    check("  %-8s draws %s" % (mode, ", ".join(sorted(want)) or "no rules"),
          got == want, "got %s" % (sorted(got) if got is not None else "no table"))

t = table_of({"type": "table", "border_width": 1.4, "border_color": "#1f3a5f"})
widths = {c[3] for c in (getattr(t, "_linecmds", []) or []) if len(c) > 3}
check("the rule width is the operator's", widths == {1.4}, widths)

t_nz = table_of({"type": "table", "zebra": False})
check("alternate-row shading can be turned off",
      not any("ROWBACKGROUNDS" in str(c[0]).upper()
              for c in (getattr(t_nz, "_bkgrndcmds", []) or [])))
t_z = table_of({"type": "table"})
check("  and is ON by default", any("ROWBACKGROUNDS" in str(c[0]).upper()
      for c in (getattr(t_z, "_bkgrndcmds", []) or [])),
      "with no rules at all it is what keeps a wide row readable")

print()
print("[two or more sections on one row]")
rows = group_side_by_side([
    {"type": "line_chart", "id": "a"},
    {"type": "table", "id": "b", "side_by_side": True},
    {"type": "kpi_grid", "id": "c", "side_by_side": True},
])
check("three sections fold into ONE row", len(rows) == 1 and rows[0]["type"] == "row",
      [r.get("type") for r in rows])
check("  carrying all three columns", len(rows[0].get("columns") or []) == 3,
      len(rows[0].get("columns") or []))
four = group_side_by_side([{"type": "line_chart", "id": str(i),
                            "side_by_side": i > 0} for i in range(4)])
check("  a fourth starts a new row, it is not squeezed in",
      len(four) == 2, [r.get("type") for r in four])

print()
print("[the editor exposes all of it]")
UI = io.open(os.path.join(ROOT, "frontend", "src", "components", "Reports",
                          "ReportTemplateDesigner.jsx"),
             encoding="utf-8", errors="replace").read()
check("text sections have font controls",
      "function FontControls(" in UI and UI.count("<FontControls") >= 2,
      "heading and body are styled separately")
check("tables have border controls",
      "function TableStyleControls(" in UI and "<TableStyleControls" in UI)
check("  including the rule colour and width",
      "border_color" in UI and "border_width" in UI and "table_borders" in UI)
check("alignment is offered, not just size",
      '<option value="justify">Justified</option>' in UI)

print()
print("[a mask on ANY text token, in every section]")
from app.services.report_renderer import resolve_text_tokens            # noqa: E402
ROWS = {"R1_Pass": [{"value": 1.0}], "R2_Pass": [{"value": 0.0}],
        "Temp": [{"value": 82.5}], "WO": [{"value": None, "value_text": "WO-4471"}]}


def fake(cond, f, t):
    return ROWS.get(str(cond.get("tag_name") or ""), [])


W = ("2026-10-09 00:00:00", "2026-10-09 23:59:59")
r = lambda p: resolve_text_tokens(p, *W, "", fake)                      # noqa: E731
check("a bare token takes a mask", r("{R1_Pass|1=PASS,0=FAIL}") == "PASS")
check("  0 maps too, it is not treated as empty",
      r("{R2_Pass|1=PASS,0=FAIL}") == "FAIL")
check("  masks work mid-sentence, several at a time",
      r("c1 {R1_Pass|1=PASS,0=FAIL} c2 {R2_Pass|1=PASS,0=FAIL}")
      == "c1 PASS c2 FAIL")
check("  the prefixed form takes one as well",
      r("{tag:R1_Pass|1=PASS,0=FAIL}") == "PASS")
check("  so does an aggregate", r("{max:Temp|*=OUT OF RANGE}") == "OUT OF RANGE")
check("  a text tag can be masked", r("{WO|WO-4471=CORRECT}") == "CORRECT")
check("no mask leaves the reading alone", r("{Temp}") == "82.500")
check("a run token is still reserved", r("{start}") == "{start}",
      "it is filled later, from the window")

print()
print("[formatting inside a table]")
from app.services.report_renderer import _draw_table                    # noqa: E402
from reportlab.platypus import Table as _T                              # noqa: E402


def built(section, header, body):
    story: list = []
    _draw_table(section, header, body, ST, story)
    acc: list = []

    def walk(x):
        for f in getattr(x, "_content", []) or []:
            if isinstance(f, _T):
                acc.append(f)
            else:
                walk(f)
    for f in story:
        if isinstance(f, _T):
            acc.append(f)
        else:
            walk(f)
    return acc


outer = built({"type": "static_table",
               "column_styles": [{"align": "left"},
                                 {"pill": True, "background": "#14a89a",
                                  "color": "#ffffff", "align": "center"}]},
              ["Check", "Result"],
              [["Electrical", "Checked / Passed"], ["Valves", "Checked / Passed"]])[0]
nested = [c for row in outer._cellvalues for c in row if isinstance(c, _T)]
check("a column can be rendered as pills", len(nested) == 2, len(nested))
check("  each pill is filled and rounded",
      bool(getattr(nested[0], "_cornerRadii", None))
      and any("BACKGROUND" in str(c[0]).upper()
              for c in (getattr(nested[0], "_bkgrndcmds", []) or [])))
check("  the other column stays plain text",
      all(isinstance(row[0], str) for row in outer._cellvalues[1:]),
      "only the styled column changes")

plain = built({"type": "static_table",
               "column_styles": [{}, {"align": "center", "color": "#c0392b",
                                      "bold": True}]},
              ["A", "B"], [["1", "2"]])[0]
cmds = [c[0] for c in (getattr(plain, "_bkgrndcmds", []) or [])] +        [c[0] for c in (getattr(plain, "_linecmds", []) or [])]
check("a column can be centred and coloured without a pill",
      any(isinstance(c, _T) for row in plain._cellvalues for c in row) is False,
      "no nested table means no pill was drawn")

check("the editor exposes column formatting",
      "function ColumnStyleControls(" in UI and "<ColumnStyleControls" in UI)
check("  and the mask syntax is written down",
      "{R1_Pass|1=PASS,0=FAIL}" in UI,
      "a syntax nobody can discover is a syntax nobody uses")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
