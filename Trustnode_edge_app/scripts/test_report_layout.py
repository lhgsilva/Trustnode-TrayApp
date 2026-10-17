# -*- coding: utf-8 -*-
"""Report layout: side-by-side sections, alignment, and nothing overlapping.

2026-10-01, reported against a rendered TCU test report:
  * KPI values sat at different heights whenever a label wrapped to two lines;
  * every chart drew its legend on top of its own axis timestamps;
  * a section title could be orphaned at the foot of a page, its chart on the
    next, leaving most of a page blank;
  * configured axis labels and units never reached the chart;
  * bar charts drew the first bar straddling the Y axis;
  * there was no way to put two sections on one line.
"""
from __future__ import annotations

import io
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.environ.setdefault("TRUSTNODE_SKIP_DOTENV", "1")
_TMP = tempfile.mkdtemp(prefix="tn-layout-")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "s.db")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


from app.services.report_renderer import group_side_by_side   # noqa: E402

print("TrustNode - report layout")
print()
print("[two sections can share a line]")
secs = [{"type": "header", "title": "H"},
        {"type": "line_chart", "title": "A"},
        {"type": "bar_chart", "title": "B", "side_by_side": True},
        {"type": "table", "title": "C"}]
out = group_side_by_side(secs)
check("adjacent sections fold into one row",
      [s["type"] for s in out] == ["header", "row", "table"],
      [s["type"] for s in out])
check("  the row holds both, in order",
      [c["title"] for c in out[1]["columns"]] == ["A", "B"])
check("  the flag is cleared on the child",
      out[1]["columns"][1].get("side_by_side") is False,
      "a child that still asks to pair would re-fold forever")

three = group_side_by_side([{"type": "line_chart", "title": "A"},
                            {"type": "bar_chart", "title": "B", "side_by_side": True},
                            {"type": "table", "title": "C", "side_by_side": True},
                            {"type": "table", "title": "D", "side_by_side": True}])
check("a row takes at most three", len(three[0]["columns"]) == 3, three[0]["columns"])
check("  the fourth starts a new section", three[1]["title"] == "D", three[1])

# 2026-10-17: "header" used to be refused as the FIRST member of a row, to
# keep the report banner full width. But a header is also how a title PILL is
# written, and the operator asked for "two pill titles that are one aligned on
# the left and other on the right ... on the same row, using the feature to
# share the width of the previous". Ticking that after a title did nothing at
# all - the two titles came out stacked, with no hint why. A header is
# content, so it may now start a row; nothing pairs unless the NEXT section
# explicitly asks to.
check("a title section CAN start a row when the next one asks",
      [s["type"] for s in group_side_by_side(
          [{"type": "header"}, {"type": "table", "side_by_side": True}])]
      == ["row"],
      "two pill titles side by side is the whole point of the control")
check("  but a banner nobody asked to pair stays full width",
      [s["type"] for s in group_side_by_side(
          [{"type": "header"}, {"type": "table"}])] == ["header", "table"],
      "pairing is opt-in, per section")
check("  a page break still never starts a row",
      [s["type"] for s in group_side_by_side(
          [{"type": "page_break"}, {"type": "table", "side_by_side": True}])]
      == ["page_break", "table"],
      "a break and a spacer are not content")
check("  nor does a spacer",
      [s["type"] for s in group_side_by_side(
          [{"type": "spacer"}, {"type": "table", "side_by_side": True}])]
      == ["spacer", "table"])
check("the first section cannot pair with nothing",
      group_side_by_side([{"type": "table", "side_by_side": True}])[0]["type"] == "table")

print()
print("[the chart puts each thing in its own band]")
SRC = io.open(os.path.join(ROOT, "backend", "app", "services",
                           "report_renderer.py"), encoding="utf-8").read()
chart = SRC.split("def _section_chart", 1)[1].split("\ndef ", 1)[0]
check("ticks, legend and x title have separate rows",
      "TICK_BAND" in chart and "LEGEND_BAND" in chart and "X_TITLE_BAND" in chart,
      "they all used to be drawn relative to chart_bottom and collided")
