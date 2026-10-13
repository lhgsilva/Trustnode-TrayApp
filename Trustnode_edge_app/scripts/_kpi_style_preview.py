# -*- coding: utf-8 -*-
"""Render the two KPI presentations side by side, on real meter data.

Cards (rounded) and the plain table, so the spacing and the corners can be
compared on one page. Writes into the scratch workspace it is given, never
the live reports folder.
"""
from __future__ import annotations

import glob
import json
import os
import sys

spec_files = sorted(glob.glob(os.path.join(os.environ.get("TMP", "/tmp"),
                                           "tn-energy-*", "span.json")),
                    key=os.path.getmtime)
if not spec_files:
    print("no energy snapshot found - run the energy probe first")
    raise SystemExit(2)
spec = json.load(open(spec_files[-1], encoding="utf-8"))
os.environ.update(TRUSTNODE_SKIP_DOTENV="1", TRUSTNODE_DATA_DIR=spec["tmp"],
                  TRUSTNODE_APP_STORE_PATH=spec["db"],
                  TRUSTNODE_BOOT_INTEGRITY_CHECK="never")
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

from app.services.report_renderer import render_template_to_pdf  # noqa: E402

RUN = {"preset": "run"}
ITEMS = [
    {"label": "Energy used (kWh)", "tag_name": "insight.power_usage_kwh",
     "aggregation": "max"},
    {"label": "Peak demand (kW)", "tag_name": "insight.active_power_kw",
     "aggregation": "max"},
    {"label": "Average load (kW)", "tag_name": "insight.active_power_kw",
     "aggregation": "avg"},
    {"label": "Power factor (avg)", "tag_name": "insight.power_factor",
     "aggregation": "avg"},
]

sections = [
    {"type": "header", "title": "KPI presentation", "subtitle": "cards vs table"},
    {"type": "kpi_grid", "title": "Cards - rounded corners", "kpi_style": "cards",
     "kpi_corners": "round", "columns": 4, "time_range": dict(RUN), "items": ITEMS},
    {"type": "kpi_grid", "title": "Cards - square corners", "kpi_style": "cards",
     "kpi_corners": "square", "columns": 4, "time_range": dict(RUN), "items": ITEMS},
    {"type": "kpi_grid", "title": "Table", "kpi_style": "table",
     "columns": 4, "time_range": dict(RUN), "items": ITEMS},
]

path, nbytes, _sha = render_template_to_pdf(
    {"name": "KPI presentation", "definition": {"sections": sections}},
    window=(spec["from"], spec["to"]))
print("PDF %s %d" % (path, nbytes))
