# -*- coding: utf-8 -*-
"""Line interpolation, and equal samples for equal triggers.

2026-10-11, reported: "we need to make sure thae samples colleted to be the
same if the triggers are the same" and "we also are missing the
interpolation option for charts".

Two defects:

  1. `readings_count` ("Sample count") shrank the FETCH, not the drawing. Two
     sections on the same run window carrying different caps - 1999 and 200,
     straight from the operator's own template - therefore read different
     slices of the window and reported different sample counts, which looked
     like a trigger fault. The window now decides what is read; the cap only
     thins it evenly afterwards, keeping the first and last reading. Same
     window and same cap always give the same count.

  2. A report chart drew straight segments between readings and nothing else.
     A stepped signal - a setpoint, a state, a recipe step - was drawn as a
     ramp it never performed: a diagonal between two readings implies values
     that were never measured. The dashboard widgets have had five
     interpolation modes for a year; reports had none.

Writes into a scratch workspace; never the live store.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
_TMP = tempfile.mkdtemp(prefix="tn-interp-")
os.environ.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=_TMP,
                  TRUSTNODE_APP_STORE_PATH=os.path.join(_TMP, "s.db"),
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never")

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)


from app.state import app_store                                   # noqa: E402
from app.services.report_renderer import (                        # noqa: E402
    _section_chart, _build_styles, _interpolate, _thin_evenly,
    norm_interpolation, CHART_INTERPOLATIONS)
from reportlab.graphics.shapes import Drawing, Group, Line         # noqa: E402

print("TrustNode - chart interpolation and equal samples for equal triggers")
print()

# ------------------------------------------------- the maths, in isolation
print("[the shapes a mode draws]")
SQUARE = [(0.0, 0.0), (10.0, 10.0), (20.0, 0.0)]
check("linear leaves the points alone", _interpolate(SQUARE, "linear") == SQUARE)
check("  an unknown mode falls back to linear",
      _interpolate(SQUARE, "spiral") == SQUARE)
check("  and so does an empty one", norm_interpolation("") == "linear")
check("  every advertised mode is accepted",
      all(norm_interpolation(m) == m for m in CHART_INTERPOLATIONS),
      ", ".join(CHART_INTERPOLATIONS))

after = _interpolate(SQUARE, "stepAfter")
check("stepAfter holds the old value, then jumps",
      (10.0, 0.0) in after and (20.0, 10.0) in after,
      "a held signal must look held, not like a ramp: %s" % (after,))
before = _interpolate(SQUARE, "stepBefore")
check("  stepBefore jumps first, then holds",
      (0.0, 10.0) in before and (10.0, 0.0) in before, before)
check("  both still pass through every reading",
      all(p in after for p in SQUARE) and all(p in before for p in SQUARE))

smooth = _interpolate(SQUARE, "natural")
check("a smooth mode adds intermediate points", len(smooth) > len(SQUARE),
      "%d points from %d" % (len(smooth), len(SQUARE)))
check("  and still passes through the readings",
      all(any(abs(px - x) < 0.01 and abs(py - y) < 0.01 for px, py in smooth)
          for x, y in SQUARE),
      "a curve that misses its own samples is a drawing, not a chart")

# A spike: monotone must not overshoot below 0 or above 10 between samples.
SPIKE = [(0.0, 0.0), (10.0, 0.0), (20.0, 10.0), (30.0, 10.0)]
mono = _interpolate(SPIKE, "monotone")
check("monotone never invents a value outside its samples",
      all(-0.01 <= y <= 10.01 for _x, y in mono),
      "an overshooting spline shows a peak the process never reached: "
      "min=%.2f max=%.2f" % (min(y for _x, y in mono), max(y for _x, y in mono)))
check("  a single point is returned untouched",
      _interpolate([(1.0, 2.0)], "monotone") == [(1.0, 2.0)])
check("  and so is an empty list", _interpolate([], "stepAfter") == [])

# ------------------------------------------------------- the even thinning
print()
print("[a cap thins evenly; it does not cut the window short]")
pts = [(("t%03d" % i), float(i), None) for i in range(1000)]
thin = _thin_evenly(pts, 100)
check("a cap of 100 yields 100 points", len(thin) == 100, len(thin))
check("  the FIRST reading is kept", thin[0] == pts[0])
check("  the LAST reading is kept exactly", thin[-1] == pts[-1],
      "dropping the tail would shorten the window - the defect this replaces")
check("  they are spread, not the first 100",
      thin[50][1] > 100, "point 50 is sample %s" % (thin[50][1],))
check("a cap larger than the data changes nothing", _thin_evenly(pts, 5000) is pts)
check("  a missing cap changes nothing", _thin_evenly(pts, None) is pts)
check("  a nonsense cap changes nothing", _thin_evenly(pts, "lots") is pts)

# ------------------------------------- the reported case, end to end
print()
print("[two sections, one trigger, different Sample counts]")
base = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None) - timedelta(minutes=20)
rows = []
for i in range(600):                       # 10 minutes at 1 Hz
    ts = (base + timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    for tag, val in (("Temperature", 150.0 + i * 0.01),
                     ("Flow", 8.0 + (i % 20) * 0.05),
                     ("Pressure", 1120.0 + (i % 11))):
        rows.append({"ts_utc": ts, "gateway_id": "gw-i", "tag_name": tag,
                     "value": val, "quality": 192, "quality_label": "GOOD"})
app_store.append_historian_rows(rows)
frm = base.strftime("%Y-%m-%d %H:%M:%S")
to = (base + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
WINDOW = {"preset": "custom", "from_utc": frm, "to_utc": to}


def ser(tag, axis):
    return {"id": tag, "label": tag, "gateway_id": "gw-i",
            "tag_name": tag, "axis": axis}


def caption(section):
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
    return [t for t in out if "series" in t or "readings" in t]


THREE = {"type": "line_chart", "title": "Full Test", "time_range": dict(WINDOW),
         "readings_count": 1999,
         "series": [ser("Flow", "left1"), ser("Temperature", "right1"),
                    ser("Pressure", "left2")]}
TWO = {"type": "line_chart", "title": "Temperature Profile",
       "time_range": dict(WINDOW), "readings_count": 200,
       "series": [ser("Temperature", "left1"), ser("Pressure", "right1")]}

c3 = caption(THREE)
c2 = caption(TWO)
check("both charts render", bool(c3) and bool(c2), (c3, c2))


def per_series(cap_text):
    import re
    m = re.search(r"(\d+) samples per series", cap_text or "")
    return int(m.group(1)) if m else -1


# Different caps are the operator's own choice, so the DRAWN counts may
# differ - but both must cover the whole window, which is the thing that
# was actually broken.
check("a 1999-cap section sees the whole window", per_series(c3[0]) == 600,
      "600 readings exist in the window: %s" % (c3,))
check("  a 200-cap section is thinned, not truncated",
      per_series(c2[0]) == 200,
      "it used to read a different SLICE of the window: %s" % (c2,))

# The guarantee the operator asked for: same trigger AND same cap -> same.
SAME_A = dict(THREE, readings_count=200)
SAME_B = dict(TWO, readings_count=200)
check("same window and same cap give the SAME per-series count",
      per_series(caption(SAME_A)[0]) == per_series(caption(SAME_B)[0]) == 200,
      "this is the requirement: 'the samples colleted to be the same if the "
      "triggers are the same'")
NOCAP_A = {k: v for k, v in THREE.items() if k != "readings_count"}
NOCAP_B = {k: v for k, v in TWO.items() if k != "readings_count"}
check("  and with no cap at all they also match",
      per_series(caption(NOCAP_A)[0]) == per_series(caption(NOCAP_B)[0]) == 600,
      (caption(NOCAP_A), caption(NOCAP_B)))

# --------------------------------------------- it reaches the drawn chart
print()
print("[the mode reaches the PDF, not just the config]")


def segments(section):
    story: list = []
    _section_chart(dict(section), _build_styles(), story)
    n = 0

    def walk(obj):
        nonlocal n
        for sh in getattr(obj, "contents", []) or []:
            if isinstance(sh, (Group, Drawing)):
                walk(sh)
            elif isinstance(sh, Line) and not sh.strokeDashArray and sh.strokeWidth > 0.5:
                n += 1

    for f in story:
        if isinstance(f, Drawing):
            walk(f)
        else:
            for sub in getattr(f, "_content", []) or []:
                if isinstance(sub, Drawing):
                    walk(sub)
    return n


SMALL = dict(TWO, readings_count=50)
lin = segments(dict(SMALL, interpolation="linear"))
step = segments(dict(SMALL, interpolation="stepAfter"))
nat = segments(dict(SMALL, interpolation="natural"))
check("linear is the default", segments(SMALL) == lin, (segments(SMALL), lin))
check("  a step draws about twice the segments", step > lin * 1.5,
      "%d vs %d" % (step, lin))
check("  a smooth curve draws many more", nat > lin * 4,
      "%d vs %d" % (nat, lin))
mixed = dict(SMALL, interpolation="linear")
mixed["series"] = [dict(TWO["series"][0], interpolation="stepAfter"),
                   dict(TWO["series"][1])]
check("  one series can override the chart", segments(mixed) > lin,
      "%d vs %d - a stepped setpoint over a linear measurement"
      % (segments(mixed), lin))
check("  an area chart interpolates too",
      segments(dict(SMALL, type="area_chart", interpolation="stepAfter")) > lin,
      "the fill and the line must agree about where the curve runs")

print()
print("[the editor offers it]")
import io as _io                                                   # noqa: E402
DES = _io.open(os.path.join(ROOT, "frontend", "src", "components", "Reports",
                            "ReportTemplateDesigner.jsx"), encoding="utf-8").read()
check("the chart has an interpolation control",
      "onChange={(e) => onChange({ interpolation: e.target.value })}" in DES)
check("  a series can override it",
      "updateSeries(s.id, { interpolation: e.target.value })" in DES)
check("  the modes match the dashboard's names",
      all(('"%s"' % m) in DES for m in CHART_INTERPOLATIONS),
      "a chart must mean the same thing in both places")
check("  and the sample cap says it is a MAXIMUM",
      "Max samples" in DES,
      '"Sample count" read as a target, which is why two sections on one '
      'trigger looked like they collected different amounts')

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