check("  the legend sits on the legend row", "ly = legend_y" in chart)
check("  the ticks sit on the tick row", "ticks_y" in chart)
# 2026-10-08: this used to look for the local name `_unit_l`, which the
# four-axis rewrite replaced with a per-axis helper. Assert the PROPERTY -
# every axis combines its label with its unit - rather than a variable name.
check("the configured unit reaches the axis title",
      "_axis_title(" in chart and 'section.get(keys["unit"])' in chart,
      "a unit nobody draws is a setting that lies")
check("  for EVERY axis, not just the first two",
      all(k in SRC for k in ("y_axis_unit", "y_left2_unit",
                             "y_axis_right_unit", "y_right2_unit")),
      "a third unit used to share the first axis's scale silently")
check("  and is NOT glued to the top tick value",
      "_format_tick(lv, fmt) + (unit_l" not in chart,
      'it produced "31.66C" where a number belongs')
check("the x-axis keeps the YEAR, not the milliseconds",
      "xs[:19]" in chart and "[-19:]" not in chart,
      'the last 19 characters of "2026-10-01 10:10:27.538" are '
      '"-10-01 10:10:27.538"')
check("a bucketed chart stamps time like every other section",
      'strftime("%Y-%m-%dT%H:%M:%SZ")' not in SRC,
      'the ISO T...Z form made one axis read "…T15:10:00" beside another '
      'reading "… 15:10:00"')
check("bars are clamped inside the plot",
      "chart_right - bar_w - 1" in chart,
      "centred on its x the first bar straddled the Y axis")

print()
print("[nothing is orphaned from its heading]")
check("a chart travels with its title", "_emit_block(section, block, story)" in chart)
check("  and so does a table",
      "_emit_block(section, block, story)" in SRC.split("def _draw_table", 1)[1][:2500])
check("a column does NOT wrap its children",
      'if section.get("_in_column")' in SRC,
      "KeepTogether measures as infinite inside a table cell")

print()
print("[KPI figures line up across the row]")
kpi = SRC.split("def _section_kpi_grid", 1)[1].split("\ndef ", 1)[0]
check("value and label get fixed bands",
      "rowHeights=[22, 24]" in kpi,
      "a two-line label pushed its number up and broke the row")

print()
print("[an unresolved 'run' window is BOUNDED]")
from app.services.report_renderer import _resolve_time_range   # noqa: E402
_f, _t = _resolve_time_range({"preset": "run"})
check("previewing a run-window section does not scan everything",
      bool(_f) and bool(_t),
      "('', '') means NO filter: every section read the whole historian, "
      "which hangs the render on a multi-gigabyte store")
check("  'none' still means all available",
      _resolve_time_range({"preset": "none"}) == ("", ""),
      "that preset asks for it explicitly")

print()
print("[it is all editable]")
DES = io.open(os.path.join(ROOT, "frontend", "src", "components", "Reports",
                           "ReportTemplateDesigner.jsx"), encoding="utf-8").read()
check("the live PLC cycle preset is built in",
      'value: "plc_live_cycle_test"' in DES and "function plcLiveCycleTest(" in DES,
      "it must ship IN the exe, not be created by a script")
for _want in ("cycle_started1", "cycle_ended1", "cycle_started2", "cycle_ended2",
              "SimREAL[3]", "SimREAL[4]"):
    check("  it names the real tag %-16s" % _want, _want in DES)
check("  each inner cycle has its own trigger",
      DES.count("trigger: { start: plcCond(") == 2,
      "inner 1 and inner 2 are driven by their own bits")
check("  and no hard-coded gateway id",
      "gw-1790849236862" not in DES,
      "blank gateway matches on the tag alone, so the preset does not go stale")
check("the designer offers side-by-side", "side_by_side" in DES)
check("  and text alignment", "Text alignment" in DES)
check("  and axis labels / units",
      "x_axis_label" in DES and "y_axis_unit" in DES)

print()
print("[the section editor is arranged, not a wall of prose]")
import re as _re                                              # noqa: E402
CSS = io.open(os.path.join(ROOT, "frontend", "src", "styles.css"),
              encoding="utf-8", errors="replace").read()
check("the controls are grouped",
      DES.count('className="tn-subhead"') >= 3,
      "When / Trigger / Layout - they shared one two-column row before")
check("  and the group heading has a style",
      ".tn-subhead" in CSS,
      "it was used in four places and never defined, so it rendered as plain text")
