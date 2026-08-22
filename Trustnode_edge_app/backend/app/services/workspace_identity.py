"""Which workspace is this process serving, and is it the one this machine uses?

2026-08-22 incident. The desktop app was launched from a shell that still had
`TRUSTNODE_DATA_DIR` pointing at a throwaway test workspace
(`%TEMP%\\tn_test_8000`). desktop/main.js honours that variable unconditionally,
so the app opened an EMPTY store and then did exactly the right thing for an
empty store: no gateways, nothing collecting, no licence modules, empty Tags and
Devices pages.

From the operator's chair that is indistinguishable from "the software deleted
all my data" — while the real workspace (9.9 M readings and every config) sat
untouched one directory away. Nothing warned anybody, because by its own lights
nothing was wrong: each layer behaved correctly for the workspace it was handed.

This module asks the question nobody was asking out loud: *is the workspace I am
serving the one this machine normally uses, and if not, does the usual one hold
data I am now hiding?* `/api/health` publishes the answer and the UI turns it
into a banner, so a substituted workspace announces itself instead of looking
like data loss.

Read-only and best-effort: it must never raise, and it never opens a database
for writing.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from typing import Any, Dict


def default_data_dir() -> str:
    """The workspace a normal install uses when nothing overrides it.

    Kept in step with AppStore._resolve_db_path() and desktop/main.js
    resolveBackendDataDir(): the legacy ~/.trustnode_edge/data wins when it
    exists, because that is where existing installs keep their data.
    """
    legacy = os.path.join(os.path.expanduser("~"), ".trustnode_edge", "data")
    if sys.platform == "win32":
        if os.path.isdir(legacy):
            return legacy
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return os.path.join(program_data, "TrustNode", "edge")
    return legacy


def _has_readings(db_path: str) -> int:
    """1 when the historian in `db_path` holds at least one reading, 0 when it is
    empty, -1 when it cannot be read.

    Deliberately NOT a COUNT(*): this runs on the health path and a real store is
    multi-gigabyte, where counting takes tens of seconds. `LIMIT 1` answers the
    only question that matters here in constant time.
    """
    if not db_path or not os.path.exists(db_path):
        return -1
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            con.execute("PRAGMA query_only=1")
            row = con.execute("SELECT 1 FROM historian_readings LIMIT 1").fetchone()
            return 1 if row else 0
        finally:
            con.close()
    except Exception:
        return -1


def _norm(path: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(path or ""))
    except Exception:
        return str(path or "")


_cache: Dict[str, Any] | None = None


def summary() -> Dict[str, Any]:
    """`describe()`, computed once per process.

    The workspace a process serves cannot change while it runs, so the two
    SQLite probes are paid exactly once. /api/health is on the boot-critical
    path and must never grow a per-request database open.
    """
    global _cache
    if _cache is None:
        try:
            _cache = describe()
        except Exception:
            _cache = {"data_dir": "", "is_default": True, "hiding_real_data": False,
                      "warning": ""}
    return _cache


def describe() -> Dict[str, Any]:
    """A small, JSON-safe summary of the workspace this process is serving."""
    try:
        from app.state import app_store
        db_path = str(getattr(app_store, "_db_path", "") or "")
    except Exception:
        db_path = ""
    data_dir = os.path.dirname(db_path) if db_path else os.environ.get(
        "TRUSTNODE_DATA_DIR", "").strip()
    default_dir = default_data_dir()
    default_db = os.path.join(default_dir, "trustnode_app_store.db")

    is_default = _norm(data_dir) == _norm(default_dir)
    # Why this process is somewhere else, when it is.
    source = ""
    if not is_default:
        if os.environ.get("TRUSTNODE_APP_STORE_PATH", "").strip():
            source = "TRUSTNODE_APP_STORE_PATH"
        elif os.environ.get("TRUSTNODE_DATA_DIR", "").strip():
            source = "TRUSTNODE_DATA_DIR"
        else:
            source = "resolved"

    here = _has_readings(db_path)
    # Only probe the second store when the answer can still change.
    there = -1 if is_default else _has_readings(default_db)

    # The case worth shouting about: serving a substitute workspace that holds
    # nothing, while the machine's usual one holds real data.
    hiding = bool(not is_default and here == 0 and there == 1)

    return {
        "data_dir": data_dir,
        "db_path": db_path,
        "is_default": is_default,
        "override_source": source,
        "has_data_here": here,
        "default_data_dir": default_dir,
        "default_has_data": there,
        "hiding_real_data": hiding,
        "warning": (
            f"This app is running against {data_dir}, which holds no collected "
            f"data, while the workspace this machine normally uses "
            f"({default_dir}) does. Nothing has been deleted - close the app and "
            f"start it without {source or 'the data-dir override'} set."
        ) if hiding else "",
    }
