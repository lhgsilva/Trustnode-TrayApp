# -*- coding: utf-8 -*-
"""A chart must cover its whole window, not the newest N rows of it.

2026-10-11, asked: "check also if the data collected in the reports,
including charts and other telemetry tables use the period time set in the
gateway or data avaible in historian, for exemple if is 1s not data less
than 1 s".

The answer was: neither. The resolution came from a ROW BUDGET.
get_historian_rows_range is `ORDER BY ts_utc DESC LIMIT n`, and a chart asked
for 240 rows by default. So a one-hour section on a tag collected every
second read the newest 240 readings - the last four minutes - and drew them
across a one-hour axis. Nothing errored and the chart looked plausible.

Windows over two hours were already fixed (they read pre-bucketed rows);
everything shorter was not. This is the same "a row limit is not a time
window" defect found in the power chart on 2026-08-29.

A report can never show finer than the gateway collected, because bucketing
only aggregates rows that exist - that half of the question needs no change,
and this asserts it rather than assuming it.

Writes into a scratch workspace; never the live store.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
_TMP = tempfile.mkdtemp(prefix="tn-chartwin-")
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
from app.services.report_renderer import _fetch_multi_series     # noqa: E402

print("TrustNode - a chart covers its window, at the rate the gateway collected")
print()

# One hour of 1 Hz data: 3600 readings, far more than the old 240 budget.
base = (datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
        - timedelta(hours=1, minutes=5))
rows = []
for i in range(3600):
    ts = (base + timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    rows.append({"ts_utc": ts, "gateway_id": "gw-w", "tag_name": "Flow_1s",
                 "value": float(i), "quality": 192, "quality_label": "GOOD"})
# A second tag on a 60 s period over the same hour: 60 readings only.
for i in range(60):
    ts = (base + timedelta(seconds=i * 60)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    rows.append({"ts_utc": ts, "gateway_id": "gw-w", "tag_name": "Level_60s",
                 "value": float(i), "quality": 192, "quality_label": "GOOD"})
app_store.append_historian_rows(rows)

frm = base.strftime("%Y-%m-%d %H:%M:%S")
to = (base + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")


def series(tag, **extra):
    sec = {"type": "line_chart",
           "time_range": {"preset": "custom", "from_utc": frm, "to_utc": to},
           "series": [{"id": "s", "gateway_id": "gw-w", "tag_name": tag,
                       "axis": "left1"}]}
    sec.update(extra)
    return sec


def points(tag, limit, **extra):
    _meta, aligned = _fetch_multi_series(series(tag, **extra), default_limit=limit)
    return aligned[0] if aligned else []


def covered(pts):
    """How much of the hour the returned points actually span, 0..1."""
    if len(pts) < 2:
        return 0.0
    t0 = datetime.strptime(pts[0][0][:19], "%Y-%m-%d %H:%M:%S")
    t1 = datetime.strptime(pts[-1][0][:19], "%Y-%m-%d %H:%M:%S")
    return (t1 - t0).total_seconds() / 3600.0


print("[a dense tag over a window wider than the row budget]")
# The defect lives in the RAW read: ORDER BY ts_utc DESC LIMIT n returns the
# newest n, so a budget smaller than the window's readings silently becomes a
# time window. Shown here at the store, which is what the chart used to get
# handed straight through.
_raw = app_store.get_historian_rows_range(
    from_utc=frm, to_utc=to, limit=240, offset=0,
    gateway="gw-w", tag="Flow_1s", prefer_cloud_reads=False)
_raw_pts = [(str(r.get("ts") or ""), 0.0, None) for r in reversed(_raw)]
check("a raw read of 240 rows spans four minutes, not an hour",
      covered(_raw_pts) < 0.2,
      "%d rows spanning %.0f%% of the window - handed to a chart unchanged, "
      "that IS the bug" % (len(_raw), covered(_raw_pts) * 100))

old = points("Flow_1s", 240)
check("  but the chart now buckets instead of truncating",
      covered(old) > 0.95,
      "%d points spanning %.0f%% - the fallback fires on ANY budget, not "
      "only the new default" % (len(old), covered(old) * 100))

new = points("Flow_1s", 5000)
check("the chart's budget covers the whole hour", covered(new) > 0.95,
      "%d points spanning %.0f%%" % (len(new), covered(new) * 100))
check("  and it is a drawable number of points", 20 <= len(new) <= 5000, len(new))
check("  oldest first, so the line is not drawn backwards",
      len(new) > 1 and new[0][0] < new[-1][0])

print()
print("[it never invents a resolution the gateway did not collect]")
lvl = points("Level_60s", 5000)
check("a 60 s tag yields its 60 readings, not 3600",
      len(lvl) <= 61,
      "%d points - bucketing can only aggregate rows that exist, so there is "
      "no way to show data finer than the period" % len(lvl))
check("  and still covers the hour", covered(lvl) > 0.95,
      "%.0f%%" % (covered(lvl) * 100))
if len(new) <= 3600:
    check("  the 1 s tag is not padded beyond its own samples", len(new) <= 3600,
          "%d points from 3600 readings" % len(new))

print()
print("[and the CHART itself asks for the detail, not just the fetcher]")
# Bucketing to the minute keeps the window honest but throws away 59 of every
# 60 samples. A one-hour chart on a 1 s tag should still be drawn at 1 s.
from app.services.report_renderer import (                       # noqa: E402
    _section_chart, _build_styles)
from reportlab.graphics.shapes import Drawing, Group, Line        # noqa: E402

_story: list = []
_section_chart(series("Flow_1s"), _build_styles(), _story)
_segments = 0
def _walk(obj):
    global _segments
    for sh in getattr(obj, "contents", []) or []:
        if isinstance(sh, (Group, Drawing)):
            _walk(sh)
        elif isinstance(sh, Line) and not sh.strokeDashArray:
            _segments += 1
for _f in _story:
    if isinstance(_f, Drawing):
        _walk(_f)
    else:
        for _sub in getattr(_f, "_content", []) or []:
            if isinstance(_sub, Drawing):
                _walk(_sub)
check("a 1-hour chart on a 1 s tag is drawn at 1 s", _segments > 1000,
      "%d segments - at the old 240 budget the fallback buckets to the "
      "minute and draws about 60, which is honest but needlessly coarse"
      % _segments)

print()
print("[an operator who pins a count still gets it]")
pinned = points("Flow_1s", 5000, readings_count=240)
check("readings_count=240 caps the points", len(pinned) <= 240, len(pinned))
check("  and the window is still covered end to end", covered(pinned) > 0.95,
      "%d points spanning %.0f%% - a smaller budget must mean a COARSER "
      "chart, never a shorter one" % (len(pinned), covered(pinned) * 100))

print()
print("[windows that already worked are unchanged]")
short_to = (base + timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S")
short = _fetch_multi_series(
    {"type": "line_chart",
     "time_range": {"preset": "custom", "from_utc": frm, "to_utc": short_to},
     "series": [{"id": "s", "gateway_id": "gw-w", "tag_name": "Flow_1s",
                 "axis": "left1"}]}, default_limit=5000)[1][0]
check("a 3-minute window still returns raw 1 s samples", len(short) >= 150,
      "%d points - bucketing a short window to the minute would throw away "
      "the detail the operator opened it for" % len(short))
check("  consecutive samples are one second apart",
      len(short) > 2
      and (datetime.strptime(short[1][0][:19], "%Y-%m-%d %H:%M:%S")
           - datetime.strptime(short[0][0][:19], "%Y-%m-%d %H:%M:%S")
           ).total_seconds() == 1.0)

wide_to = (base + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
wide = _fetch_multi_series(
    {"type": "line_chart",
     "time_range": {"preset": "custom", "from_utc": frm, "to_utc": wide_to},
     "series": [{"id": "s", "gateway_id": "gw-w", "tag_name": "Flow_1s",
                 "axis": "left1"}]}, default_limit=5000)[1][0]
check("a 6-hour window still uses the pre-bucketed path",
      0 < len(wide) <= 400,
      "%d points - the >2h path was already correct and must stay" % len(wide))

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
