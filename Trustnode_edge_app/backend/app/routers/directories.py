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
`app_settings.directories.<key>`, under THIS EDGE's scope.

WHICH WRITERS ACTUALLY ASK (checked 2026-10-02): only the reports path, via
resolve_directory("reports"). This docstring used to claim "honored by every
relevant write path" and that was not true - historian exports, log dumps,
backups, CSV sinks and template storage each resolve their own location, so
setting those four rows here changes nothing on disk. The page says so per
row rather than offering a control that does nothing. Wiring a writer up is
a one-line resolve_directory() call in that writer, not a change here.

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

# Which keys a writer ACTUALLY resolves through resolve_directory(). Checked
# by grep, 2026-10-02: only reports. The others are shown as the location
# their files go to, and the page says the override is not wired yet rather
# than taking a path and ignoring it. Add a key here the same day you add the
# resolve_directory() call to that writer.
_HONORED_KEYS = frozenset({"reports"})


def _default_base() -> Path:
    # Windows: %USERPROFILE%\Documents\TrustNode  (familiar to operators).
    # Linux/Mac: ~/.trustnode_edge/data
    if sys.platform == "win32":
        return Path(os.path.expandvars("%USERPROFILE%")) / "Documents" / "TrustNode"
    return Path.home() / ".trustnode_edge" / "data"


def _default_for(key: str) -> Path:
    return _default_base() / key


def _read_app_settings() -> Dict[str, Any] | None:
    """This EDGE's app_settings document, or None for "could not read it".

    ONE domain, off the global lock: get_bootstrap() builds EVERY domain
    behind the lock the historian writer holds, and on a multi-gigabyte store
    under load this page simply never answered and rendered an empty table
    (2026-10-02).

    get_domain_scoped_fast(), NOT get_domain_fast(). The first version of
    this used the plain fast read, which goes to `config_documents` - the
    LEGACY UNSCOPED table that one SQLite file shares across every customer
    and edge it has ever been activated for. Serving that as "this edge's
    folders" is a cross-scope read, not a display bug. The scoped reader
    takes this edge's scope key, reads its own document, and falls back to
    unscoped only when this edge has none - which is also where a pre-scoping
    install's existing overrides still live, so nothing is lost.

    None means "unknown". A caller must never treat it as "empty": writing
    back a document you failed to read is how configs got wiped four times
    (config_wipe_on_failed_read).
    """
    try:
        doc = app_store.get_domain_scoped_fast("app_settings")
    except Exception:
        return None
    return doc if isinstance(doc, dict) else None


def _load_overrides() -> Dict[str, str]:
    s = _read_app_settings()
    if s is None:
        # Unknown, so show the defaults - a read failure must not look like
        # "the operator cleared every override".
        return {}
    d = s.get("directories") if isinstance(s.get("directories"), dict) else {}
    return {str(k): str(v) for k, v in d.items() if v}


def _save_overrides(overrides: Dict[str, str]) -> None:
    """Persist the overrides into THIS EDGE's app_settings document.

    Two rules this did not follow before:

    * It read the current document with a bare `or {}`, so a failed read
      became an empty document and the save then wiped every OTHER key in
      app_settings. That is the blank-write trap, four incidents deep. A read
      we could not complete now refuses the save instead.
    * It wrote with upsert_domain() - unscoped. Where a save goes has to
      match where the read looks, or an operator sets a folder, the scoped
      read does not see it, and the page shows the default back.
    """
    current = _read_app_settings()
    if current is None:
        raise HTTPException(
            status_code=503,
            detail="Could not read the current settings, so nothing was "
                   "saved - the store is busy. Try again in a moment.")
    s = dict(current)
    # Drop empty / falsy values so the doc stays clean.
    clean = {k: str(v) for k, v in overrides.items() if str(v or "").strip()}
    s["directories"] = clean
    scope_key = ""
    try:
        scope_key = str(app_store.site_scope_key() or "")
    except Exception:
        scope_key = ""
    if scope_key:
        app_store.upsert_domain_scoped(scope_key, "app_settings", s,
                                       actor="directories_router")
    else:
        # No edge identity yet: unscoped is where this install's reads come
        # from too, so that is where its writes belong.
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
            # False means: this is where those files go, but setting a path
            # here does not move them yet.
            "honored": key in _HONORED_KEYS,
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

class PathCheckRequest(BaseModel):
    path: str = ""
    # When true the folder is created if missing, so "Save to" can point at a
    # folder the operator has not made yet.
    create: bool = False


@router.post("/validate")
def validate_path(payload: PathCheckRequest) -> dict:
    """Is this a folder we can actually write a report into?

    2026-10-07, requested: the report "Save to" box was free text, so a typo
    or an un-synced OneDrive folder was only discovered when a scheduled run
    silently failed hours later. The UI calls this as the operator types.

    A pattern may legitimately contain {tokens} that are only resolved at run
    time ({date}, a tag value). Those are checked as far as the first token -
    the fixed leading part of the path - because that is the part that has to
    exist today.
    """
    raw = str(payload.path or "").strip()
    if not raw:
        return {"ok": True, "state": "default",
                "message": "Using the default reports folder."}

    # Keep only the directory part, and only up to the first token.
    text = raw.replace("\\", "/")
    head = text.split("{", 1)[0]
    folder = head.rsplit("/", 1)[0] if "/" in head else ""
    if not folder:
        # No directory at all: just a file name, which lands in the default
        # reports folder. That is valid.
        return {"ok": True, "state": "default",
                "message": "No folder given - saved in the default reports folder."}

    try:
        target = Path(os.path.expandvars(os.path.expanduser(folder))).resolve()
    except Exception as exc:                            # noqa: BLE001
        return {"ok": False, "state": "invalid",
                "message": "That is not a usable path: %s" % str(exc)[:120]}

    if payload.create:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as exc:                        # noqa: BLE001
            return {"ok": False, "state": "denied", "path": str(target),
                    "message": "Could not create that folder: %s" % str(exc)[:120]}

    info = _probe(target)
    if not info["exists"]:
        return {"ok": False, "state": "missing", "path": str(target),
                "message": "That folder does not exist yet."}
    if not info["is_dir"]:
        return {"ok": False, "state": "not_a_folder", "path": str(target),
                "message": "That path is a file, not a folder."}
    if not info["writable"]:
        return {"ok": False, "state": "read_only", "path": str(target),
                "message": "That folder exists but cannot be written to."}
    return {"ok": True, "state": "ok", "path": str(target),
            "message": "Folder exists and is writable."}
