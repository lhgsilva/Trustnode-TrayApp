# -*- coding: utf-8 -*-
"""The energy report must render the WHOLE window with real numbers.

2026-08-26: "the power templates for reports also should be reviewed to make
sure everything is working fine."

Two defects sat behind that:
  * a chart section read raw rows through get_historian_rows_range, which caps
    at 5 000. At the 1 Hz cadence a power meter runs at, a "24h trend" section
    got the newest ~1.4 h and drew it across a 24 h axis - wrong in a way that
    looks perfectly fine on the page;
  * the Energy consumption preset shipped with every tag_name empty, so the
    KPIs and both series rendered blank until an operator bound six fields by
    hand.

Seeds a day of 1 Hz power data and renders the real template through the real
renderer. No hardware, no app.
"""
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

TMP = tempfile.mkdtemp(prefix="tn-preport-")
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_DATA_DIR"] = TMP
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(TMP, "s.db")
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"

FAILS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:110]) if detail else ""
    print("  {0:58s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


from app.state import app_store  # noqa: E402
from app.services import report_renderer as rr  # noqa: E402

# --- seed 24 h of 1 Hz power data -----------------------------------------
TAGS = {
    "insight.live_kw": ("power_insight", 12.0),
    "insight.current_a": ("power_insight", 30.0),
    "insight.total_kwh": ("power_insight", 0.0),
    "insight.peak_kw": ("power_insight", 18.0),
}
END = datetime.now(timezone.utc).replace(microsecond=0)
START = END - timedelta(hours=24)

print("  seeding 24 h of 1 Hz power data...")
t0 = time.monotonic()
con = sqlite3.connect(app_store._db_path, timeout=60)
con.execute("PRAGMA journal_mode=WAL")
buf = []
ts = START
n = 0
while ts < END:
    stamp = ts.strftime("%Y-%m-%d %H:%M:%S.000")
    for tag, (src, base) in TAGS.items():
        # live_kw swings so avg/peak are distinguishable; total_kwh only climbs
        val = base + (n % 600) * 0.01 if tag != "insight.total_kwh" else n * 0.001
        buf.append(("default", stamp, "pm-1", "Meter 1", "Meter", "127.0.0.1",
                    "Power Management", tag, val, None, "REAL", 192, "GOOD",
                    src, stamp))
    n += 1
    ts += timedelta(seconds=1)
    if len(buf) >= 50000:
        con.executemany(
            "INSERT INTO historian_readings (tenant_id, ts_utc, gateway_id, gateway_name,"
            " device_name, plc_ip, database_name, tag_name, value, value_text, data_type,"
            " quality, quality_label, source, created_utc)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", buf)
        buf = []
if buf:
    con.executemany(
        "INSERT INTO historian_readings (tenant_id, ts_utc, gateway_id, gateway_name,"
        " device_name, plc_ip, database_name, tag_name, value, value_text, data_type,"
        " quality, quality_label, source, created_utc)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", buf)
con.commit()
total = con.execute("SELECT COUNT(*) FROM historian_readings").fetchone()[0]
con.close()
print("  seeded {0:,} rows in {1:.1f}s".format(total, time.monotonic() - t0))

# --- the chart section, exactly as the template defines it -----------------
print("\n[the 24h dual-axis section]")
section = {
    "type": "line_chart",
    "title": "Power vs current (dual axis)",
    "time_range": {"preset": "24h"},
    "series": [
        {"id": "a", "label": "Power", "gateway_id": "", "tag_name": "insight.live_kw",
         "axis": "left", "chart_type": "line", "unit": "kW", "multiplier": 1, "offset": 0},
        {"id": "b", "label": "Current", "gateway_id": "", "tag_name": "insight.current_a",
         "axis": "right", "chart_type": "line", "unit": "A", "multiplier": 1, "offset": 0},
    ],
}
t0 = time.monotonic()
meta, aligned = rr._fetch_multi_series(section)
took = time.monotonic() - t0
check("both series return data", len(aligned) == 2 and all(aligned),
      [len(a) for a in aligned])
check("  and it renders quickly", took < 15.0, "{0:.2f}s".format(took))

if aligned and aligned[0]:
    pts = aligned[0]
    stamps = [p[0] for p in pts if p and p[0]]
    span_h = 0.0
    try:
        def _p(t):
            for f in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                      "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
                try:
                    return datetime.strptime(str(t).replace("Z", "").split("+")[0], f)
                except Exception:
                    continue
            return None
        a, b = _p(stamps[0]), _p(stamps[-1])
        if a and b:
            span_h = abs((b - a).total_seconds()) / 3600.0
    except Exception:
        pass
    print("  {0} points spanning {1:.1f} h".format(len(pts), span_h))
    check("  the section COVERS the full 24 h", span_h >= 23.0,
          "{0:.1f} h".format(span_h))
    check("  with real values, not blanks",
          sum(1 for p in pts if p[1] is not None) > 100,
          sum(1 for p in pts if p[1] is not None))

# a SHORT window must keep using raw rows - that path was already correct
print("\n[a short window is untouched]")
short = dict(section, time_range={"preset": "1h"})
meta2, aligned2 = rr._fetch_multi_series(short)
check("a 1h section still returns points", bool(aligned2 and aligned2[0]),
      [len(a) for a in aligned2])

# --- the KPI items the preset ships with -----------------------------------
print("\n[the preset's KPIs are bound to real tags]")
designer = open(os.path.join(ROOT, "frontend", "src", "components", "Reports",
                             "ReportTemplateDesigner.jsx"), encoding="utf-8").read()
i = designer.find('case "energy_consumption"')
block = designer[i:i + 2600] if i >= 0 else ""
check("the energy preset exists", bool(block))
for tag in ("insight.total_kwh", "insight.live_kw", "insight.peak_kw",
            "insight.current_a"):
    check("  binds {0}".format(tag), tag in block)
# Only the KPI items and chart series matter. defaultSection() also carries a
# section-level tag_name, which is unused once a `series` list is present.
unbound = [ln.strip()[:70] for ln in block.splitlines()
           if 'tag_name: ""' in ln and ('makeId("kpi")' in ln or 'makeId("ser")' in ln)]
check("  no KPI or series is left unbound", not unbound, unbound[:3])

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
