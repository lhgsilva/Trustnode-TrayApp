# -*- coding: utf-8 -*-
"""A chart never draws outside its plot, and a tag's case never hides its data.

2026-11-02, from a customer's PDF: a Heating chart with y_min 30 drew the
room-temperature samples taken before the test BELOW the plot box and a
flat run out to its left - _y() mapped values linearly and nothing clipped
the result. The same page said "section trigger did not fire; showing the
report window" under every chart: the trigger was spelled
R1_TRUSTNODE_RESULTS.TEST_CYCLE_STARTED[4] while the gateway's tag list says
R1_Trustnode_Results.… - the PLC does not care about case, the operator
typed it from memory, and the exact lookup found nothing.

Now every series draws inside a group clipped to the plot rectangle, and a
range read that finds nothing for the exact spelling retries the same name
in any case, inside the same window.

    python scripts/test_chart_clip_and_tag_case.py
"""
from __future__ import annotations

import inspect
import os
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="tn_clip_")
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(_TMP, "store.db")
os.environ["TRUSTNODE_DATA_DIR"] = _TMP
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
os.environ["TRUSTNODE_TENANT_ID"] = "tenant-cust-clip"

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-66s %s%s" % (name, "PASS" if ok else "FAIL",
                            ("  - " + detail) if (not ok and detail) else ""))


def _ts(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) + ".000"


def main() -> int:
    print("charts stay in their box; a tag's case does not hide its readings")
    from app.state import app_store
    from app.tenant import set_current_tenant
    set_current_tenant("tenant-cust-clip")
    from app.services import report_renderer as rr

    # ---- the drawing: every series inside a clip group ----------------------
    src = inspect.getsource(rr._section_chart)
    check("each series draws inside a group clipped to the plot",
          "_plot_clip()" in src and "isClipPath=1" in src and "series_group.add(PolyLine" in src)
    check("  the clip is a clip only, not a filled shape",
          "fillColor=None, strokeColor=None" in src.split("isClipPath=1")[1][:80],
          "left at the defaults the path is filled black before it clips")
    check("  the area fill, bars and single-point markers are clipped too",
          "series_group.add(Polygon" in src and "series_group.add(Rect" in src and "series_group.add(Circle" in src)
    check("pathlib.Path is still pathlib.Path in the renderer",
          "from reportlab.graphics.shapes import Path as _RLPath" in inspect.getsource(rr)
          and "output_path = Path(output_path)" in inspect.getsource(rr.render_template_to_pdf))

    # ---- a chart whose data lies outside its y range still renders ---------
    now = time.time()
    rows = []
    for i in range(0, 600, 2):
        rows.append({"ts_utc": _ts(now - 600 + i), "gateway_id": "gw1", "gateway_name": "PLC",
                     "device_name": "d", "tag_name": "Test_Ring_1_Temp",
                     "value": 20.0 if i < 300 else 45.0, "quality": 192,
                     "quality_label": "GOOD", "source": "test"})
        rows.append({"ts_utc": _ts(now - 600 + i), "gateway_id": "gw1", "gateway_name": "PLC",
                     "device_name": "d", "tag_name": "R1_Trustnode_Results.TEST_CYCLE_STARTED[4]",
                     "value": 1.0 if 298 <= i <= 302 else 0.0, "quality": 192,
                     "quality_label": "GOOD", "source": "test"})
    app_store.append_historian_rows(rows)
    tpl = {"id": "t", "name": "clip", "definition": {"sections": [
        {"type": "line_chart", "title": "Heating Profile",
         "time_range": {"preset": "custom", "from_utc": _ts(now - 600), "to_utc": _ts(now)},
         "readings_count": 5000, "y_min": "30", "y_max": "60", "interpolation": "monotone",
         "series": [{"id": "s", "label": "Consumer", "gateway_id": "gw1", "tag_name": "Test_Ring_1_Temp",
                     "color": "#fa0000", "axis": "left", "multiplier": 1, "offset": 0}]}]}}
    path, n, _ = rr.render_template_to_pdf(tpl)
    check("a chart with readings below y_min renders", n > 5000, "%d bytes" % n)
    try:
        os.remove(str(path))
    except Exception:
        pass

    # ---- the store: exact first, then the same name in any case -----------
    src_range = inspect.getsource(app_store.get_historian_rows_range)
    check("the range read retries the tag in any case after an exact miss",
          "tag_name = :tag_exact COLLATE NOCASE" in src_range)
    exact = app_store.get_historian_rows_range(from_utc=_ts(now - 600), to_utc=_ts(now),
                                               gateway="gw1", tag="Test_Ring_1_Temp", limit=5000)
    other = app_store.get_historian_rows_range(from_utc=_ts(now - 600), to_utc=_ts(now),
                                               gateway="gw1", tag="TEST_RING_1_TEMP", limit=5000)
    check("the exact spelling reads its rows", len(exact) == 300, "%d" % len(exact))
    check("  and the same name in another case reads the SAME rows",
          len(other) == len(exact) and {r.get("ts") for r in other} == {r.get("ts") for r in exact},
          "%d vs %d" % (len(other), len(exact)))
    missing = app_store.get_historian_rows_range(from_utc=_ts(now - 600), to_utc=_ts(now),
                                                 gateway="gw1", tag="Test_Ring_9_Temp", limit=5000)
    check("  a tag that really does not exist still reads nothing", missing == [])

    # ---- and a trigger spelled in the wrong case fires ---------------------
    win = rr.resolve_trigger_window(
        {"start": {"gateway_id": "gw1", "tag_name": "R1_TRUSTNODE_RESULTS.TEST_CYCLE_STARTED[4]", "value": "1"}},
        _ts(now - 600), _ts(now))
    check("a section trigger typed in upper case fires against mixed-case data",
          win[0] != _ts(now - 600) and win[0] != rr._NEVER, repr(win))

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
