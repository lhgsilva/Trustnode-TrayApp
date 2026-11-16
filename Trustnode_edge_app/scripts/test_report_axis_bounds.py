# -*- coding: utf-8 -*-
"""Each Y-axis bound of a report chart is Auto, Manual or linked to a Tag.

2026-11-16, asked: "on the report builder we should be able to configure the
Y axis to be auto and/or manual and also link the values to a tag so the
charts in the pdf are more properly done; auto, manual and linked tags
should be individual to min and max."

The bound keeps its field (y_min / y_max / y_right_min ... ) and its meaning
by shape: "" auto, a number manual, a tag token linked. Drawn on a real
chart, the tick labels at the ends of the axis must be the bound:

  * data 0..49, Max linked to {Limit} (reads 150), Min to {min:Low} (-20)
    -> the axis runs -20 .. 150;
  * Max Manual 100, Min Auto -> top tick 100, bottom from the data;
  * a tag with no reading leaves the bound Auto (never 0, never an error);
  * the right axis takes its own linked bound independently;
  * the builder offers Auto / Manual / Tag on every one of the 8 bounds.

    python scripts/test_report_axis_bounds.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "backend"))
_TMP = tempfile.mkdtemp(prefix="tn-axisbounds-")
os.environ.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=_TMP,
                  TRUSTNODE_APP_STORE_PATH=os.path.join(_TMP, "s.db"),
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never", TRUSTNODE_TENANT_ID="tenant-cust-ab")

from app.state import app_store                               # noqa: E402
from app.tenant import set_current_tenant                     # noqa: E402
from app.services.report_renderer import (                    # noqa: E402
    _build_styles, _section_chart, apply_text_tokens)
from reportlab.graphics.shapes import Drawing, Group, String  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %-66s %s%s" % (name, "PASS" if ok else "FAIL", ("  - " + detail) if (not ok and detail) else ""))


set_current_tenant("tenant-cust-ab")
base = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None) - timedelta(minutes=30)
rows = []
for i in range(120):
    ts = (base + timedelta(seconds=i * 10)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    rows.append({"ts_utc": ts, "gateway_id": "gw", "tag_name": "Flow", "value": float(i % 50), "quality": 192, "quality_label": "GOOD"})
    rows.append({"ts_utc": ts, "gateway_id": "gw", "tag_name": "Temp", "value": 60.0 + (i % 10), "quality": 192, "quality_label": "GOOD"})
    rows.append({"ts_utc": ts, "gateway_id": "gw", "tag_name": "Limit", "value": 150.0, "quality": 192, "quality_label": "GOOD"})
    rows.append({"ts_utc": ts, "gateway_id": "gw", "tag_name": "Low", "value": -20.0 if i == 7 else 5.0, "quality": 192, "quality_label": "GOOD"})
    rows.append({"ts_utc": ts, "gateway_id": "gw", "tag_name": "TempMax", "value": 90.0, "quality": 192, "quality_label": "GOOD"})
app_store.append_historian_rows(rows)
frm = base.strftime("%Y-%m-%d %H:%M:%S")
to = (base + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")


def section(**bounds):
    s = {"type": "line_chart", "title": "Bounds", "gateway_id": "gw",
         "time_range": {"preset": "custom", "from_utc": frm, "to_utc": to},
         "series": [{"id": "s1", "label": "Flow", "gateway_id": "gw", "tag_name": "Flow", "axis": "left1"},
                    {"id": "s2", "label": "Temp", "gateway_id": "gw", "tag_name": "Temp", "axis": "right1"}]}
    s.update(bounds)
    return s


def drawn(sec):
    tpl = apply_text_tokens({"name": "t", "definition": {"sections": [sec]}})
    resolved = tpl["definition"]["sections"][0]
    story: list = []
    _section_chart(dict(resolved), _build_styles(), story)
    out = []

    def walk(obj):
        for sh in getattr(obj, "contents", []) or []:
            if isinstance(sh, String):
                out.append(sh.text)
            elif isinstance(sh, (Group, Drawing)):
                walk(sh)
    for f in story:
        if isinstance(f, Drawing):
            walk(f)
        else:
            for sub in getattr(f, "_content", []) or []:
                if isinstance(sub, Drawing):
                    walk(sub)
    return resolved, out


def nums(texts):
    out = []
    for t in texts:
        try:
            out.append(float(str(t).replace(",", "")))
        except Exception:
            pass
    return out


print("each Y-axis bound is Auto, Manual or a Tag")
res, txt = drawn(section(y_max="{Limit}", y_min="{min:Low}"))
check("a Max linked to a tag resolves to its reading (150)", str(res.get("y_max")) == "150", repr(res.get("y_max")))
check("  a Min linked to a tag's minimum resolves to -20", str(res.get("y_min")) == "-20", repr(res.get("y_min")))
n = nums(txt)
check("  the drawn axis runs from -20 to 150", (-20.0 in n) and (150.0 in n), repr(sorted(set(n))[:12]))

res, txt = drawn(section(y_max="100", y_min=""))
n = nums(txt)
check("Max Manual 100 with Min Auto: the top tick is 100", 100.0 in n and max(x for x in n if x <= 1000) <= 100.0, repr(sorted(set(n))[:12]))
check("  and the bottom follows the data (no -20)", -20.0 not in n, repr(sorted(set(n))[:12]))

res, txt = drawn(section(y_max="{NoSuchTag}"))
check("a tag with no reading leaves the bound Auto, not 0", str(res.get("y_max")) in ("-", "") or res.get("y_max") is None, repr(res.get("y_max")))
n = nums(txt)
check("  and the chart still draws its data range", bool(n) and max(x for x in n if x <= 1000) >= 49.0, repr(sorted(set(n))[:12]))

res, txt = drawn(section(y_right_max="{TempMax}", y_right_min="0"))
check("the right axis takes its own linked Max (90) and manual Min (0)",
      str(res.get("y_right_max")) == "90" and str(res.get("y_right_min")) == "0", repr((res.get("y_right_min"), res.get("y_right_max"))))
n = nums(txt)
check("  and draws 0 .. 90 on that axis", 90.0 in n and 0.0 in n, repr(sorted(set(n))[:16]))

designer = open(os.path.join(ROOT, "frontend", "src", "components", "Reports", "ReportTemplateDesigner.jsx"), encoding="utf-8").read()
fields = ("y_min", "y_max", "y_right_min", "y_right_max", "y_left2_min", "y_left2_max", "y_right2_min", "y_right2_max")
check("the builder offers Auto / Manual / Tag on all 8 bounds",
      all(('<AxisBoundInput label="M' in designer) and (("value={section.%s ??" % f) in designer) for f in fields)
      and '<option value="tag">Tag</option>' in designer, "")

print()
print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("    FAILED: %s" % f)
raise SystemExit(0 if not FAIL else 1)
