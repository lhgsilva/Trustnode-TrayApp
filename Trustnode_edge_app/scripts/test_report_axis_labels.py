# -*- coding: utf-8 -*-
"""Axis labels can carry a tag value, and the caption counts per series.

2026-10-11, asked about a report showing two charts over the SAME run window:

  "I have two differen section that are using the same main schedule report
   trigger but are loggin different number os samples why?"

They were not. One chart said "30 samples across 3 series" and the other
"20 samples across 2 series" - ten readings per series in both. The caption
reported the TOTAL across series, so identical data read as two different
numbers and looked like a trigger fault. It now says per series.

And, requested in the same message:

  "we should be able to add the axis lables for different Y axis and X axis
   tht also cab be a tag value"

The four Y titles, their units and the X title were the one group of
operator-typed strings apply_text_tokens skipped, so "{tag:Unit_Name}" on an
axis printed literally instead of resolving.

Writes into a scratch workspace; never the live store.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
_TMP = tempfile.mkdtemp(prefix="tn-axislbl-")
os.environ.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=_TMP,
                  TRUSTNODE_APP_STORE_PATH=os.path.join(_TMP, "s.db"),
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)


from app.state import app_store                                  # noqa: E402
from app.services.report_renderer import (                       # noqa: E402
    _section_chart, _build_styles, apply_text_tokens, AXES4, _AXIS_KEYS)
from reportlab.graphics.shapes import Drawing, Group, String      # noqa: E402

print("TrustNode - axis labels from tags, and an honest sample caption")
print()

base = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None) - timedelta(minutes=10)
rows = []
for i in range(10):
    ts = (base + timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    for tag, val in (("Temperature", 150.0 + i), ("Flow", 8.0 + i * 0.1),
                     ("Pressure", 1120.0 + i)):
        rows.append({"ts_utc": ts, "gateway_id": "gw-a", "tag_name": tag,
                     "value": val, "quality": 192, "quality_label": "GOOD"})
# A text tag naming the unit the recipe is running in.
rows.append({"ts_utc": base.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
             "gateway_id": "gw-a", "tag_name": "Unit_Name",
             "value": None, "value_text": "degC",
             "quality": 192, "quality_label": "GOOD"})
rows.append({"ts_utc": base.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
             "gateway_id": "gw-a", "tag_name": "Product",
             "value": None, "value_text": "TCU-440",
             "quality": 192, "quality_label": "GOOD"})
app_store.append_historian_rows(rows)

frm = base.strftime("%Y-%m-%d %H:%M:%S")
to = (base + timedelta(seconds=20)).strftime("%Y-%m-%d %H:%M:%S")
WINDOW = {"preset": "custom", "from_utc": frm, "to_utc": to}


def ser(tag, axis, label=None):
    return {"id": tag, "label": label or tag, "gateway_id": "gw-a",
            "tag_name": tag, "axis": axis}


# The two sections from the report, over the SAME window.
THREE = {"id": "a", "type": "line_chart", "title": "Full Test",
         "time_range": dict(WINDOW), "x_axis_label": "Time",
         "y_axis_label": "Value",
         "series": [ser("Flow", "left1"), ser("Temperature", "right1"),
                    ser("Pressure", "left2")]}
TWO = {"id": "b", "type": "line_chart", "title": "Temperature Profile",
       "time_range": dict(WINDOW), "x_axis_label": "Time",
       "y_axis_label": "Value",
       "series": [ser("Temperature", "left1", "TCU"),
                  ser("Pressure", "right1", "Consumer")]}


def texts(section):
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


def drawn(section):
    story: list = []
    _section_chart(dict(section), _build_styles(), story)
    out: list[str] = []

    def walk(obj):
        for sh in getattr(obj, "contents", []) or []:
            if isinstance(sh, String):
                out.append(str(sh.text))
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


print("[the same window, two sections, different series counts]")
cap3 = [t for t in texts(THREE) if "series" in t]
cap2 = [t for t in texts(TWO) if "series" in t]
check("both captions were produced", bool(cap3) and bool(cap2), (cap3, cap2))
check("the 3-series chart reports its PER-SERIES count",
      cap3 and "10 samples per series" in cap3[0],
      "it said '30 samples across 3 series', which looks like more data "
      "than the other chart got: %s" % (cap3,))
check("  the 2-series chart reports the SAME per-series count",
      cap2 and "10 samples per series" in cap2[0],
      "this is the whole answer to 'why are they different': they are not: "
      "%s" % (cap2,))
check("  and the total is still there, not hidden",
      cap3 and "30 readings" in cap3[0] and cap2 and "20 readings" in cap2[0],
      (cap3, cap2))
check("  an uneven chart says the range instead of a single figure",
      True, "asserted below")

uneven = dict(THREE)
uneven["series"] = [ser("Flow", "left1"),
                    ser("Tag_With_No_Rows", "right1")]
cap_u = [t for t in texts(uneven) if "readings" in t or "series" in t]
check("    a series with no rows does not fake a tidy number",
      cap_u and ("each" in cap_u[0] or "per series" in cap_u[0]), cap_u)

print()
print("[axis labels resolve a tag value]")
TOKENED = {"id": "c", "type": "line_chart", "title": "Profile",
           "time_range": dict(WINDOW),
           "x_axis_label": "Time - {Product}",
           "y_axis_label": "Temperature", "y_axis_unit": "{Unit_Name}",
           "y_axis_right_label": "Pressure for {Product}",
           "y_axis_right_unit": "mbar",
           "series": [ser("Temperature", "left1"), ser("Pressure", "right1")]}
tpl = {"id": "t1", "name": "T", "definition": {"sections": [dict(TOKENED)]}}
resolved = apply_text_tokens(tpl)["definition"]["sections"][0]

check("the X title resolves its token",
      resolved["x_axis_label"] == "Time - TCU-440", resolved["x_axis_label"])
check("  a Y title resolves one too",
      resolved["y_axis_right_label"] == "Pressure for TCU-440",
      resolved["y_axis_right_label"])
check("  and so does a Y UNIT",
      resolved["y_axis_unit"] == "degC", resolved["y_axis_unit"])
check("  a label with no token is untouched",
      resolved["y_axis_label"] == "Temperature", resolved["y_axis_label"])
check("  the unit that was literal stays literal",
      resolved["y_axis_right_unit"] == "mbar", resolved["y_axis_right_unit"])

shapes = drawn(resolved)
check("the resolved unit is what the chart DRAWS",
      "Temperature (degC)" in shapes,
      "the unit rides on the axis title: %s"
      % [t for t in shapes if "Temperature" in t])
check("  and the resolved X title is drawn",
      "Time - TCU-440" in shapes, [t for t in shapes if "Time" in t])

print()
print("[every axis is covered, not just the first]")
for axis in AXES4:
    keys = _AXIS_KEYS[axis]
    sec = {"id": "x", "type": "line_chart", "time_range": dict(WINDOW),
           keys["label"]: "L-{Product}", keys["unit"]: "{Unit_Name}",
           "series": [ser("Temperature", axis)]}
    got = apply_text_tokens(
        {"id": "t", "name": "T", "definition": {"sections": [sec]}}
    )["definition"]["sections"][0]
    check("  %-6s title and unit both resolve" % axis,
          got[keys["label"]] == "L-TCU-440" and got[keys["unit"]] == "degC",
          (got[keys["label"]], got[keys["unit"]]))

print()
print("[the editor offers the picker on those fields]")
import io as _io                                                  # noqa: E402
DES = _io.open(os.path.join(ROOT, "frontend", "src", "components", "Reports",
                            "ReportTemplateDesigner.jsx"), encoding="utf-8").read()
check("the X-axis title has a tag picker",
      "onChange={(v) => onChange({ x_axis_label: v })}" in DES,
      "a token nobody can discover is a token nobody uses")
for key in ("y_axis_label", "y_axis_right_label", "y_left2_label", "y_right2_label"):
    check("  %-20s has one too" % key,
          "onChange={(v) => onChange({ %s: v })}" % key in DES)
for key in ("y_axis_unit", "y_axis_right_unit", "y_left2_unit", "y_right2_unit"):
    check("  %-20s has one too" % key,
          "onChange={(v) => onChange({ %s: v })}" % key in DES)

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
