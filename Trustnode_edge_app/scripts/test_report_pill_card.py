# -*- coding: utf-8 -*-
"""The pill-card section, and the two value bugs found while building it.

2026-10-09, requested: "a new section that would be a pill card style that
we can format color, backgroud and aligment to, this also can be linked to a
plc tag".

A KPI grid answers "what was the number"; a pill answers "is it alright" -
PASS in green beside a work order in grey, readable at arm's length on a
printed test report.

Two faults surfaced while wiring it up, and both predate the pill:

* A text-typed tag stores its string in `value_text` and leaves `value`
  NULL, so the aggregate came back as 0.0 - not None. _fetch_kpi_value
  returned on any non-None number, so a KPI cell bound to a work order read
  "0.00" instead of "WO-4471". Zero is now the one number worth a second
  look, and the text lookup only wins if a string actually exists.
* A tag with NO rows in the window also aggregates to 0.0. The KPI grid
  checked only `value is None`, so an uncollected tag rendered a confident
  "0.00" - the same failure as an unplugged block reading as a healthy one.

Scratch workspace; the live store is never opened.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
_TMP = tempfile.mkdtemp(prefix="tn-pill-")
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
from app.state import app_store                            # noqa: E402
from app.services.report_renderer import (                 # noqa: E402
    _build_styles, _section_pill_card, _fetch_kpi_value, _draw_section)

ST = _build_styles()
BASE = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
rows = []
for i in range(30):
    ts = (BASE + timedelta(seconds=i * 10)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    rows.append({"ts_utc": ts, "gateway_id": "gw-1", "tag_name": "TCU_Temp",
                 "value": 48.0 + i * 0.1, "quality": 192, "quality_label": "GOOD"})
    rows.append({"ts_utc": ts, "gateway_id": "gw-1", "tag_name": "WorkOrder",
                 "value": None, "value_text": "WO-4471",
                 "quality": 192, "quality_label": "GOOD"})
rows.append({"ts_utc": BASE.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
             "gateway_id": "gw-1", "tag_name": "Pressure", "value": 0.0,
             "quality": 192, "quality_label": "GOOD"})
app_store.append_historian_rows(rows)
FRM = BASE.strftime("%Y-%m-%d %H:%M:%S")
TO = (BASE + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")

print("TrustNode - pill cards")
print()

print("[a value that is text, a value that is zero, a value that is absent]")
check("a TEXT tag returns its string, not 0.00",
      _fetch_kpi_value({"gateway_id": "gw-1", "tag_name": "WorkOrder",
                        "aggregation": "latest"}, FRM, TO)[2] == "WO-4471",
      "the aggregate over a NULL value column is 0.0, which hid the text")
real_zero = _fetch_kpi_value({"gateway_id": "gw-1", "tag_name": "Pressure",
                              "aggregation": "latest"}, FRM, TO)
check("  a GENUINE zero reading is still zero",
      real_zero[0] == 0.0 and real_zero[1] >= 1, real_zero)
absent = _fetch_kpi_value({"gateway_id": "gw-1", "tag_name": "Nope",
                           "aggregation": "latest"}, FRM, TO)
check("  a tag with no rows reports no samples",
      absent[1] == 0,
      "the renderer uses the sample count to print - instead of 0.00")


def pill_texts(section):
    story: list = []
    _section_pill_card(section, ST, story)
    tables = [f for f in story if isinstance(f, Table)]
    if not tables:
        return None, None
    out = []
    for row in tables[0]._cellvalues:
        for c in row:
            if isinstance(c, Paragraph):
                out.append(c.text)
    return tables[0], out


SECTION = {
    "type": "pill_card", "title": "Status", "gateway_id": "gw-1",
    "time_range": {"preset": "custom", "from_utc": FRM, "to_utc": TO},
    "align": "center", "columns": 4, "font_size": 11,
    "pills": [
        {"label": "Result", "text": "PASS", "color": "#ffffff",
         "background": "#14a89a", "bold": True},
        {"label": "Temp", "tag_name": "TCU_Temp", "aggregation": "max",
         "unit": "C", "color": "#7f1d1d", "background": "#fee2e2",
         "border_color": "#fecaca"},
        {"label": "Order", "tag_name": "WorkOrder", "color": "#0e1116",
         "background": "#eceff3"},
        {"label": "Missing", "tag_name": "NoSuchTag", "background": "#f6f8fa"},
    ],
}

print()
print("[the pills themselves]")
table, texts = pill_texts(SECTION)
check("the section renders", table is not None)
check("  a literal pill shows its text", "Result PASS" in (texts or []), texts)
check("  a tag-bound pill shows the reading with its unit",
      any(t.startswith("Temp 50.9") and t.endswith("C") for t in (texts or [])), texts)
check("  a TEXT tag shows the work order", "Order WO-4471" in (texts or []), texts)
check("  a tag with no reading shows a dash, never 0.00",
      "Missing -" in (texts or []), texts)

print()
print("[formatting]")
check("each pill keeps its own background",
      len([c for c in (getattr(table, "_bkgrndcmds", []) or [])
           if str(c[0]).upper() == "BACKGROUND"]) == 4)
check("  an outline is drawn only where asked",
      [str(c[0]).upper() for c in (getattr(table, "_linecmds", []) or [])] == ["BOX"])
check("  the section honours its alignment", table.hAlign == "CENTER", table.hAlign)
left = dict(SECTION, align="left")
t2, _ = pill_texts(left)
check("  ...and left is left", t2.hAlign == "LEFT", t2.hAlign)

print()
print("[it is a first-class section, not a special case]")
story: list = []
_draw_section(dict(SECTION), ST, story)
check("the shared dispatch draws it",
      any(isinstance(f, Table) for f in story)
      or any(isinstance(f, Table) for f in getattr(story[0], "_content", []) or []),
      "a row/column must be able to hold one too")

empty: list = []
_section_pill_card({"type": "pill_card", "pills": []}, ST, empty)
check("  an unconfigured section says so rather than drawing nothing",
      any(isinstance(f, Paragraph) and "No pills" in f.text for f in empty))

print()
print("[the editor offers it]")
UI = io.open(os.path.join(ROOT, "frontend", "src", "components", "Reports",
                          "ReportTemplateDesigner.jsx"),
             encoding="utf-8", errors="replace").read()
check("it is in the Add section list", '{ type: "pill_card", label: "Pill cards"' in UI)
check("  with colour, background and alignment controls",
      'title="Text colour"' in UI and 'title="Background"' in UI
      and 'onChange={(e) => onChange({ align: e.target.value })}' in UI)
check("  and a tag binding per pill",
      "updatePill(pill.id, { tag_name: e.target.value })" in UI)
check("  a new section starts with a usable pill",
      'case "pill_card":' in UI and '"#14a89a"' in UI)

print()
print("[value masks: a reading shown as the word it stands for]")
from app.services.report_renderer import parse_value_map, apply_value_map  # noqa: E402
m = parse_value_map("1=PASSED, 0=FAILED, *=CHECK")
check("the shorthand parses", m == {"1": "PASSED", "0": "FAILED", "*": "CHECK"}, m)
check("  a dict is accepted too",
      parse_value_map({"1": "OK"}) == {"1": "OK"})
check("1.0 matches a mask written as \"1\"",
      apply_value_map(m, 1.0, None) == "PASSED",
      "a tag reads 1.0; nobody writes the mask that way")
check("  0 is FAILED, not falsy-skipped", apply_value_map(m, 0.0, None) == "FAILED",
      "the whole point of the mask is that 0 means something")
check("  an unlisted value falls to *", apply_value_map(m, 7.0, None) == "CHECK")
check("  no mask means no masking", apply_value_map({}, 1.0, None) is None)

app_store.append_historian_rows([
    {"ts_utc": BASE.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], "gateway_id": "gw-1",
     "tag_name": "R1_Pass", "value": 1.0, "quality": 192, "quality_label": "GOOD"},
    {"ts_utc": BASE.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], "gateway_id": "gw-1",
     "tag_name": "R2_Pass", "value": 0.0, "quality": 192, "quality_label": "GOOD"}])
_t, masked = pill_texts({
    "type": "pill_card", "gateway_id": "gw-1",
    "time_range": {"preset": "custom", "from_utc": FRM, "to_utc": TO},
    "pills": [
        {"label": "Cycle 1", "tag_name": "R1_Pass", "value_map": "1=PASSED, 0=FAILED"},
        {"label": "Cycle 2", "tag_name": "R2_Pass", "value_map": "1=PASSED, 0=FAILED"},
        {"label": "Cycle 3", "tag_name": "Absent", "value_map": "1=PASSED, 0=FAILED"},
    ]})
check("a pill shows PASSED instead of 1.00", "Cycle 1 PASSED" in (masked or []), masked)
check("  and FAILED instead of 0.00", "Cycle 2 FAILED" in (masked or []), masked)
check("  a tag with NO reading is still a dash", "Cycle 3 -" in (masked or []),
      "a mask must not invent a result for data that was never recorded")

print()
print("[run tokens inside section text - the braces must not reach the page]")
from app.services.report_renderer import apply_text_tokens              # noqa: E402
tpl = apply_text_tokens({
    "name": "Functional Test Report", "id": "tpl-9",
    "definition": {"sections": [
        {"type": "header", "title": "T",
         "subtitle": "Work order {R1_Pass} - {start} to {end}",
         "time_range": {"preset": "custom", "from_utc": FRM, "to_utc": TO},
         "gateway_id": "gw-1"},
        {"type": "static_table", "header": ["Field", "{template}"],
         "rows": [["Window", "{start}"], ["Serial", "{R1_Pass}"]],
         "time_range": {"preset": "custom", "from_utc": FRM, "to_utc": TO},
         "gateway_id": "gw-1"},
    ]}})
sub = tpl["definition"]["sections"][0]["subtitle"]
check("{start} / {end} are filled in a subtitle", "{" not in sub, sub)
check("  with a readable timestamp, not the file-name form", ":" in sub, sub)
st = tpl["definition"]["sections"][1]
check("a details-table CELL resolves a tag", "{" not in st["rows"][1][1], st["rows"][1])
check("  and a run token", "{" not in st["rows"][0][1], st["rows"][0])
check("  and so does its header", "{" not in st["header"][1], st["header"])

print()
print("[the editor can build a details table and pick a gateway]")
check("a details table is in the Add section list",
      '{ type: "static_table", label: "Details table"' in UI,
      "it existed in preset templates but could not be created or edited")
check("  its cells are editable", "const setCell = (r, c, v) =>" in UI)
check("  rows and columns can be added", "addDetailColumn" in UI and "addRow" in UI)
check("the test summary table can choose a gateway",
      UI.count("onChange={(e) => onChange({ gateway_id: e.target.value })}") >= 1
      and "(section default)" in UI,
      "without one, tagList() was always empty and no tag could be selected")
check("  a pill can carry a mask", 'placeholder="1=PASS, 0=FAIL"' in UI)

print()
print("[the banner is text too - it must be styleable]")
from app.services.report_renderer import _section_header                # noqa: E402
hs: list = []
_section_header({"type": "header", "title": "Functional Test Report",
                 "subtitle": "Line two", "title_align": "center",
                 "title_font_size": 28, "title_font_color": "#14a89a",
                 "subtitle_align": "center", "subtitle_italic": True,
                 "stamp_align": "right"}, ST, hs)
hp = [f for f in hs if isinstance(f, Paragraph)]
check("the banner renders title, subtitle and stamp", len(hp) == 3, len(hp))
if len(hp) == 3:
    check("  the title takes its size and centring",
          float(hp[0].style.fontSize) == 28.0 and hp[0].style.alignment == 1)
    check("  the subtitle is styled independently",
          hp[1].style.alignment == 1 and "Oblique" in hp[1].style.fontName,
          hp[1].style.fontName)
    check("  and so is the generated-at stamp", hp[2].style.alignment == 2,
          hp[2].style.alignment)

print()
print("[pill corners]")
WANT = {"both": 4, "left": 2, "right": 2, "top": 2, "bottom": 2, "none": 0}
for mode, rounded in WANT.items():
    st2: list = []
    _section_pill_card({"type": "pill_card", "pill_corners": mode,
                        "font_size": 10,
                        "pills": [{"label": "A", "text": "1",
                                   "background": "#14a89a"}]}, ST, st2)
    tb = [f for f in st2 if isinstance(f, Table)][0]
    radii = getattr(tb, "_cornerRadii", None) or []
    check("  %-6s rounds %d corner(s)" % (mode, rounded),
          len([r for r in radii if r]) == rounded, radii)

print()
print("[a pill used as a section header]")
hdr: list = []
_section_pill_card({"type": "pill_card", "as_header": True, "font_size": 10,
                    "align": "left",
                    "pills": [{"label": "SECTION", "text": "Cycle results",
                               "background": "#1f3a5f"}]}, ST, hdr)
ht = [f for f in hdr if isinstance(f, Table)][0]
hc = [c for row in ht._cellvalues for c in row if isinstance(c, Paragraph)][0]
check("a header band is ONE full-width cell", len(ht._cellvalues[0]) == 1)
check("  with a larger, bold face", float(hc.style.fontSize) > 10.0
      and "Bold" in hc.style.fontName, hc.style.fontName)
check("  reading left, like a heading, not centred like a badge",
      hc.style.alignment == 0, hc.style.alignment)

print()
print("[an unset colour matches the swatch the editor shows]")
ink: list = []
_section_pill_card({"type": "pill_card",
                    "pills": [{"label": "A", "text": "1",
                               "background": "#14a89a"}]}, ST, ink)
it = [f for f in ink if isinstance(f, Table)][0]
ic = [c for row in it._cellvalues for c in row if isinstance(c, Paragraph)][0]
check("text on a filled pill defaults to white",
      ic.style.textColor.hexval().endswith("ffffff"), ic.style.textColor)
nobg: list = []
_section_pill_card({"type": "pill_card",
                    "pills": [{"label": "A", "text": "1"}]}, ST, nobg)
nt = [f for f in nobg if isinstance(f, Table)][0]
nc = [c for row in nt._cellvalues for c in row if isinstance(c, Paragraph)][0]
check("  but NOT when there is no fill behind it",
      not nc.style.textColor.hexval().endswith("ffffff"),
      "white on white would be invisible")

print()
print("[every section can share a row]")
check("the layout control is one shared component",
      "function SectionLayout(" in UI,
      "it lived inline in the chart/table branch only")
check("  used by every content section",
      UI.count("<SectionLayout") >= 9,
      "%d use site(s)" % UI.count("<SectionLayout"))
check("  and it warns when the line above cannot be joined",
      "CANNOT_LEAD" in UI and "always takes a" in UI,
      "a banner, spacer or page break is never a partner")
check("the banner has font controls now",
      UI.count("<FontControls") >= 5,
      "title, subtitle and stamp, plus the text block's two")
check("pill corners and the header toggle are offered",
      "Rounded left only" in UI and "Use as a section header" in UI)

print()
print("[the heading ITSELF as a pill]")
from app.services.report_renderer import pill_band                      # noqa: E402
hp: list = []
_section_header({"type": "header", "title": "INITIAL TEST", "as_pill": True,
                 "pill_background": "#14a89a", "title_font_size": 14,
                 "title_align": "left", "show_generated_at": False}, ST, hp)
bands = [f for f in hp if isinstance(f, Table)]
paras = [f for f in hp if isinstance(f, Paragraph)]
check("the heading renders as ONE band", len(bands) == 1, len(bands))
check("  and NOTHING is drawn above it", len(paras) == 0,
      "a heading plus a band is the duplication in the screenshot")
if bands:
    cell = [c for row in bands[0]._cellvalues for c in row
            if isinstance(c, Paragraph)][0]
    check("  the title text is INSIDE the pill", cell.text == "INITIAL TEST",
          cell.text)
    check("  it is filled and rounded",
          any("BACKGROUND" in str(c[0]).upper()
              for c in (getattr(bands[0], "_bkgrndcmds", []) or []))
          and any(getattr(bands[0], "_cornerRadii", []) or []))

tok = apply_text_tokens({"name": "T", "definition": {"sections": [
    {"type": "header", "title": "{WorkOrder}", "as_pill": True,
     "gateway_id": "gw-1", "show_generated_at": False,
     "time_range": {"preset": "custom", "from_utc": FRM, "to_utc": TO}}]}})
hp2: list = []
_section_header(tok["definition"]["sections"][0], ST, hp2)
b2 = [f for f in hp2 if isinstance(f, Table)][0]
c2 = [c for row in b2._cellvalues for c in row if isinstance(c, Paragraph)][0]
check("a pill heading can be LINKED TO A TAG", c2.text == "WO-4471", c2.text)

plain: list = []
_section_header({"type": "header", "title": "Plain", "show_generated_at": False},
                ST, plain)
check("  and a plain heading is still a plain heading",
      not [f for f in plain if isinstance(f, Table)],
      "as_pill is opt-in; every existing template is untouched")

print()
print("[a pill SECTION used as a header does not also draw a heading]")
dup: list = []
_section_pill_card({"type": "pill_card", "title": "INITIAL TEST",
                    "as_header": True,
                    "pills": [{"label": "Result", "text": "PASS",
                               "background": "#14a89a"}]}, ST, dup)
check("only the band is drawn",
      len([f for f in dup if isinstance(f, Paragraph)]) == 0,
      "the screenshot showed INITIAL TEST sitting above a separate pill")

print()
print("[the editor offers it]")
check("the header can be shown as a pill",
      '<option value="pill">Pill band</option>' in UI)
check("  with its own band and text colours",
      "pill_background" in UI and "pill_color" in UI)
check("  and the corner choice", UI.count("Rounded left only") >= 2,
      "the header and the pill card each offer it")

print()
print("[a pill can sit on the RIGHT of the page]")
# 2026-10-17, reported: "1 pill per row but this pill can be in the right of
# the full width row - at the moment they stay only on the left".
#
# They could not move: the grid stretches each pill to avail/cols, so with
# one column the pill IS the whole page width and hAlign has nothing to
# position. Fitting to content is what gives alignment something to do.
def _pill_table(section):
    story = []
    _section_pill_card(dict(section), _build_styles(), story)
    from reportlab.platypus import Table as _T
    for f in story:
        if isinstance(f, _T):
            return f
    return None


ONE = {"type": "pill_card", "title": "", "columns": 1,
       "pills": [{"id": "p1", "text": "PASSED"}]}

fill = _pill_table(ONE)
check("the default still fills the row", fill is not None and fill.hAlign == "LEFT",
      getattr(fill, "hAlign", None))

right = _pill_table(dict(ONE, pill_fit="content", align="right"))
check("fit-to-content + right puts the pill on the right",
      right is not None and right.hAlign == "RIGHT", getattr(right, "hAlign", None))
check("  and it is no longer stretched to the full width",
      right is not None and right._argW != fill._argW,
      "colWidths must be content-sized, not avail/cols")
centre = _pill_table(dict(ONE, pill_fit="content", align="center"))
check("  centre works too",
      centre is not None and centre.hAlign == "CENTER", getattr(centre, "hAlign", None))

hdr = _pill_table({"type": "pill_card", "as_header": True, "align": "right",
                   "pill_fit": "content", "pills": [{"id": "h", "text": "TITLE"}]})
check("a pill HEADER can be right-aligned too",
      hdr is not None and hdr.hAlign == "RIGHT",
      "as_header used to force LEFT whatever the operator chose")

check("an unset alignment is still LEFT",
      _pill_table(dict(ONE, pill_fit="content")).hAlign == "LEFT",
      "existing templates must not move")

SRC_R = io.open(os.path.join(ROOT, "backend", "app", "services",
                             "report_renderer.py"), encoding="utf-8").read()
check("pill_band no longer hardcodes its own position",
      "band_align" in SRC_R,
      "align only moved the TEXT inside the pill before; where the band "
      "sits is now the caller's decision")

DES_P = io.open(os.path.join(ROOT, "frontend", "src", "components", "Reports",
                             "ReportTemplateDesigner.jsx"), encoding="utf-8").read()
check("the editor offers Pill width", "pill_fit: e.target.value" in DES_P)
check("  and Alignment", "align: e.target.value" in DES_P)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
