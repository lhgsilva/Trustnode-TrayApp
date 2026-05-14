"""Smoke test for the extended reporting features.

Covers:
  - Multi-series line chart with dual axes, units, limit lines (PDF render OK)
  - Data table with custom columns + calc column (CSV export OK)
  - TXT export endpoint (pipe-delimited)

Run via the backend venv Python:
  d:/.../Trustnode_edge_app/backend/.venv/Scripts/python.exe \
    d:/.../Trustnode_edge_app/scripts/smoke-reports-rich.py
"""
from __future__ import annotations

import os
import sys
import csv
import io

import requests

API = os.environ.get("TRUSTNODE_API_BASE", "http://127.0.0.1:8000")
USER = os.environ.get("TRUSTNODE_SMOKE_USER", "admin")
PASS = os.environ.get("TRUSTNODE_SMOKE_PASS", "admin")


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(2)


def main() -> int:
    sess = requests.Session()
    r = sess.post(f"{API}/api/auth/login", json={"username": USER, "password": PASS}, timeout=20)
    r.raise_for_status()
    token = r.json().get("token")
    if not token:
        fail("Login returned no token")
    sess.headers.update({"Authorization": f"Bearer {token}"})

    multi_chart_section = {
        "id": "sec-chart-1",
        "type": "line_chart",
        "title": "SimREAL[2] vs SimREAL[3]",
        "time_range": {"preset": "24h"},
        "readings_count": 200,
        "show_legend": True,
        "value_format": "2dp",
        "x_axis_label": "Time",
        "y_axis_label": "Value",
        "y_axis_unit": " u",
        "y_axis_right_label": "Counter",
        "y_axis_right_unit": " c",
        "series": [
            {"id": "s0", "label": "Sim 2", "gateway_id": "gw-1778540283647",
             "tag_name": "SimREAL[2]", "color": "#14a89a", "axis": "left", "unit": "u"},
            {"id": "s1", "label": "Sim 3", "gateway_id": "gw-1778540283647",
             "tag_name": "SimREAL[3]", "color": "#d63838", "axis": "right",
             "unit": "c", "chart_type": "bar"},
        ],
        "limit_lines": [
            {"id": "ll1", "value": 200, "axis": "left", "label": "Hi limit", "color": "#dc2626", "dash": True},
            {"id": "ll2", "value": 100, "axis": "left", "label": "Lo limit", "color": "#0e8479", "dash": True},
        ],
    }

    table_section = {
        "id": "sec-table-1",
        "type": "table",
        "title": "Recent comparison",
        "time_range": {"preset": "24h"},
        "readings_count": 200,
        "row_limit": 12,
        "series": [
            {"id": "s0", "label": "Sim 2", "gateway_id": "gw-1778540283647", "tag_name": "SimREAL[2]", "color": "#14a89a", "unit": "u"},
            {"id": "s1", "label": "Sim 3", "gateway_id": "gw-1778540283647", "tag_name": "SimREAL[3]", "color": "#d63838", "unit": "c"},
        ],
        "columns": [
            {"id": "c0", "key": "ts", "title": "Timestamp"},
            {"id": "c1", "key": "value", "series_id": "s0", "title": "Sim 2", "format": "3dp", "unit": "u"},
            {"id": "c2", "key": "value", "series_id": "s1", "title": "Sim 3", "format": "3dp", "unit": "c"},
            {"id": "c3", "key": "calc", "title": "Sim2 - Sim3", "expr": "a - b", "series_ids": ["s0", "s1"], "format": "3dp", "unit": "diff"},
        ],
    }

    template = {
        "name": "Smoke rich template",
        "description": "Multi-series + table",
        "definition": {
            "sections": [
                {"type": "header", "title": "Smoke Rich Report", "subtitle": "Multi-series & calcs"},
                multi_chart_section,
                table_section,
            ]
        },
    }

    # Render the whole template to PDF
    r = sess.post(f"{API}/api/reports/render", json={"template": template}, timeout=120)
    r.raise_for_status()
    g = r.json().get("generated") or {}
    if g.get("file_bytes", 0) <= 0:
        fail("PDF render produced 0-byte output")
    print(f"OK  PDF render {g.get('file_bytes')} bytes")

    # CSV export of the table section
    r = sess.post(f"{API}/api/reports/export/csv", json={"section": table_section}, timeout=60)
    r.raise_for_status()
    text = r.text
    if not text or "Timestamp" not in text.splitlines()[0]:
        fail(f"CSV export missing header. Got: {text[:120]!r}")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if len(rows) < 2:
        fail(f"CSV has only {len(rows)} rows (need header + body)")
    print(f"OK  CSV export {len(rows)-1} body rows, header={rows[0]}")

    # TXT export of the table section
    r = sess.post(f"{API}/api/reports/export/txt", json={"section": table_section}, timeout=60)
    r.raise_for_status()
    txt = r.text
    if not txt or " | " not in txt.splitlines()[0]:
        fail("TXT export missing pipe-delimited header")
    body_lines = [l for l in txt.splitlines() if l.strip()]
    if len(body_lines) < 2:
        fail("TXT export missing body rows")
    print(f"OK  TXT export {len(body_lines)-1} body rows")

    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
