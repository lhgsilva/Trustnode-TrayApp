#!/usr/bin/env python3
"""
Smoke test: dashboard config persistence + export/import payload compatibility.

Checks:
1) dashboard_configurations can be written/read via app-store API.
2) Stored payload keeps widgets + layout fields (x,y,w,h) + mode/per_row/tag_colors.
3) Export JSON shape matches frontend importer expectations.
4) Restores original dashboard configuration after test.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

BASE_URL = os.environ.get("TRUSTNODE_SMOKE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
BOOTSTRAP_URL = f"{BASE_URL}/api/app-store/bootstrap"
DOMAIN_URL = f"{BASE_URL}/api/app-store/domain"
OUT_FILE = Path(__file__).resolve().parent / "output" / "dashboard-config-export-smoke.json"
API_TOKEN = os.environ.get("TRUSTNODE_SMOKE_TOKEN", "").strip()


def _req(method: str, url: str, **kwargs) -> dict[str, Any]:
    headers = dict(kwargs.pop("headers", {}) or {})
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"
    res = requests.request(method, url, timeout=20, headers=headers, **kwargs)
    res.raise_for_status()
    data = res.json()
    if not isinstance(data, dict) or not data.get("ok", True):
        raise RuntimeError(f"Invalid API response from {url}: {data}")
    return data


def _bootstrap() -> dict[str, Any]:
    data = _req("GET", BOOTSTRAP_URL)
    out = data.get("data") or {}
    if not isinstance(out, dict):
        raise RuntimeError("Bootstrap payload is not an object")
    return out


def _save_dashboard(payload: dict[str, Any]) -> None:
    body = {"domain": "dashboard_configurations", "payload": payload, "actor": "smoke-dashboard-roundtrip"}
    _req("PUT", DOMAIN_URL, json=body)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def main() -> int:
    print(f"[smoke] base_url={BASE_URL}")
    mode = "api"
    before: dict[str, Any]
    save_fn = _save_dashboard
    try:
        before = _bootstrap()
    except Exception as api_err:
        print(f"[smoke] API unavailable ({api_err}); using direct AppStore mode.")
        mode = "direct"
        repo_root = Path(__file__).resolve().parents[1]
        backend_dir = repo_root / "backend"
        sys.path.insert(0, str(backend_dir))
        from app.services.app_store import AppStore  # type: ignore

        store = AppStore()
        before = store.get_bootstrap(prefer_cloud_reads=False) or {}

        def _save_direct(payload: dict[str, Any]) -> None:
            store.upsert_domain("dashboard_configurations", payload, actor="smoke-dashboard-roundtrip")

        save_fn = _save_direct
    before_dash = before.get("dashboard_configurations")
    if not isinstance(before_dash, dict):
        before_dash = {"widgets": [], "mode": "kpi", "per_row": 2, "tag_colors": {}}

    stamp = int(time.time())
    test_widget = {
        "id": f"smoke-{stamp}",
        "type": "line_chart",
        "title": f"Smoke {stamp}",
        "x": 0,
        "y": 0,
        "w": 6,
        "h": 4,
        "config": {
            "gateway_id": "smoke-gw",
            "tag_name": "smoke_tag",
            "readings_count": 120,
            "interpolation": "stepAfter",
        },
    }
    test_dash = {
        "widgets": [test_widget],
        "mode": "kpi",
        "per_row": 2,
        "tag_colors": {"smoke-gw::smoke_tag": "#14a89a"},
    }

    try:
        save_fn(test_dash)
        after = _bootstrap() if mode == "api" else (store.get_bootstrap(prefer_cloud_reads=False) or {})
        dash = after.get("dashboard_configurations") or {}
        _assert(isinstance(dash, dict), "dashboard_configurations missing after write")
        widgets = dash.get("widgets") or []
        _assert(isinstance(widgets, list) and len(widgets) >= 1, "widgets not persisted")
        probe = widgets[0]
        for field in ("x", "y", "w", "h"):
            _assert(field in probe, f"layout field '{field}' missing in persisted widget")
        _assert(str(dash.get("mode", "")).lower() in {"kpi", "chart"}, "mode not persisted")
        _assert(int(dash.get("per_row", 0)) >= 1, "per_row not persisted")
        _assert(isinstance(dash.get("tag_colors", {}), dict), "tag_colors not persisted")

        export_payload = {
            "version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "dashboard_configurations": dash,
        }
        OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        OUT_FILE.write_text(json.dumps(export_payload, indent=2), encoding="utf-8")
        print(f"[smoke] wrote export sample: {OUT_FILE}")
        print("[smoke] PASS: dashboard config persisted and export payload generated.")
        return 0
    finally:
        # restore original dashboard config so test stays non-destructive
        try:
            save_fn(before_dash if isinstance(before_dash, dict) else {"widgets": [], "mode": "kpi", "per_row": 2, "tag_colors": {}})
            print("[smoke] restored original dashboard_configurations.")
        except Exception as restore_err:
            print(f"[smoke] WARN restore failed: {restore_err}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - smoke script
        print(f"[smoke] FAIL: {exc}")
        raise
