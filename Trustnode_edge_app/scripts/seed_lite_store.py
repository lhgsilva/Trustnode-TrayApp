# -*- coding: utf-8 -*-
"""Seed a throwaway app-store with a full-module licence, BEFORE the server boots."""
import os, sys
ROOT = r"D:\Trustnode\Trustnode-AB\Tray_app\Trustnode_edge_app"
sys.path.insert(0, os.path.join(ROOT, "backend"))
from app.state import app_store
from app.services.control_plane_store import ControlPlaneStore
mods = [{"key": m["key"], "enabled": True} for m in ControlPlaneStore.MODULE_CATALOG]
bs = app_store.get_bootstrap(prefer_cloud_reads=False) or {}
s = dict(bs.get("app_settings") or {})
s["license"] = {"license_id": "lic-lite-test", "status": "active",
                "start_utc": "2026-01-01 00:00:00", "end_utc": "2030-01-01 00:00:00",
                "modules": mods}
app_store.upsert_domain("app_settings", s, actor="lite-test-seed")
print(f"seeded {len(mods)} modules")
