# -*- coding: utf-8 -*-
"""Chart interpolation and time range must stay REACHABLE in the UI.

2026-08-30: "we are missing the interpolation option of charts when setting
the series and multiseries on the dashboard widgets".

The control had not been deleted - it was wrapped in `{false ? (` during the
Configure-dialog redesign, on the theory that Series & Axis now owned it. It
never got added there, so the setting disappeared from the UI while the
renderer went on honouring config.interpolation. A widget setting that no
screen can reach is indistinguishable from a missing feature, and nothing
failed: the code compiled, the chart drew, the option was simply gone.

Same day: "we should have also a topn for the intervals, in numbers of
readings or pre defined, like current day, last hour, last 8 hours, last 24
hours". The preset engine already existed and worked - it was buried in the
inner Data Query modal, and had neither "current day" nor "8 hours".

These are source checks because that is where this class of fault lives: a
feature rendered unreachable by a dead branch, or a new per-series field
dropped by the save allowlist (which has already happened once to the per-lane
Y bounds, as that allowlist's own comment records).
"""
from __future__ import annotations

import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = os.path.join(ROOT, "frontend", "src", "components", "Dashboard")
FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:56s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:140]) if detail else ""))
    if not ok:
        FAILS.append(name)


designer = io.open(os.path.join(DASH, "DashboardDesigner.jsx"),
                   encoding="utf-8", errors="replace").read()
widgets = io.open(os.path.join(DASH, "DashboardWidgets.jsx"),
                  encoding="utf-8", errors="replace").read()

print("[interpolation is reachable]")
# Targeted, not blanket: one OTHER `{false ?` block in this file hides the
# manual Y-axis scale, and that one is legitimate - the Axis configuration
# modal owns those keys now, so the setting is still reachable. Interpolation
# had no second home, which is what made hiding it a deletion.
dead = [m for m in range(len(designer)) if designer.startswith("{false ?", m)]
hidden_interp = any("Interpolation" in designer[m:m + 2000] for m in dead)
check("interpolation is not hidden behind a false branch", not hidden_interp,
      "%d dead branch(es) in the file; none may contain it" % len(dead))

# The options list must be RENDERED, not merely defined: twice, because the
# widget-wide control and the per-series override are separate places.
uses = len(re.findall(r"CHART_INTERPOLATION_OPTIONS\.map", designer))
check("the options are rendered in two places", uses >= 2,
      "%d render site(s) - widget-wide and per-series" % uses)
check("the widget-wide control writes config.interpolation",
      "config: { ...p.config, interpolation: e.target.value }" in designer)
check("the per-series control writes the row",
      "patchRow({ interpolation: e.target.value })" in designer)
check("a per-series value survives Save",
      re.search(r'interpolation: \["linear", "monotone", "natural", "step", '
                r'"stepBefore", "stepAfter"\]', designer) is not None,
      "the series allowlist drops any field it does not name")

print()
print("[the renderer honours both levels]")
line_area = re.findall(r"type=\{s\.interpolation \|\| interpolation\}", widgets)
check("Line and Area prefer the per-series value", len(line_area) >= 2,
      "%d render site(s)" % len(line_area))
check("a bad per-series value falls back instead of breaking the chart",
      '"stepBefore", "stepAfter"]\n            .includes(t) ? t : "";' in widgets
      or '.includes(t) ? t : "";' in widgets,
      "an unknown curve type would otherwise reach Recharts verbatim")

print()
print("[the time range offers what an operator asks for]")
for value, label in (("8h", "Last 8 hours"), ("today", "Current day")):
    check("preset %-6s is offered" % value,
          ('{ value: "%s"' % value) in designer and label in designer)
check("the range selector is reachable from the main Configure tab",
      len(re.findall(r"query_time_filter_preset: e\.target\.value", designer)) >= 2,
      "one in the Data Query modal, one beside Reading points where "
      "operators actually look")
check("8h resolves to a real window",
      '"8h": 8 * 60 * 60 * 1000' in widgets)
check("current day is midnight-to-now, not a 24 h window",
      'preset === "today"' in widgets
      and widgets.count('preset === "today"') >= 2,
      "needed in BOTH the query range and the client-side filter")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
