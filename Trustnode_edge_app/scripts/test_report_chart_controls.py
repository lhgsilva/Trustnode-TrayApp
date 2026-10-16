# -*- coding: utf-8 -*-
"""Tick count, line thickness, legend alignment and thin-data charts.

2026-10-11, requested: "we should be able to edit axis, labels and units to
be printed on the the charts respecting spaces, makking sure they are not
overlaping texts... thickness of lines in th charts to be changable... all
charts we should be able to change the min and max for axis and ticks too...
legengs labels to be editables and also aligments to be changed too" and "we
faced some problems when the chart for some reason was not printed in the pdf
for not enought data etc, but we need to garantee that will work".

What was wrong:
  * the Y scale was divided into exactly 5 intervals, in two hardcoded loops;
  * every line was drawn at strokeWidth=1.4, with no way to change it;
  * the legend always began hard against the Y axis;
  * the tick gutter was a fixed 34pt, so a wide scale (1013.250, or a
    negative number) ran its labels into the rotated axis title;
  * a series with ONE sample drew nothing at all - the segment loop starts at
    index 1 - which is one of the literal "not printed for not enough data"
    cases;
  * four different causes of an empty chart printed the same sentence, so a
    mis-set trigger could not be told from a tag that is not being collected.

Renders real charts and inspects the drawn shapes. Writes into a scratch
workspace; never the live store.
"""
from __future__ import annotations

import io
import math
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
_TMP = tempfile.mkdtemp(prefix="tn-chartctl-")
os.environ.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=_TMP,
                  TRUSTNODE_APP_STORE_PATH=os.path.join(_TMP, "s.db"),
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:160]) if detail else ""))
    if not ok:
        FAILS.append(name)


from app.state import app_store                                  # noqa: E402
from app.services.report_renderer import (                       # noqa: E402
    _section_chart, _build_styles, _empty_chart_reason, _NEVER)
from reportlab.graphics.shapes import (                          # noqa: E402
    Circle, Drawing, Group, Line, Rect, String)

print("TrustNode - chart ticks, thickness, legend and thin data")
print()

