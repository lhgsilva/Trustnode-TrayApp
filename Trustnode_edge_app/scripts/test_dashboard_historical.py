# -*- coding: utf-8 -*-
"""Historical dashboards are a SNAPSHOT, and the arrows scrub through it.

2026-09-03, reported: "when we set to historical, the charts should not update
any more. The data still should be logged and collected as normal, only the
charts frozen, in the range window set by the date/time range." And: the
arrows should "spam to the next unit, second, minute or hours based on the X
axis of the chart", the way a SCADA trend viewer pans.

What was wrong: in Historical mode `dashboardRows` fell back to
`localHistoricalRows` - the LIVE stream filtered to the range - whenever the
fetched snapshot's key did not match the current one. A slow fetch, a range
still being typed, or an end time at "now" all landed on that branch, so the
charts carried on scrolling underneath a Historical badge.

Nothing here touches collection: the gateway keeps polling and the historian
keeps writing while the chart is frozen. This is only about what the chart is
allowed to draw.

Asserted on the source. The behaviour lives in a React memo and a callback,
and the alternative - a browser driving the live install - is the thing that
blanked three dashboard widgets on 2026-08-22.
"""
from __future__ import annotations

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAILS: list[str] = []


def check(name, ok, detail=""):
    print("  {0:58s}: {1}{2}".format(name, "PASS" if ok else "FAIL",
                                     (" - " + str(detail)[:150]) if detail else ""))
    if not ok:
        FAILS.append(name)


DESIGNER = os.path.join(ROOT, "frontend", "src", "components", "Dashboard",
                        "DashboardDesigner.jsx")
src = io.open(DESIGNER, encoding="utf-8", errors="replace").read()

rows_memo = src.split("const dashboardRows = useMemo(")[1].split("}, [")[0] \
    if "const dashboardRows = useMemo(" in src else ""
pan = src.split("const panHistoricalWindow = useCallback(")[1].split("}, [")[0] \
    if "const panHistoricalWindow = useCallback(" in src else ""

print("TrustNode - historical dashboards freeze, and the arrows scrub")
print()
print("[historical draws the snapshot, never the live stream]")
check("the historical branch exists", bool(rows_memo))
check("a historical range never falls back to live rows",
      "return localHistoricalRows;" not in rows_memo.split("if (hasHistoricalRange)")[-1],
      "the fallback WAS the live stream filtered to the range, which is why a "
      "chart kept moving under a Historical badge")
check("  and entering Historical always seeds a window",
      "const enterHistoricalMode = useCallback(" in src
      and "onClick={enterHistoricalMode}" in src,
      "a mode with no range has nothing to freeze, so switching in with the "
      "dates empty left the charts running - the reported symptom")
check("  an unmatched range renders nothing rather than live data",
      ": [];" in rows_memo,
      "an empty chart says 'loading'; a moving one says 'this is your "
      "history', and only one of those is true")
check("live mode still returns the live rows",
      "dashboardTimeMode !== \"historical\"" in rows_memo
      and "liveBootstrapRows" in rows_memo,
      "switching back to Live must resume normal trending immediately")

print()
print("[the whole window is loaded, not just its newest rows]")
# 2026-09-03, reported: historical "is not showing the data filtered by the
# historian range". Measured against the live app: /historian/range returns
# the NEWEST `limit` rows inside the window, so at the meter's ~80 rows/s a
# 30-minute AND a 60-minute request both came back covering 18:40:49..18:42:58
# - the last two minutes. The dates were honoured; the window could not fit.
#
# A ROW LIMIT IS NOT A TIME WINDOW. The same mistake cost the Power Overview a
# day on 2026-08-26, and the fix is the same one: bucket in SQL across the
# whole span. Measured after: 60 min -> 94 points in 0.43 s.
APP = os.path.join(ROOT, "frontend", "src", "App.jsx")
app = io.open(APP, encoding="utf-8", errors="replace").read()
fetch = app.split("fetchHistoricalRows={async (")[1].split("fetchWidgetRows=")[0]     if "fetchHistoricalRows={async (" in app else ""
check("the historical fetch exists", bool(fetch))
check("hitting the row cap triggers a bucketed re-read",
      "if (raw.length < limit) return raw;" in fetch
      and "getAppStoreHistorianAgg" in fetch,
      "returning the capped slice is what showed two minutes on an hour axis")
check("  the bucket is chosen from the SPAN",
      '"day"' in fetch and '"hour"' in fetch and '"minute"' in fetch,
      "so a wide window gets coarse points across all of it")
# Comment lines stripped: the code's own explanation names the buckets that
# do NOT exist, and matching prose is not a test (I made exactly this mistake
# on get_bootstrap earlier today).
fetch_code = [ln for ln in fetch.splitlines() if not ln.strip().startswith("//")]
check("  and never asks for a bucket that does not exist",
      not any('"second"' in ln or "'second'" in ln for ln in fetch_code),
      "only minute/hour/day are implemented; anything else silently becomes "
      "minute, which would put ONE point on a 60 s window")
check("  the bucketed rows are renamed into the widgets' shape",
      "tag_name: r.tag_name || r.tag" in fetch,
      "agg rows carry the tag under `tag`; every widget reads `tag_name`")
check("  a bucketing failure still shows the raw slice",
      "return raw;   // bucketing failed" in fetch,
      "a truncated view beats an empty chart")

print()
print("[the arrows step ONE axis unit, not a whole window]")
check("an axis step helper exists", "const axisStepMs = useCallback(" in src)
step_fn = src.split("const axisStepMs = useCallback(")[1].split("}, [")[0]     if "const axisStepMs = useCallback(" in src else ""
check("  the step is a FRACTION of the window, not the raw axis unit",
      "Number(span) / 8" in step_fn,
      "the first version returned the axis unit itself - 1 s for any window "
      "under five minutes - which on a 60 s window is a 1.7% shift. The chart "
      "redrew identically and the arrows were reported as doing nothing")
check("  and it snaps DOWN to a round unit",
      "STEP_LADDER_MS" in src and "if (unit <= target) chosen = unit;" in step_fn,
      "so the window still lands on whole seconds, minutes or hours")
ladder = src.split("const STEP_LADDER_MS = useMemo(")[1].split("], []);")[0]     if "const STEP_LADDER_MS = useMemo(" in src else ""
for label, needle in (("seconds", "1000, 2000, 5000"),
                      ("minutes", "60000, 2 * 60000, 5 * 60000"),
                      ("hours", "3600000, 2 * 3600000"),
                      ("a day", "24 * 3600000")):
    check("  the ladder covers " + label, needle in ladder, ladder[:60])
check("the arrows ask for a unit step",
      src.count("panHistoricalWindow(1, { unit: true })") == 1
      and src.count("panHistoricalWindow(-1, { unit: true })") == 1,
      "both arrows must scrub; one left on the old whole-window jump is worse "
      "than neither")
check("  a DRAG still moves a whole window",
      "byUnit ? axisStepMs(span) : span" in pan,
      "the drag gesture is one operators already have; only the arrows changed")
check("the window WIDTH is preserved when panning",
      "const span = Math.max(60 * 1000, toMs - fromMs);" in pan,
      "a pan that also resized the window would not be a pan")

print()
print("[a one-second step has to survive the date control]")
check("the range inputs expose seconds",
      src.count('type="datetime-local" step="1"') == 2,
      "datetime-local rounds to the MINUTE by default, so a one-second step "
      "would be discarded and the arrow would look dead")
check("  and the value written back carries seconds",
      "pad(d.getSeconds())" in pan,
      "formatting to minutes throws the step away just as surely")

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