# 2026-10-09: this matched the collapse rule as an exact literal, so adding
# .tn-row-5 to it failed a test about behaviour that had not changed. Assert
# that EVERY row class collapses, which is the actual requirement.
# The collapse rule lists every row class and sets ONE column. Match on the
# full single-column declaration: ".tn-row-2 { ...: 1fr 1fr }" also contains
# the substring "1fr", which is how the first attempt picked the wrong line.
_collapse = [l for l in CSS.splitlines()
             if all(("tn-row-%d" % n) in l for n in (2, 3, 4, 5))
             and "grid-template-columns: 1fr;" in l]
check("  the rows collapse on a narrow window",
      bool(_collapse),
      _collapse[0].strip() if _collapse
      else "no rule collapses every .tn-row-N to one column")

_longs = []
for _m in _re.finditer(r'<small className="muted">(.*?)</small>', DES, _re.S):
    _txt = " ".join(_m.group(1).split())
    if len(_txt) > 60:
        _longs.append(_txt[:60])
for _m in _re.finditer(r'<div className="tn-help-text">(.*?)</div>', DES, _re.S):
    _txt = " ".join(_m.group(1).split())
    if len(_txt) > 60:
        _longs.append(_txt[:60])
check("no control carries a paragraph of help",
      not _longs,
      "%d long block(s), e.g. %s" % (len(_longs), _longs[0] if _longs else ""))
check("  the long explanations moved to tooltips",
      DES.count("title=") >= 6,
      "available on hover, not occupying the form")
# 2026-10-11: each side became a LIST of conditions, so the start tag may
# live in trigger.start OR in trigger.start_conditions. The gate is the same
# - no stop picker until a start tag exists - so assert that, not the old
# one-line expression.
check("the stop trigger only appears once a start tag is chosen",
      "const hasStart = Boolean(trigger.start?.tag_name" in DES
      and "{hasStart ? (" in DES,
      "two four-field pickers were always on screen")
check("  and a start tag in EITHER shape counts",
      "trigger.start_conditions.some((c) => c?.tag_name)" in DES,
      "reading only the single dict would hide the stop picker for a "
      "trigger whose conditions are in the list")

print()
print("[KPI presentation]")
_kpi = SRC.split("def _section_kpi_grid", 1)[1].split(chr(10) + "def ", 1)[0]
check("the KPI strip is tight, not mostly air",
      '("TOPPADDING", (0, 0), (-1, -1), 3)' in _kpi
      and '("BOTTOMPADDING", (0, 0), (-1, -1), 3)' in _kpi,
      "it carried 8pt above and below on top of the value/label bands")
check("  top and bottom match", _kpi.count("(-1, -1), 3)") >= 2)
check("it can be shown as cards OR a table",
      'kpi_style' in _kpi and '== "table"' in _kpi)
check("  cards have rounded corners by default",
      "ROUNDEDCORNERS" in _kpi and 'kpi_corners' in _kpi)
check("  and square is still available", '"square"' in _kpi)
_tok = SRC.split("def apply_text_tokens", 1)[1].split(chr(10) + "def ", 1)[0]
check("a KPI label can carry a tag value",
      'it.get("label")' in _tok,
      "the description should link to a tag like any other text")
check("the designer exposes the choice",
      'kpi_style: e.target.value' in DES and 'kpi_corners: e.target.value' in DES)

print()
print("[every text field can link to a tag]")
check("there is a tag-insert control",
      "function TagTokenField(" in DES and "tn-tag-insert" in DES,
      "a {tag:...} token nobody can discover is a token nobody uses")
check("  it offers latest / avg / max / min",
      all(k in DES for k in ('value: "tag"', 'value: "avg"',
                             'value: "max"', 'value: "min"')))
check("  it inserts at the cursor, not just at the end",
      "selectionStart" in DES,
      "appending only would stop a sentence being built around the value")
_fields = DES.count("<TagTokenField")
check("  every title, subtitle and text body uses it", _fields >= 8,
      "%d field(s) - a plain <input> for a title would have no tag button" % _fields)
check("  and no plain title input is left",
      'onChange={(e) => onChange({ title: e.target.value })}' not in DES)
CSS2 = io.open(os.path.join(ROOT, "frontend", "src", "styles.css"),
               encoding="utf-8", errors="replace").read()
check("  the picker is styled and collapses on a narrow window",
      ".tn-tag-pop" in CSS2 and "grid-template-columns: 1fr;" in CSS2)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
