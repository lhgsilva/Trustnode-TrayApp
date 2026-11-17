# -*- coding: utf-8 -*-
"""Report sections respect their window and never average unless asked.

2026-11-17, reported at a customer: "the window time for each section is not
being respected, the charts are not graphing the correct time window of the
data, and worse it is aggregating automatically without any instructions".
Measured on a seeded store, with "Group samples: Raw":

  * a 1 h window was averaged every 1 s, a 6 h window every 5 s (any window
    over 2 h, or holding over 5 000 readings, was bucketed and averaged);
  * points were placed by POSITION: a tag recorded only in the last 3 h of a
    6 h window was drawn from 0% to 45% of the width (its time: 50%-100%),
    collection holes were squeezed out, the axis labels were the first and
    last reading, not the window;
  * the chart's table export and the data table paired series by POSITION,
    so one tag's value sat next to another tag's timestamp.

Renders real chart sections (ReportLab, PolyLine recorded) against a seeded
store: Flow 6 h at 0.5 s with a 30-minute hole, Temp every 1 s for the last
3 h only. Flow's values are whole numbers, so an average would show.

    python scripts/test_report_chart_window.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_TMP = tempfile.mkdtemp(prefix="tn_rptwin_")
os.environ.update(TRUSTNODE_APP_STORE_PATH=os.path.join(_TMP, "s.db"), TRUSTNODE_DATA_DIR=_TMP, TRUSTNODE_SKIP_DOTENV="1",
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_TENANT_ID="tenant-cust-rw")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "backend"))
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-78s %s%s" % (name, "PASS" if ok else "FAIL", ("  - " + detail) if (not ok and detail) else ""))


def ep(s):
    s = str(s).replace("T", " ").replace("Z", "")
    for f in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:26], f).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    raise ValueError(s)


def main() -> int:
    print("report sections respect their window")
    from app.state import app_store
    from app.tenant import set_current_tenant
    set_current_tenant("tenant-cust-rw")
    from app.services import report_renderer as rr

    now = datetime.now(timezone.utc).replace(microsecond=0)
    rows = []
    t = now - timedelta(hours=6)
    while t <= now:
        age_h = (now - t).total_seconds() / 3600
        if not (2.0 <= age_h <= 2.5):
            ts = t.strftime("%Y-%m-%d %H:%M:%S.") + "%03d" % (t.microsecond // 1000)
            base = dict(ts_utc=ts, gateway_id="gw1", gateway_name="PLC", device_name="d", plc_ip="1", database_name="db",
                        quality=192, quality_label="GOOD", source="t")
            rows.append(dict(base, tag_name="Flow", value=float(int((now - t).total_seconds()) % 600)))
            if age_h <= 3 and t.microsecond == 0:
                rows.append(dict(base, tag_name="Temp", value=float(int(t.timestamp()) % 1000)))
        t += timedelta(seconds=0.5)
    for i in range(0, len(rows), 20000):
        app_store.append_historian_rows(rows[i:i + 20000])

    def sec(preset, **kw):
        d = {"type": "line_chart", "title": "w", "time_range": {"preset": preset}, "bucket": "raw",
             "series": [{"id": "s0", "gateway_id": "gw1", "tag_name": "Flow", "color": "#ff0000"},
                        {"id": "s1", "gateway_id": "gw1", "tag_name": "Temp", "color": "#0000ff"}]}
        d.update(kw)
        return d

    # ---- 1. raw is raw ---------------------------------------------------------------
    s1 = sec("1h")
    meta, al = rr._fetch_multi_series(s1)
    flow = al[0]
    check("1 h raw: every reading of the window (7 200 at 0.5 s), no caption about averaging",
          7190 <= len(flow) <= 7201 and not s1.get("_auto_bucket_note"), "%d pts, note %r" % (len(flow), s1.get("_auto_bucket_note")))
    check("  every value is a real reading (Flow is whole numbers - an average would not be)",
          all(p[1] == int(p[1]) for p in flow))
    w0, w1 = (ep(x) for x in s1["_window_used"])
    check("  and the readings span the window", ep(flow[0][0]) - w0 < 2 and w1 - ep(flow[-1][0]) < 3)
    s6 = sec("6h")
    s6["_fetch_mode"] = "window_peaks"
    meta, al = rr._fetch_multi_series(s6)
    note = str(s6.get("_auto_bucket_note") or "")
    check("6 h, over the per-series cap: drawn as the low and high reading of short intervals, said in the caption",
          "low and high" in note and "nothing averaged" in note, note)
    check("  every value drawn is a real reading (no averages)", all(p[1] == int(p[1]) for p in al[0]))
    check("  the peaks survive (Flow's sawtooth reaches 599 and 0)", max(p[1] for p in al[0]) == 599 and min(p[1] for p in al[0]) == 0)
    check("  no stamp before the window start", all(ep(p[0]) >= ep(s6["_window_used"][0]) - 0.001 for p in al[0]))
    tsp = sorted(ep(b[0]) - ep(a[0]) for a, b in zip(al[1], al[1][1:]))
    check("  Temp (9 000 readings, under the cap) is read raw: every reading, 1 s apart",
          8990 <= len(al[1]) <= 9001 and tsp[len(tsp) // 2] == 1.0, "%d pts" % len(al[1]))
    sb = sec("1h", bucket="1m", series=[{"id": "s0", "gateway_id": "gw1", "tag_name": "Flow", "aggregation": "avg"}])
    meta, al = rr._fetch_multi_series(sb)
    sp = sorted(ep(b[0]) - ep(a[0]) for a, b in zip(al[0], al[0][1:]))
    check("an explicit 'Group samples: 1 minute' + Average still aggregates, one point a minute",
          al[0] and sp[len(sp) // 2] == 60 and any(p[1] != int(p[1]) for p in al[0]), "%d pts" % len(al[0]))

    # ---- 2. drawn at their time ----------------------------------------------------------
    from reportlab.graphics.shapes import PolyLine, String
    drawn: list[tuple[str, list[float]]] = []
    labels: list[str] = []

    class RecLine(PolyLine):
        def __init__(self, points, **kw):
            c = kw.get("strokeColor")
            drawn.append((c.hexval() if c is not None else "", list(points)))
            super().__init__(points, **kw)

    class RecString(String):
        def __init__(self, x, y, text, **kw):
            labels.append(str(text))
            super().__init__(x, y, text, **kw)
    rr.PolyLine, rr.String = RecLine, RecString
    styles = rr._build_styles()
    story: list = []
    s6c = sec("6h")
    w0, w1 = (ep(x) for x in rr._resolve_time_range(s6c["time_range"]))
    rr._section_chart(s6c, styles, story)
    red = [pts for c, pts in drawn if c.lower().endswith("ff0000")]
    blue = [pts for c, pts in drawn if c.lower().endswith("0000ff")]
    check("the chart rendered both series", bool(red) and bool(blue), "%d red, %d blue polylines" % (len(red), len(blue)))
    if red and blue:
        rx = [x for pts in red for x in pts[0::2]]
        bx = [x for pts in blue for x in pts[0::2]]
        left, right = min(rx), max(rx)
        frac = lambda x: (x - left) / (right - left)
        check("Temp (the last 3 h of a 6 h window) starts at the middle of the axis, not at its left edge",
              abs(frac(min(bx)) - 0.5) < 0.02 and abs(frac(max(bx)) - 1.0) < 0.02, "Temp drawn %.2f .. %.2f" % (frac(min(bx)), frac(max(bx))))
        check("Flow's line breaks at the 30-minute collection hole (two runs, hole at 58%-67%)",
              len(red) >= 2 and any(abs(frac(pts[-2]) - (3.5 / 6)) < 0.02 for pts in red) and any(abs(frac(pts[0]) - (4.0 / 6)) < 0.02 for pts in red),
              "runs %s" % [(round(frac(p[0]), 3), round(frac(p[-2]), 3)) for p in red])
    start_lbl = datetime.fromtimestamp(w0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    end_lbl = datetime.fromtimestamp(w1, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    check("the X axis is labelled with the WINDOW's start and end, with time ticks between",
          any(l.startswith(start_lbl) for l in labels) and any(l.startswith(end_lbl) for l in labels)
          and sum(1 for l in labels if len(l) == 5 and l[2] == ":") >= 3, "labels %s" % [l for l in labels if ":" in l][:6])

    # ---- 3. tables joined by time ----------------------------------------------------------
    rr.PolyLine, rr.String = PolyLine, String
    tsec = {"type": "data_table", "title": "t", "time_range": {"preset": "15m"}, "bucket": "raw", "row_limit": 40,
            "series": [{"id": "s0", "gateway_id": "gw1", "tag_name": "Flow"}, {"id": "s1", "gateway_id": "gw1", "tag_name": "Temp"}]}
    header, body = rr._build_data_table_rows(tsec)
    bad = []
    for row in body:
        ts = ep(row[0])
        want = float(int(ts) % 1000)          # Temp at or before ts (Temp is on whole seconds)
        if row[2] not in ("-",) and abs(float(row[2].split()[0]) - want) > 0.0001:
            bad.append((row[0], row[2], want))
    check("data table: each row's Temp is the Temp reading at that time (rows joined by time, not by position)",
          body and not bad, "%d rows, %d wrong: %s" % (len(body), len(bad), bad[:2]))
    check("  newest readings of the window, not bucketed (40 rows of 0.5 s cycles)", len(body) == 40 and
          (ep(body[-1][0]) - ep(body[0][0])) < 25, "%d rows over %.0fs" % (len(body), ep(body[-1][0]) - ep(body[0][0]) if body else 0))
    h2, b2 = rr.build_chart_section_rows(sec("15m"))
    bad2 = [r for r in b2 if r[3] != "-" and abs(float(r[3]) - float(int(ep(r[1])) % 1000)) > 0.0001]
    check("chart export (CSV companion): rows joined by time too", b2 and not bad2, "%d rows, %d wrong" % (len(b2), len(bad2)))

    # ---- 4. the preview reads what the PDF draws ---------------------------------------------
    tpl = {"id": "t1", "name": "w", "definition": {"sections": [sec("6h")]}}
    data = rr.build_template_render_data(tpl)
    ch = [x for x in (data.get("sections") or []) if x.get("type") in ("line_chart", "chart")]
    pts = (ch[0].get("series") or [{}])[0].get("points") if ch else None
    first = (pts[0].get("ts") if isinstance(pts[0], dict) else pts[0][0]) if pts else None
    check("the data preview covers the whole 6 h window (it read the newest 240 readings)",
          bool(pts) and (ep(first) - ep(rr._resolve_time_range({"preset": "6h"})[0])) < 120, "first %s" % first)

    print()
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