# --- one wide-scale series and one narrow one ---------------------------
base = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None) - timedelta(minutes=30)
rows = []
for i in range(90):
    ts = (base + timedelta(seconds=i * 20)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    rows.append({"ts_utc": ts, "gateway_id": "gw-c", "tag_name": "Pressure_mbar",
                 "value": 1013.25 + 40.0 * math.sin(i / 7.0),
                 "quality": 192, "quality_label": "GOOD"})
    rows.append({"ts_utc": ts, "gateway_id": "gw-c", "tag_name": "Temp_C",
                 "value": 21.0 + 3.0 * math.cos(i / 5.0),
                 "quality": 192, "quality_label": "GOOD"})
# A tag with exactly ONE reading, inside the same window.
rows.append({"ts_utc": (base + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
             "gateway_id": "gw-c", "tag_name": "Lone_Sample", "value": 7.5,
             "quality": 192, "quality_label": "GOOD"})
app_store.append_historian_rows(rows)
frm = base.strftime("%Y-%m-%d %H:%M:%S")
to = (base + timedelta(minutes=35)).strftime("%Y-%m-%d %H:%M:%S")
WINDOW = {"preset": "custom", "from_utc": frm, "to_utc": to}

SECTION = {
    "type": "line_chart", "title": "Controls", "time_range": dict(WINDOW),
    "x_axis_label": "Time", "value_format": "3dp",
    "y_axis_label": "Pressure", "y_axis_unit": "mbar",
    "y_axis_right_label": "Temperature", "y_axis_right_unit": "C",
    "series": [
        {"id": "s1", "label": "Pressure", "gateway_id": "gw-c",
         "tag_name": "Pressure_mbar", "axis": "left1"},
        {"id": "s2", "label": "Temperature", "gateway_id": "gw-c",
         "tag_name": "Temp_C", "axis": "right1"},
    ],
}


def shapes_of(section):
    """Every shape the chart draws, flattened out of its groups."""
    story: list = []
    _section_chart(dict(section), _build_styles(), story)
    out: list = []

    def walk(obj):
        for sh in getattr(obj, "contents", []) or []:
            if isinstance(sh, (Group, Drawing)):
                walk(sh)
            else:
                out.append(sh)

    for f in story:
        if isinstance(f, Drawing):
            walk(f)
        else:
            for sub in getattr(f, "_content", []) or []:
                if isinstance(sub, Drawing):
                    walk(sub)
    return out


def paragraph_texts(section):
    story: list = []
    _section_chart(dict(section), _build_styles(), story)
    out: list[str] = []

    def walk(f):
        if hasattr(f, "text"):
            out.append(str(f.text))
        for sub in getattr(f, "_content", []) or []:
            walk(sub)

    for f in story:
        walk(f)
    return out


def tick_labels(section):
    """{x column: how many numeric labels} for the drawn tick scales."""
    cols: dict[int, int] = {}
    for sh in shapes_of(section):
        if isinstance(sh, String) and sh.fontSize == 7:
            txt = str(sh.text).replace("-", "").replace(".", "")
            if txt.isdigit():
                key = int(round(sh.x))
                cols[key] = cols.get(key, 0) + 1
    return cols


def tick_columns(section):
    return sorted(tick_labels(section))


print("[the Y scale is divided into as many ticks as asked for]")
for count in (2, 3, 5, 8, 12):
    got = sorted(tick_labels(dict(SECTION, y_tick_count=count)).values())
    # Two drawn axes, each labelled at every tick boundary -> count + 1.
    check("  %2d ticks -> %d labels per axis" % (count, count + 1),
          bool(got) and all(n == count + 1 for n in got),
          "hardcoded `range(0, 6)` gave every chart exactly 6: %s" % (got,))

grid5 = [s for s in shapes_of(dict(SECTION, y_tick_count=5))
         if isinstance(s, Line) and s.strokeDashArray]
grid10 = [s for s in shapes_of(dict(SECTION, y_tick_count=10))
          if isinstance(s, Line) and s.strokeDashArray]
check("  the GRID lines follow the same count", len(grid10) > len(grid5),
      "%d vs %d - ONE count per chart is why labels cannot land between "
      "another axis's grid lines" % (len(grid5), len(grid10)))
check("  a silly count is clamped, not obeyed",
      len(tick_columns(dict(SECTION, y_tick_count=400))) == 2
      and len(tick_columns(dict(SECTION, y_tick_count=0))) == 2,
      "a 400-tick axis is a solid black bar")

print()
print("[tick labels get the room they actually need]")
# "1013.250" is far wider than "1013"; the gutter used to be 34pt for both.
wide = tick_columns(dict(SECTION, value_format="3dp"))
narrow = tick_columns(dict(SECTION, value_format="int"))
check("a wide scale reserves a wider gutter", wide[0] > narrow[0],
      "left tick column at x=%s (3dp) vs x=%s (int)" % (wide[0], narrow[0]))
check("  the gutter is at least as wide as the widest label", wide[0] >= 20.0, wide[0])
check("  and the plot is narrower for it",
      max(s.width for s in shapes_of(dict(SECTION, value_format="3dp"))
          if isinstance(s, Rect) and s.width > 100)
      < max(s.width for s in shapes_of(dict(SECTION, value_format="int"))
            if isinstance(s, Rect) and s.width > 100),
      "reserving space it does not use would be the opposite mistake")
check("  the rotated axis title is still drawn",
      any(isinstance(s, String) and str(s.text) == "Pressure (mbar)"
          for s in shapes_of(dict(SECTION, value_format="3dp"))))

print()
print("[line thickness]")


def widths(section):
    return sorted({round(s.strokeWidth, 2) for s in shapes_of(section)
                   if isinstance(s, Line) and not s.strokeDashArray
                   and s.strokeWidth and s.strokeWidth > 0.5})


check("the default is unchanged at 1.4pt", widths(SECTION) == [1.4], widths(SECTION))
check("  the section can set them all", widths(dict(SECTION, line_width=3)) == [3.0],
      widths(dict(SECTION, line_width=3)))
mixed = dict(SECTION, line_width=3)
mixed["series"] = [dict(SECTION["series"][0], line_width=0.6),
                   dict(SECTION["series"][1])]
check("  and one series can differ", widths(mixed) == [0.6, 3.0],
      "a thin setpoint under a thick measurement: %s" % (widths(mixed),))
check("  a nonsense thickness is clamped",
      widths(dict(SECTION, line_width=99)) == [6.0]
      and widths(dict(SECTION, line_width="abc")) == [1.4],
      "%s / %s" % (widths(dict(SECTION, line_width=99)),
                   widths(dict(SECTION, line_width="abc"))))

print()
print("[legend alignment]")


def legend_x(section):
    """x of the leftmost legend swatch - an 8x8 Rect."""
    xs = [s.x for s in shapes_of(section)
          if isinstance(s, Rect) and round(s.width) == 8 and round(s.height) == 8]
    return round(min(xs), 1) if xs else None


lx_left = legend_x(dict(SECTION, legend_align="left"))
lx_mid = legend_x(dict(SECTION, legend_align="center"))
lx_right = legend_x(dict(SECTION, legend_align="right"))
check("a legend is drawn at all", lx_left is not None)
check("  left is the default", legend_x(SECTION) == lx_left, (legend_x(SECTION), lx_left))
check("  centred moves it right of left", bool(lx_mid and lx_mid > lx_left),
      (lx_left, lx_mid))
check("  right-aligned moves it further still", bool(lx_right and lx_right > lx_mid),
      (lx_mid, lx_right))
check("  an unknown value falls back to left",
      legend_x(dict(SECTION, legend_align="sideways")) == lx_left)
check("  the label is the series' own text",
      any(isinstance(s, String) and str(s.text).startswith("Pressure")
          for s in shapes_of(SECTION)))
relabel = dict(SECTION)
relabel["series"] = [dict(SECTION["series"][0], label="P1 inlet"),
                     dict(SECTION["series"][1])]
check("    so renaming a series renames its legend entry",
      any(isinstance(s, String) and "P1 inlet" in str(s.text)
          for s in shapes_of(relabel)))

print()
print("[a chart with barely any data still draws something]")
lone = dict(SECTION, title="One sample")
lone["series"] = [{"id": "s1", "label": "Lone", "gateway_id": "gw-c",
                   "tag_name": "Lone_Sample", "axis": "left1"}]
lone_shapes = shapes_of(lone)
dots = [s for s in lone_shapes if isinstance(s, Circle)]
check("a single reading is marked", len(dots) == 1,
      "the segment loop starts at index 1, so one point drew NOTHING: "
      "%d circle(s)" % len(dots))
if dots:
    plot = [s for s in lone_shapes if isinstance(s, Rect) and s.width > 100]
    inside = bool(plot) and (plot[0].x < dots[0].cx < plot[0].x + plot[0].width)
    check("  and sits inside the plot, not on the Y axis", inside,
          "cx=%.1f plot x=%.1f..%.1f" % (
              dots[0].cx, plot[0].x, plot[0].x + plot[0].width) if plot else "no plot")
caps = paragraph_texts(lone)
check("  the caption says the trend is thin",
      any("too few readings" in c for c in caps),
      "one dot looks like a working chart otherwise: %s" % (caps,))

print()
print("[an empty chart says WHY it is empty]")
check("no series at all",
      "No series configured" in _empty_chart_reason({"series": []}, []))
check("  a series with no tag chosen",
      "No tag selected" in _empty_chart_reason({"series": [{"id": "s"}]}, []))
check("  a trigger that never became true",
      "trigger never became true" in _empty_chart_reason(
          {"series": [{"tag_name": "T"}],
           "time_range": {"from_utc": _NEVER, "to_utc": _NEVER}}, []))
reason = _empty_chart_reason(
    {"series": [{"tag_name": "T"}],
     "time_range": {"from_utc": frm, "to_utc": to}}, [])
check("  otherwise it names the window it looked in",
      "No readings stored" in reason and frm[:16] in reason, reason)
nothing = dict(SECTION, time_range=dict(WINDOW))
nothing["series"] = [{"id": "s1", "gateway_id": "gw-c",
                      "tag_name": "Tag_That_Does_Not_Exist", "axis": "left1"}]
check("  and a real render uses it",
      any("No readings stored" in c for c in paragraph_texts(nothing)),
      paragraph_texts(nothing))

print()
print("[a section whose trigger fell back says so]")
fell = dict(SECTION, _trigger_fell_back=True)
check("the caption admits the window is not the section's",
      any("section trigger did not fire" in c for c in paragraph_texts(fell)),
      "charting the report's window under a section titled 'Heating phase' "
      "without saying so is a wrong report, not a fallback")
check("  and an ordinary section says nothing of the kind",
      not any("did not fire" in c for c in paragraph_texts(SECTION)))

print()
print("[the timestamp X axis can be hidden]")


def x_texts(section):
    return [str(s_.text) for s_ in shapes_of(section)
            if isinstance(s_, String)]


on = x_texts(SECTION)
off = x_texts(dict(SECTION, show_x_axis=False))
check("by default the X-axis title is drawn", "Time" in on)
check("  and timestamps are drawn",
      any(t.count(":") == 2 and t[:2].isdigit() is False or "-" in t for t in on),
      "two stamps sit under the plot")
check("show_x_axis=false removes the X-axis title", "Time" not in off, off[:6])
check("  and removes the timestamps",
      len([t for t in off if "-" in t and ":" in t]) == 0,
      [t for t in off if "-" in t and ":" in t])
check("  the series are still drawn",
      len([s_ for s_ in shapes_of(dict(SECTION, show_x_axis=False))
           if isinstance(s_, Line) and not s_.strokeDashArray]) > 10,
      "hiding an axis must not hide the data")


def drawing_height(section):
    """Total height of the emitted Drawing. The plot itself is a fixed 72mm
    by design - the canvas GROWS to fit the bands under it - so hiding the
    axis gives the space back to the PAGE, not to the plot."""
    story = []
    _section_chart(dict(section), _build_styles(), story)
    for f in story:
        if isinstance(f, Drawing):
            return f.height
        for sub in getattr(f, "_content", []) or []:
            if isinstance(sub, Drawing):
                return sub.height
    return 0


_on_h = drawing_height(SECTION)
_off_h = drawing_height(dict(SECTION, show_x_axis=False))
check("  and the section gives the space back to the page",
      _off_h < _on_h,
      "%.0f -> %.0fpt: the plot is a fixed 72mm, so the saving is vertical "
      "space in the report, not a bigger chart" % (_on_h, _off_h))
# BOTH bands must go: the tick row AND the axis title. Losing only one
# still shrinks the canvas, so a loose "it got smaller" assertion passes
# while half the feature is broken.
check("  BOTH the tick row and the title band are reclaimed",
      (_on_h - _off_h) >= 20,
      "%.0fpt freed; expected ~23 (11pt ticks + 12pt title). Less than that "
      "means one band is still reserved." % (_on_h - _off_h))
check("  and no more than that",
      (_on_h - _off_h) < 40,
      "%.0fpt - the plot is fixed at 72mm, only the bands are saved"
      % (_on_h - _off_h))
check("  the editor offers the switch",
      "onChange={(e) => onChange({ show_x_axis: e.target.checked })}" in io.open(
          os.path.join(ROOT, "frontend", "src", "components", "Reports",
                       "ReportTemplateDesigner.jsx"), encoding="utf-8").read(),
      "a renderer option with no checkbox is an option nobody can set")
check("  the legend still works with it off",
      legend_x(dict(SECTION, show_x_axis=False)) is not None)

print()
print("[nothing that already worked has changed]")
base_shapes = shapes_of(SECTION)
check("both axes are still drawn", len(tick_columns(SECTION)) == 2,
      tick_columns(SECTION))
check("  explicit min/max still apply",
      len([s for s in shapes_of(dict(SECTION, y_min=900, y_max=1100))
           if isinstance(s, String) and str(s.text) == "900.000"]) == 1,
      "the Min/Max fields that already existed")
check("  the x-axis title is still centred under the plot",
      any(isinstance(s, String) and str(s.text) == "Time"
          and s.textAnchor == "middle" for s in base_shapes))
check("  limit lines still draw",
      len([s for s in shapes_of(dict(SECTION, limit_lines=[
          {"value": 1020, "axis": "left1", "label": "Max", "color": "#dc2626"}]))
           if isinstance(s, Line) and s.strokeDashArray == [3, 3]]) == 1)
check("  show_legend=false still hides it",
      legend_x(dict(SECTION, show_legend=False)) is None)
check("  an area chart still fills",
      any(type(s).__name__ == "Polygon" for s in shapes_of(dict(SECTION, type="area_chart"))))
check("  a bar chart still draws bars",
      len([s for s in shapes_of(dict(SECTION, type="bar_chart"))
           if isinstance(s, Rect)]) > 50)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
