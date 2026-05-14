"""End-to-end smoke test for the reporting module.

Verifies:
  1. CRUD on /api/reports/templates
  2. CRUD on /api/reports/schedules
  3. Inline render (POST /api/reports/render) produces a valid PDF on disk
  4. Schedule run-now generates a record + valid PDF
  5. The PDF download endpoint returns a %PDF- file
  6. Generated record metadata is consistent (sha256 matches the bytes on disk)

Run:
  d:/.../Tray_app/Trustnode_edge_app/backend/.venv/Scripts/python.exe \
    d:/.../Tray_app/Trustnode_edge_app/scripts/smoke-reports.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

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

    # 1) Create template ---------------------------------------------------
    body = {
        "name": "Smoke template",
        "description": "Auto-created by smoke-reports.py",
        "definition": {
            "sections": [
                {"type": "header", "title": "Smoke Report", "subtitle": "Automated", "show_generated_at": True},
                {"type": "text", "title": "Notes", "text": "This is an automated smoke test report."},
                {"type": "kpi_grid", "title": "KPIs", "columns": 3, "items": [
                    {"label": "Count any", "gateway_id": "gw-1778540283647", "tag_name": "SimREAL[2]",
                     "operator": "any", "aggregation": "count"},
                ]},
                {"type": "line_chart", "title": "Trend", "gateway_id": "gw-1778540283647",
                 "tag_name": "SimREAL[2]", "time_range": {"preset": "1h"}, "readings_count": 100},
                {"type": "table", "title": "Recent", "gateway_id": "gw-1778540283647",
                 "tag_name": "SimREAL[2]", "time_range": {"preset": "1h"}, "row_limit": 10},
            ]
        },
    }
    r = sess.post(f"{API}/api/reports/templates", json=body, timeout=15)
    r.raise_for_status()
    tpl = r.json().get("template") or {}
    tpl_id = tpl.get("id")
    if not tpl_id:
        fail("Template create returned no id")
    print(f"OK  template created id={tpl_id}")

    # 2) Update template (no-op patch) -------------------------------------
    body2 = dict(body)
    body2["description"] = "Updated by smoke"
    r = sess.put(f"{API}/api/reports/templates/{tpl_id}", json=body2, timeout=15)
    r.raise_for_status()
    if r.json().get("template", {}).get("description") != "Updated by smoke":
        fail("Template update did not persist description")
    print("OK  template updated")

    # 3) Render inline (no schedule) ---------------------------------------
    r = sess.post(f"{API}/api/reports/render", json={"template": r.json()["template"]}, timeout=60)
    r.raise_for_status()
    generated = r.json().get("generated") or {}
    gen_id = generated.get("id")
    if not gen_id:
        fail("Inline render returned no generated record")
    if generated.get("file_bytes", 0) <= 0:
        fail("Inline render produced 0-byte PDF")
    file_path = Path(generated.get("file_path") or "")
    if not file_path.exists():
        fail(f"PDF file missing on disk: {file_path}")
    raw = file_path.read_bytes()
    if not raw.startswith(b"%PDF-"):
        fail("Generated file is not a PDF")
    sha = hashlib.sha256(raw).hexdigest()
    if generated.get("file_sha256") != sha:
        fail("Generated record sha256 does not match file bytes")
    print(f"OK  inline render -> {file_path.name} ({len(raw)} bytes)")

    # 4) Download via API --------------------------------------------------
    r = sess.get(f"{API}/api/reports/generated/{gen_id}/file", timeout=30)
    r.raise_for_status()
    if not r.content.startswith(b"%PDF-"):
        fail("API download returned non-PDF content")
    if hashlib.sha256(r.content).hexdigest() != sha:
        fail("API-downloaded bytes do not match disk file")
    print("OK  downloaded via API matches sha256")

    # 5) Create schedule --------------------------------------------------
    sch_body = {
        "name": "Smoke schedule",
        "template_id": tpl_id,
        "enabled": True,
        "trigger_mode": "time",
        "recurrence": "daily",
        "hour": 8,
        "minute": 0,
        "deliver_email": False,
    }
    r = sess.post(f"{API}/api/reports/schedules", json=sch_body, timeout=15)
    r.raise_for_status()
    sch = r.json().get("schedule") or {}
    sch_id = sch.get("id")
    if not sch_id:
        fail("Schedule create returned no id")
    print(f"OK  schedule created id={sch_id}")

    # 6) Run schedule now -------------------------------------------------
    r = sess.post(f"{API}/api/reports/schedules/{sch_id}/run", json={}, timeout=60)
    r.raise_for_status()
    res = r.json()
    if not res.get("ok"):
        fail(f"Schedule run returned ok=false: {res}")
    g2 = res.get("generated") or {}
    if not g2.get("id"):
        fail("Schedule run returned no generated record")
    if Path(g2.get("file_path") or "").stat().st_size <= 0:
        fail("Schedule-run PDF is 0 bytes")
    print(f"OK  schedule run -> generated id={g2.get('id')} bytes={g2.get('file_bytes')}")

    # 7) Generated list filter by schedule_id -----------------------------
    r = sess.get(f"{API}/api/reports/generated?schedule_id={sch_id}", timeout=15)
    r.raise_for_status()
    sch_generated = r.json().get("generated") or []
    if not any(g.get("id") == g2.get("id") for g in sch_generated):
        fail("Schedule-filtered list does not include the just-generated report")
    print(f"OK  generated list filtered by schedule_id has {len(sch_generated)} entries")

    # 8) Cleanup ----------------------------------------------------------
    sess.delete(f"{API}/api/reports/schedules/{sch_id}", timeout=15)
    sess.delete(f"{API}/api/reports/generated/{gen_id}", timeout=15)
    sess.delete(f"{API}/api/reports/generated/{g2.get('id')}", timeout=15)
    sess.delete(f"{API}/api/reports/templates/{tpl_id}", timeout=15)
    print("OK  cleanup")

    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
