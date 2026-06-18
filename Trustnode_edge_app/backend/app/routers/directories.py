"""Directories router (operator 2026-06-18).

Lets the operator pick where each kind of generated file lives.
Covered:
  reports   — generated PDFs from the Reports page
  exports   — historian XLSX/CSV exports
  logs      — app_logs CSV dumps / debug logs
  backups   — manual local-SQLite backups
  csv       — gateway CSV-file sinks (per-sink override still wins)
  templates — report template ZIPs / customer branding

Each key has a sensible default (Documents\TrustNode\<key> on Windows;
~/.trustnode_edge/data/<key> elsewhere). Operator overrides persist in
`app_settings.directories.<key>` and are honored by every relevant
write path.

Endpoints (all admin-only via the existing auth middleware):
  GET  /api/directories               → list with current + default paths
  POST /api/directories               → set one or more overrides
  POST /api/directories/reset/{key}   → revert to default
  POST /api/directories/open/{key}    → reveal in OS file manager
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.state import app_store

router = APIRouter(prefix="/api/directories", tags=["directories"])


# Order matters: this drives the UI list order.
_KNOWN_DIRS = [
    ("reports",   "Generated Reports",     "Where PDF reports land after generation"),
    ("exports",   "Historian Exports",     "XLSX / CSV exports from the Historian page"),
    ("logs",      "Application Logs",      "Diagnostic logs + tamper-evident audit"),
    ("backups",   "Database Backups",      "Manual SQLite backups (Database → Backup)"),
    ("csv",       "CSV Gateway Sinks",     "Default folder for per-gateway CSV writers"),
    ("templates", "Report Templates",      "Report template ZIPs + customer brand assets"),
]


def _default_base() -> Path:
    # Windows: %USERPROFILE%\Documents\TrustNode  (familiar to operators).
    # Linux/Mac: ~/.trustnode_edge/data
    if sys.platform == "win32":
        return Path(os.path.expandvars("%USERPROFILE%")) / "Documents" / "TrustNode"
    return Path.home() / ".trustnode_edge" / "data"


def _default_for(key: str) -> Path:
    return _default_base() / key


def _load_overrides() -> Dict[str, str]:
    try:
        bootstrap = app_store.get_bootstrap() or {}
    except Exception:
        return {}
    s = bootstrap.get("app_settings") if isinstance(bootstrap.get("app_settings"), dict) else {}
    d = s.get("directories") if isinstance(s.get("directories"), dict) else {}
    return {str(k): str(v) for k, v in d.items() if v}


def _save_overrides(overrides: Dict[str, str]) -> None:
    try:
        bootstrap = app_store.get_bootstrap() or {}
    except Exception:
        bootstrap = {}
    s = dict(bootstrap.get("app_settings") or {})
    # Drop empty / falsy values so the doc stays clean.
    clean = {k: str(v) for k, v in overrides.items() if str(v or "").strip()}
    s["directories"] = clean
    app_store.upsert_domain("app_settings", s, actor="directories_router")


def _resolve(key: str) -> Path:
    """Returns the path to use right now for `key` — operator override
    if set, otherwise the default. Resolved + ~-expanded.
    """
    overrides = _load_overrides()
    raw = overrides.get(key) or ""
    if raw:
        return Path(os.path.expanduser(raw)).resolve()
    return _default_for(key).resolve()


def _probe(p: Path) -> Dict[str, Any]:
    info = {"exists": False, "writable": False, "is_dir": False}
    try:
        info["exists"] = p.exists()
        info["is_dir"] = p.is_dir()
        if info["exists"] and info["is_dir"]:
            # Try creating a temp file to confirm write permission.
            probe = p / f".tn-write-probe.{os.getpid()}"
            try:
                probe.write_text("x")
                probe.unlink()
                info["writable"] = True
            except Exception:
                info["writable"] = False
    except Exception:
        pass
    return info


# -- Public path resolver. Other services import this when they need to
# write a file so the operator's override is honored. -------------------

def resolve_directory(key: str, create: bool = True) -> Path:
    """Module-level helper: return the current path for `key`, optionally
    creating it if missing. Always falls back to the default.
    """
    p = _resolve(key)
    if create:
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    return p


# -- HTTP API -----------------------------------------------------------

class DirectoriesUpdateRequest(BaseModel):
    overrides: Dict[str, str]


@router.get("")
def get_directories() -> dict:
    overrides = _load_overrides()
    rows: list[dict[str, Any]] = []
    for key, label, hint in _KNOWN_DIRS:
        default_path = str(_default_for(key))
        current_path = str(_resolve(key))
        info = _probe(Path(current_path))
        rows.append({
            "key": key,
            "label": label,
            "hint": hint,
            "default_path": default_path,
            "current_path": current_path,
            "is_overridden": key in overrides,
            **info,
        })
    return {"ok": True, "rows": rows}


@router.post("")
def post_directories(payload: DirectoriesUpdateRequest) -> dict:
    overrides = _load_overrides()
    for k, v in (payload.overrides or {}).items():
        key = str(k).strip()
        if not key:
            continue
        if any(key == d[0] for d in _KNOWN_DIRS):
            val = str(v or "").strip()
            if val:
                # Create on save so the UI feedback is honest.
                try:
                    Path(os.path.expanduser(val)).mkdir(parents=True, exist_ok=True)
                except Exception:
                    pass
                overrides[key] = val
            else:
                overrides.pop(key, None)
    _save_overrides(overrides)
    return get_directories()


@router.post("/reset/{key}")
def reset_directory(key: str) -> dict:
    overrides = _load_overrides()
    overrides.pop(str(key), None)
    _save_overrides(overrides)
    return get_directories()


@router.post("/open/{key}")
def open_directory(key: str) -> dict:
    """Reveal the directory in the OS file manager. Best-effort —
    the backend may run on the customer's headless plant PC where no
    desktop is available, in which case this simply returns {opened: false}.
    """
    target = _resolve(str(key))
    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    opened = False
    try:
        if sys.platform == "win32":
            os.startfile(str(target))  # type: ignore[attr-defined]
            opened = True
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
            opened = True
        else:
            subprocess.Popen(["xdg-open", str(target)])
            opened = True
    except Exception:
        opened = False
    return {"ok": True, "key": key, "path": str(target), "opened": opened}
