# -*- coding: utf-8 -*-
"""The web client view must honour the same axes and units as the edge.

2026-10-17, reported: comparing a dashboard profile on the edge with the same
profile opened in the web Lite client view, "the configuration of multiple Y
axis and units are not replicating".

They could not. Lite mapped every series with

    yAxisId: s.axis === "right" ? "right" : "left"

so a series the operator put on Left 2 or Right 2 silently landed on Left 1 -
sharing a scale it has nothing to do with and drawing a plausible, wrong
line. This is the same defect that was fixed in the REPORT renderer on
2026-10-08; the web view still had it. Units were worse: the file contained
zero references to any *_unit key, so a configured unit never appeared at
all.

The edge is the reference: DashboardWidgets.jsx normalises with
normSeriesAxis4() and carries a per-axis unit/suffix map. Lite now mirrors
both, using the SAME config keys, so one profile renders the same on both.

Also guarded here: liteAxisId lives at MODULE scope. renderSeriesElement is
a separate top-level function, and a copy defined inside ChartWidget would
be a ReferenceError there - an "X is not defined" that only appears at
runtime, which is the failure mode `npm run lint:undef` exists for and which
this hand-written file is not covered by.

Reads source only. No network, no store.
"""
from __future__ import annotations

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:170]) if detail else ""))
    if not ok:
        FAILS.append(name)


LITE = io.open(os.path.join(ROOT, "backend", "static", "lite_view", "index.html"),
               encoding="utf-8").read()
EDGE = io.open(os.path.join(ROOT, "frontend", "src", "components", "Dashboard",
                            "DashboardWidgets.jsx"), encoding="utf-8").read()

print("TrustNode - Lite renders the same axes and units as the edge")
print()

print("[1. the two-axis collapse is gone]")
# Ignore comment lines: the fix's own comment quotes the old expression to
# explain what it did, and a literal search would match that forever.
_code_lines = [l for l in LITE.splitlines()
               if not l.lstrip().startswith("//")]
check("no series is forced onto left/right any more",
      not any('s.axis === "right" ? "right" : "left"' in l for l in _code_lines),
      "this one expression put Left 2 and Right 2 onto Left 1")
check("  every series maps through the 4-way normaliser",
      LITE.count("yAxisId: liteAxisId(s.axis)") >= 3,
      "line, area and bar each need it: %d"
      % LITE.count("yAxisId: liteAxisId(s.axis)"))
check("  limit lines too", "yAxisId: liteAxisId(ll.axis)" in LITE,
      "a threshold drawn against the wrong scale is worse than none")

print()
print("[2. all four axes can be drawn]")
for axis in ("left1", "left2", "right1", "right2"):
    check("  %-6s is a known axis" % axis, '"%s"' % axis in LITE)
# 2026-10-17: these helpers moved to MODULE scope and gained the "lite"
# prefix, because the tag monitor is a separate top-level function and has to
# draw its axes from the same place - it had its own pair declared with the
# legacy left/right ids, so every series asked for an axis that did not exist
# and the monitor chart drew nothing.
check("only the axes in use are drawn",
      "const liteAxesUsed" in LITE and "used.add(liteAxisId(sd.axis))" in LITE,
      "an axis nobody is on must not steal width")
check("  and ONE builder draws them for both charts",
      LITE.count("liteYAxes(") >= 3,
      "the widget and the tag monitor must not keep separate axis rules: "
      "found %d reference(s)" % LITE.count("liteYAxes("))
check("  left1 is the fallback so a chart always has a scale",
      'if (!used.size) used.add("left1")' in LITE)
check("  the second axis on a side is oriented correctly",
      'orientation: isRight ? "right" : "left"' in LITE)

print()
print("[3. units are read - they were not referenced AT ALL before]")
for key in ("primary_unit", "y_axis_right_unit", "y_left2_unit", "y_right2_unit"):
    check("  Lite reads %-20s" % key, key in LITE,
          "the edge writes this key; Lite ignored every one of them")
check("the unit is shown on the axis",
      "liteAxisUnit(" in LITE and "tickFormatter: u ?" in LITE,
      "a reader cannot tell kW from degC otherwise")
check("  and folded into the axis label when both are set",
      'lbl + " (" + u + ")"' in LITE)

print()
print("[4. the SAME config keys as the edge - or it is not replication]")
m = re.search(r"LITE_AXIS_CFG\s*=\s*\{(.+?)\n  \};", LITE, re.S)
check("Lite declares a per-axis config map", bool(m))
if m:
    block = m.group(1)
    for axis, unit_key in (("left1", "primary_unit"),
                           ("right1", "y_axis_right_unit"),
                           ("left2", "y_left2_unit")):
        check("  %-6s -> %-20s matches the edge" % (axis, unit_key),
              unit_key in block and unit_key in EDGE,
              "both sides must name the same key or the profile will not "
              "replicate")

print()
print("[5. liteAxisId is reachable from BOTH functions]")
lines = LITE.split("\n")
defs = [i for i, l in enumerate(lines) if l.startswith("const liteAxisId")]
check("it is defined at module scope", bool(defs),
      "defined inside ChartWidget it is a ReferenceError in "
      "renderSeriesElement - a runtime-only failure")


def enclosing(idx):
    for i in range(idx, -1, -1):
        mm = re.match(r"^(function|const)\s+([A-Za-z0-9_]+)", lines[i])
        if mm:
            return mm.group(2)
    return "(module)"


users = sorted({enclosing(i) for i, l in enumerate(lines)
                if "liteAxisId(" in l and not l.startswith("const liteAxisId")})
check("  used from more than one top-level function", len(users) >= 2,
      ", ".join(users))
check("  and every one of them is below the definition",
      all(i > defs[0] for i, l in enumerate(lines)
          if "liteAxisId(" in l and not l.startswith("const liteAxisId")),
      "a const is in the temporal dead zone above its own declaration")

print()
print("[5b. each axis has its OWN scale]")
# 2026-10-17, reported: "the data is not showing respecting each Y axis ...
# and min and max and units". There were exactly two manual-range configs -
# manualLeft and manualRight - and each was applied to BOTH axes on its side,
# so left2 inherited left1's min/max and right2 inherited right1's. A 0..100%
# axis and a 900..1100 mbar axis on the same side drew on one scale and the
# second series flat-lined off the top.
for key in ("y_axis_mode", "y_right_axis_mode",
            "y_left2_axis_mode", "y_right2_axis_mode"):
    check("  Lite reads %-20s" % key, key in LITE,
          "without its own mode an axis cannot be manual on its own")
for key in ("y_left2_min", "y_left2_max", "y_right2_min", "y_right2_max"):
    check("  and %-20s" % key, key in LITE,
          "the edge writes this key for the second axis on that side")
check("the scale is built per axis, not per side",
      "buildManualY(cfg[k.mode], cfg[k.min], cfg[k.max], cfg[k.step])" in LITE,
      "one call per axis, reading that axis's own keys")
check("  the two side-wide scales are gone",
      "manualLeft" not in LITE.replace("manualLeft and manualRight", "")
      and "monManualRight" not in LITE,
      "a side-wide range is what made the second axis borrow the first's")
check("  every axis key set is complete",
      all(LITE.count(k) >= 1 for k in
          ("y_left2_tick_step", "y_right2_tick_step")),
      "a manual axis with no tick step still needs the key to exist")

print()
print("[6. the edge side is unchanged]")
check("the edge still normalises 4-way", "function normSeriesAxis4(" in EDGE)
check("  and still carries its per-axis unit map",
      'left2:  { unit: "y_left2_unit"' in EDGE,
      "Lite was brought up to the edge, not the other way round")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
