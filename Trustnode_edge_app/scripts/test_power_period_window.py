# -*- coding: utf-8 -*-
"""A calendar period is an anchor, not a duration.

2026-08-31: "when in live, the insights should follow what is filtered in
period, same for total usage and tariffs, as default use the day. now it is
being changing everytime not showing the compilados."

`periodMs` was a useMemo over [powerPeriod] - computed once when the period was
chosen and then frozen - and the window was applied as `now - periodMs`. Choose
"Today (since midnight)" at 10:00 and the span froze at ten hours; by 15:00 the
window was 05:00 to 15:00. It had slid off midnight, dropped the morning, and
the totals reset a little on every refresh instead of accumulating.

The distinction this file protects:

    "Last 15 minutes"          a DURATION - slides, and should
    "Today (since midnight)"   an ANCHOR  - grows, and must not slide

Checked at source, because the arithmetic lives in the browser: a running app
cannot be asked "what did you think midnight was five hours ago?".
"""
from __future__ import annotations

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:56s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


app = io.open(os.path.join(ROOT, "frontend", "src", "App.jsx"),
              encoding="utf-8", errors="replace").read()

print("[the window is computed from an anchor]")
check("there is one helper that resolves a period to a window",
      "function powerPeriodWindow(period, nowMs)" in app,
      "one place to be right, rather than the same arithmetic in three memos")

helper = app.split("function powerPeriodWindow", 1)[-1][:900]
check("  a calendar day anchors at local midnight",
      "new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()" in helper)
check("  a month anchors at the 1st", "now.getMonth(), 1).getTime()" in helper)
check("  a year anchors at Jan 1", "new Date(now.getFullYear(), 0, 1).getTime()" in helper)
check("  and a rolling period still slides",
      "nowMs - ms" in helper,
      '"Last 15 minutes" really is a duration')

print()
print("[nothing freezes it]")
check("the live row filter recomputes the window",
      "powerPeriodWindow(powerPeriod, Date.now())" in app,
      "the previous code did now - a span captured when the period was picked")
check("  and the memo re-runs as time passes",
      "powerNowTick" in app and "setPowerNowTick(Date.now())" in app,
      "a growing window has to be recomputed, or it stops growing")
check("  with the tick in the filter's dependencies",
      re.search(r"powerPeriod, powerNowTick, selectedPowerChartMeters", app) is not None)

# The bug in one assertion: periodMs must not be memoised on the period alone.
m = re.search(r"const periodMs = useMemo\((.{0,400}?)\);", app, re.S)
check("periodMs is no longer frozen on [powerPeriod]",
      bool(m) and "[powerPeriod]" not in (m.group(1) or ""),
      "that single dependency is what froze 'Today' at the hour it was chosen")

print()
print("[the panels do not depend on which unit the chart shows]")
check("the series request always carries power and energy",
      '"power_kw",' in app and '"energy_kwh",' in app
      and "Array.from(new Set([" in app,
      "selecting A used to leave the insights and tariffs with no data at all")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
