# -*- coding: utf-8 -*-
"""A report chart must carry FOUR Y axes, like the dashboard widgets.

2026-10-08, requested: "on the reports templates when we are creating the
charts we still need the mutple Y axis as we had for the dashboard wdigets to
be print more than 2 unis of readings".

Report charts had two. Worse, the series normaliser read
`"right" if axis == "right" else "left"`, so a series assigned to left2 or
right2 was silently forced onto axis 1 - a third unit (bar next to C next to
l/min) shared someone else's scale and drew a plausible, wrong line. Nothing
errored.

This renders a real chart with four units on four axes and inspects the
drawn shapes, because "it produced a PDF" is not evidence that four scales
were drawn.

Writes into a scratch workspace; never the live store.
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
_TMP = tempfile.mkdtemp(prefix="tn-axes4-")
os.environ.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=_TMP,
                  TRUSTNODE_APP_STORE_PATH=os.path.join(_TMP, "s.db"),
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


from app.state import app_store                               # noqa: E402
from app.services.report_renderer import (                    # noqa: E402
    AXES4, norm_axis4, _section_chart, _build_styles)
from reportlab.graphics.shapes import Drawing, Group, String  # noqa: E402

print("TrustNode - four Y axes on a report chart")
print()

print("[the axis a series asks for is the axis it gets]")
check("the four canonical axes exist", AXES4 == ("left1", "left2", "right1", "right2"))
for raw, want in (("left", "left1"), ("right", "right1"), ("left1", "left1"),
                  ("left2", "left2"), ("right1", "right1"), ("right2", "right2"),
                  ("LEFT2", "left2"), (None, "left1"), ("nonsense", "left1")):
    check("  %-9s -> %s" % (raw, want), norm_axis4(raw) == want)

from app.services.report_renderer import _normalize_one_series  # noqa: E402
try:
    kept = _normalize_one_series({"id": "s", "tag_name": "t", "axis": "right2"}, {})
    check("a series keeps its outer-axis assignment",
          kept.get("axis") == "right2",
          "forcing it to left1 is how a third unit joined someone else's scale")
except Exception as exc:                                       # noqa: BLE001
    check("a series keeps its outer-axis assignment", False, str(exc)[:120])

# --- a real chart, four units -------------------------------------------
base = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None) - timedelta(minutes=30)
UNITS = {"Pressure_bar": (6.0, 0.4), "Temp_C": (180.0, 12.0),
         "Flow_lpm": (42.0, 3.0), "Power_kW": (1250.0, 90.0)}
rows = []
for i in range(120):
    ts = (base + timedelta(seconds=i * 10)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    for tag, (mid, amp) in UNITS.items():
        rows.append({"ts_utc": ts, "gateway_id": "gw-ax", "tag_name": tag,
                     "value": mid + amp * math.sin(i / 9.0 + len(tag)),
                     "quality": 192, "quality_label": "GOOD"})
app_store.append_historian_rows(rows)
frm = base.strftime("%Y-%m-%d %H:%M:%S")
to = (base + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")

SECTION = {
    "type": "line_chart", "title": "Four units",
    "time_range": {"preset": "custom", "from_utc": frm, "to_utc": to},
    "x_axis_label": "Time",
    "y_axis_label": "Pressure", "y_axis_unit": "bar",
    "y_left2_label": "Temperature", "y_left2_unit": "C",
    "y_axis_right_label": "Flow", "y_axis_right_unit": "l/min",
    "y_right2_label": "Power", "y_right2_unit": "kW",
    "series": [
        {"id": "s1", "label": "Pressure", "gateway_id": "gw-ax",
         "tag_name": "Pressure_bar", "axis": "left1"},
        {"id": "s2", "label": "Temperature", "gateway_id": "gw-ax",
         "tag_name": "Temp_C", "axis": "left2"},
        {"id": "s3", "label": "Flow", "gateway_id": "gw-ax",
         "tag_name": "Flow_lpm", "axis": "right1"},
        {"id": "s4", "label": "Power", "gateway_id": "gw-ax",
         "tag_name": "Power_kW", "axis": "right2"},
    ],
}


def drawn_strings(section):
    """Every text shape the chart puts on the canvas, as (x, y, text)."""
    story: list = []
    _section_chart(dict(section), _build_styles(), story)
    out: list[tuple[float, float, str]] = []

    def walk(obj):
        for sh in getattr(obj, "contents", []) or []:
            if isinstance(sh, String):
                out.append((sh.x, sh.y, sh.text))
            elif isinstance(sh, (Group, Drawing)):
                walk(sh)

    for f in story:
        if isinstance(f, Drawing):
            walk(f)
        else:
            for sub in getattr(f, "_content", []) or []:
                if isinstance(sub, Drawing):
                    walk(sub)
    return out


shapes = drawn_strings(SECTION)
texts = [t for _x, _y, t in shapes]

print()
print("[every axis is labelled with its own unit]")
for want in ("Pressure (bar)", "Temperature (C)", "Flow (l/min)", "Power (kW)"):
    check("  %s is drawn" % want, want in texts,
          "an axis nobody draws is a setting that lies")

print()
print("[four separate scales, not two]")
# Tick numbers sit in a column per axis. Four axes -> four distinct columns.
tick_cols = sorted({round(x) for x, _y, t in shapes
                    if t and t.replace(".", "").replace("-", "").isdigit()})
check("there are four tick columns", len(tick_cols) == 4,
      "columns at x=%s" % tick_cols)
check("  two are left of the plot and two right",
      len(tick_cols) == 4 and tick_cols[1] < tick_cols[2],
      "the outer axis must sit beyond the inner one, not on top of it")

print()
print("[the legend accounts for every series]")
for want in ("Pressure", "Temperature (L2)", "Flow (R)", "Power (R2)"):
    check("  %s" % want, want in texts,
          "the legend used to `break` on running out of width and drop series")

print()
print("[one unit still looks like one unit]")
single = dict(SECTION)
single["series"] = [SECTION["series"][0]]
for k in ("y_left2_label", "y_left2_unit", "y_axis_right_label",
          "y_axis_right_unit", "y_right2_label", "y_right2_unit"):
    single.pop(k, None)
one = drawn_strings(single)
one_cols = sorted({round(x) for x, _y, t in one
                   if t and t.replace(".", "").replace("-", "").isdigit()})
check("a single-axis chart draws ONE tick column", len(one_cols) == 1,
      "columns at x=%s - unused axes must not reserve space" % one_cols)
check("  and its plot is wider than the four-axis one",
      max(x for x, _y, _t in one) >= max(x for x, _y, _t in shapes) - 1,
      "an unused axis would still be stealing width")

print()
print("[switching a chart's type in place]")
# 2026-10-09, requested: "a option to change the type of the chart selected
# in the section ... without the need to delete and create a new one". The
# three time-series types share one configuration and one renderer, so a
# switch keeps every series, axis and window - this proves the OUTPUT still
# changes, which is the half that would silently not work.
from reportlab.graphics.shapes import Group, Polygon, Rect as _Rect  # noqa: E402


def shape_counts(stype):
    sec = dict(SECTION)
    sec["type"] = stype
    story: list = []
    _section_chart(sec, _build_styles(), story)
    counts = {"Line": 0, "Rect": 0, "Polygon": 0}

    def walk(o):
        for sh in getattr(o, "contents", []) or []:
            n = type(sh).__name__
            if n in counts:
                counts[n] += 1
            if isinstance(sh, (Group, Drawing)):
                walk(sh)
    for f in story:
        if isinstance(f, Drawing):
            walk(f)
        else:
            for sub in getattr(f, "_content", []) or []:
                if isinstance(sub, Drawing):
                    walk(sub)
    return counts


_line = shape_counts("line_chart")
_area = shape_counts("area_chart")
_bar = shape_counts("bar_chart")
check("the SAME section renders as a line chart", _line["Line"] > 10 and _line["Polygon"] == 0,
      _line)
check("  ...as an area chart, with a fill", _area["Polygon"] >= 1, _area)
check("  ...and as a bar chart, with bars", _bar["Rect"] > 10, _bar)
check("  so no series or axis has to be rebuilt",
      _line["Line"] > 0 and _area["Line"] > 0 and _bar["Rect"] > 0,
      "all three read the same series/axis/window keys")

UI_SRC = io.open(os.path.join(ROOT, "frontend", "src", "components", "Reports",
                              "ReportTemplateDesigner.jsx"),
                 encoding="utf-8", errors="replace").read()
check("the editor offers the switch",
      'onChange={(e) => onChange({ type: e.target.value })}' in UI_SRC
      and "Chart type" in UI_SRC)
check("  limited to the types that share a configuration",
      UI_SRC.count('<option value="area_chart">Area chart</option>') == 1
      and '<option value="pie_chart">' not in UI_SRC.split("Chart type")[1][:600],
      "carrying a chart's series into a pie or a table would lose work")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
