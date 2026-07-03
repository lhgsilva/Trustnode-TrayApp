"""SQLite storage for chats + insights. Tables live in the existing
app_store.db so we don't grow the storage story. Lazy schema creation on
first call so the module is install-and-go.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_chats (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    user_id       TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT 'New chat',
    data_source   TEXT NOT NULL DEFAULT 'local',
    created_utc   TEXT NOT NULL,
    updated_utc   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_messages (
    id            TEXT PRIMARY KEY,
    chat_id       TEXT NOT NULL,
    role          TEXT NOT NULL,
    content       TEXT NOT NULL,
    tool_calls    TEXT,
    tool_results  TEXT,
    created_utc   TEXT NOT NULL,
    FOREIGN KEY (chat_id) REFERENCES ai_chats(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_ai_messages_chat ON ai_messages(chat_id, created_utc);

CREATE TABLE IF NOT EXISTS ai_insights (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL DEFAULT 'default',
    user_id         TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    prompt          TEXT NOT NULL,
    tool_plan       TEXT NOT NULL,
    data_source     TEXT NOT NULL DEFAULT 'local',
    schedule_cron   TEXT NOT NULL DEFAULT '',
    email_to        TEXT NOT NULL DEFAULT '',
    last_run_utc    TEXT,
    last_result     TEXT,
    last_error      TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_utc     TEXT NOT NULL,
    updated_utc     TEXT NOT NULL
);

-- Operator 2026-06-30: per-run history. One row per insight execution
-- (manual via Run-now OR scheduled). Used by the Insights page's right
-- column to show the timeline of generated results. Auto-pruned FIFO
-- to last RUN_HISTORY_CAP rows per insight on every insert.
CREATE TABLE IF NOT EXISTS ai_insight_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    insight_id    TEXT NOT NULL,
    triggered_by  TEXT NOT NULL DEFAULT 'manual',   -- 'manual' | 'schedule'
    started_utc   TEXT NOT NULL,
    finished_utc  TEXT NOT NULL,
    ok            INTEGER NOT NULL DEFAULT 1,
    content       TEXT,
    error         TEXT,
    tool_results  TEXT,
    FOREIGN KEY (insight_id) REFERENCES ai_insights(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_ai_insight_runs_insight ON ai_insight_runs(insight_id, started_utc DESC);

-- Operator 2026-07-03: customer-editable predefined queries (the starter-query
-- palette). One row per tenant holding the full palette JSON. IF NOT EXISTS +
-- per-tenant means: FRESH install has no row (→ serve built-in defaults), an
-- UPGRADE just adds the table without touching data, and once the customer
-- customizes their palette it persists here and survives future upgrades.
CREATE TABLE IF NOT EXISTS ai_presets (
    tenant_id     TEXT PRIMARY KEY,
    palette_json  TEXT NOT NULL,
    updated_utc   TEXT NOT NULL
);
"""

# Per-insight cap on the runs table (FIFO trim on insert). Keeps the
# table bounded even if someone schedules an insight every minute.
RUN_HISTORY_CAP = 100


def _now_utc() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


import os as _os


def _db_path() -> str:
    # Operator 2026-07-02: use a DEDICATED sqlite file for the Intelligence
    # module — NOT the shared app_store.db.
    #
    # Why: the intelligence tables used to live inside app_store.db, which
    # the main backend writes to constantly (config, telemetry, historian).
    # Two independent connection pools hammering the same file fought over
    # the SQLite write lock; intelligence writes lost the fight and SILENTLY
    # failed to persist (a created chat / sent message vanished on the next
    # read), while read bursts wedged the module. A separate file has ZERO
    # contention with the main store — chats/messages/insights always persist
    # and never block the main backend.
    #
    # The file sits next to app_store.db in the same data dir so backups and
    # the workspace detector pick it up automatically.
    from app.state import app_store as _store  # type: ignore
    main_db = getattr(_store, "_db_path", "") or ""
    if main_db:
        return _os.path.join(_os.path.dirname(main_db), "trustnode_intelligence.db")
    return "trustnode_intelligence.db"


def _connect() -> sqlite3.Connection:
    # Dedicated DB → we own the file. Enable WAL once (persistent setting;
    # cheap no-op on subsequent connects since it's already WAL) and set a
    # busy_timeout as a belt-and-braces guard. No contention with app_store.
    path = _db_path()
    con = sqlite3.connect(path, timeout=5.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=ON")
    return con


# Schema is created ONCE per process, not on every call. Running the
# CREATE TABLE / DDL block on every create_chat took a write lock on the
# shared DB and was a major source of lock contention.
_SCHEMA_READY = False
import threading as _threading
_SCHEMA_LOCK = _threading.Lock()


def ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    # Operator 2026-07-02: guard with a lock + double-check so concurrent
    # first-calls from the DB executor threads don't both run the DDL and
    # the one-time migration (which was a 1-3s double-open of the shared DB).
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        with _connect() as con:
            for stmt in SCHEMA.strip().split(";\n"):
                stmt = stmt.strip()
                if stmt:
                    con.execute(stmt)
            con.commit()
        _SCHEMA_READY = True
    # One-time migration: copy any pre-existing ai_* rows from the OLD
    # shared app_store.db into the new dedicated DB, so users don't lose
    # their history when we split the storage. Best-effort + idempotent
    # (INSERT OR IGNORE by primary key). Runs once per process.
    _migrate_from_shared_db_once()


def _migrate_from_shared_db_once() -> None:
    try:
        from app.state import app_store as _store  # type: ignore
        shared = getattr(_store, "_db_path", "") or ""
        if not shared or not _os.path.exists(shared):
            return
        dedicated = _db_path()
        if _os.path.abspath(shared) == _os.path.abspath(dedicated):
            return  # nothing to migrate (paths coincide)
        src = sqlite3.connect(shared, timeout=5.0)
        src.row_factory = sqlite3.Row
        # If the shared DB has no ai_chats table, nothing to do.
        has = src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_chats'"
        ).fetchone()
        if not has:
            src.close()
            return
        with _connect() as dst:
            for table, cols in (
                ("ai_chats", "id, tenant_id, user_id, title, data_source, created_utc, updated_utc"),
                ("ai_messages", "id, chat_id, role, content, tool_calls, tool_results, created_utc"),
                ("ai_insights", "id, tenant_id, user_id, title, description, prompt, tool_plan, "
                                "data_source, schedule_cron, email_to, last_run_utc, last_result, "
                                "last_error, enabled, created_utc, updated_utc"),
            ):
                try:
                    rows = src.execute(f"SELECT {cols} FROM {table}").fetchall()
                except Exception:
                    continue
                if not rows:
                    continue
                placeholders = ",".join("?" for _ in cols.split(","))
                for r in rows:
                    try:
                        dst.execute(
                            f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({placeholders})",
                            tuple(r),
                        )
                    except Exception:
                        continue
            dst.commit()
        src.close()
    except Exception:
        # Migration is best-effort; a failure must never block the module.
        pass


# --- chats ----------------------------------------------------------------

def create_chat(tenant_id: str, user_id: str, title: str, data_source: str = "local") -> str:
    ensure_schema()
    chat_id = f"chat-{uuid.uuid4().hex[:12]}"
    now = _now_utc()
    with _connect() as con:
        con.execute(
            "INSERT INTO ai_chats(id, tenant_id, user_id, title, data_source, created_utc, updated_utc) "
            "VALUES (?,?,?,?,?,?,?)",
            (chat_id, tenant_id, user_id, title or "New chat", data_source, now, now),
        )
        con.commit()
    return chat_id


def list_chats(tenant_id: str, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    ensure_schema()
    with _connect() as con:
        # Operator 2026-07-02: include this user's chats PLUS any legacy
        # chats stored with an empty user_id (created before the auth-ctx
        # fix, when every chat was silently scoped to ''). Same tenant only.
        # This makes existing history reappear instead of looking "lost".
        rows = con.execute(
            "SELECT id, title, data_source, created_utc, updated_utc "
            "FROM ai_chats WHERE tenant_id=? AND (user_id=? OR user_id='') "
            "ORDER BY updated_utc DESC LIMIT ?",
            (tenant_id, user_id, int(limit)),
        ).fetchall()
    return [
        {"id": r[0], "title": r[1], "data_source": r[2],
         "created_utc": r[3], "updated_utc": r[4]}
        for r in rows
    ]


def get_chat(chat_id: str) -> Optional[Dict[str, Any]]:
    ensure_schema()
    with _connect() as con:
        row = con.execute(
            "SELECT id, tenant_id, user_id, title, data_source, created_utc, updated_utc "
            "FROM ai_chats WHERE id=?",
            (chat_id,),
        ).fetchone()
    if not row:
        return None
    return {"id": row[0], "tenant_id": row[1], "user_id": row[2], "title": row[3],
            "data_source": row[4], "created_utc": row[5], "updated_utc": row[6]}


def delete_chat(chat_id: str) -> None:
    ensure_schema()
    with _connect() as con:
        con.execute("DELETE FROM ai_chats WHERE id=?", (chat_id,))
        con.execute("DELETE FROM ai_messages WHERE chat_id=?", (chat_id,))
        con.commit()


def rename_chat(chat_id: str, title: str) -> None:
    ensure_schema()
    with _connect() as con:
        con.execute("UPDATE ai_chats SET title=?, updated_utc=? WHERE id=?",
                    (title, _now_utc(), chat_id))
        con.commit()


def set_chat_data_source(chat_id: str, data_source: str) -> None:
    ensure_schema()
    with _connect() as con:
        con.execute("UPDATE ai_chats SET data_source=?, updated_utc=? WHERE id=?",
                    (data_source, _now_utc(), chat_id))
        con.commit()


# --- messages -------------------------------------------------------------

def append_message(chat_id: str, role: str, content: str,
                   tool_calls: Optional[Any] = None,
                   tool_results: Optional[Any] = None) -> str:
    ensure_schema()
    msg_id = f"msg-{uuid.uuid4().hex[:12]}"
    now = _now_utc()
    with _connect() as con:
        con.execute(
            "INSERT INTO ai_messages(id, chat_id, role, content, tool_calls, tool_results, created_utc) "
            "VALUES (?,?,?,?,?,?,?)",
            (msg_id, chat_id, role, content,
             json.dumps(tool_calls) if tool_calls else None,
             json.dumps(tool_results) if tool_results else None,
             now),
        )
        con.execute("UPDATE ai_chats SET updated_utc=? WHERE id=?", (now, chat_id))
        con.commit()
    return msg_id


def list_messages(chat_id: str) -> List[Dict[str, Any]]:
    ensure_schema()
    with _connect() as con:
        rows = con.execute(
            "SELECT id, role, content, tool_calls, tool_results, created_utc "
            "FROM ai_messages WHERE chat_id=? ORDER BY created_utc ASC",
            (chat_id,),
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r[0], "role": r[1], "content": r[2],
            "tool_calls": json.loads(r[3]) if r[3] else None,
            "tool_results": json.loads(r[4]) if r[4] else None,
            "created_utc": r[5],
        })
    return out


# --- insights -------------------------------------------------------------

def create_insight(tenant_id: str, user_id: str, title: str, description: str,
                   prompt: str, tool_plan: List[Dict[str, Any]],
                   data_source: str = "local", schedule_cron: str = "",
                   email_to: str = "") -> str:
    ensure_schema()
    iid = f"ins-{uuid.uuid4().hex[:12]}"
    now = _now_utc()
    with _connect() as con:
        con.execute(
            "INSERT INTO ai_insights(id, tenant_id, user_id, title, description, prompt, "
            "tool_plan, data_source, schedule_cron, email_to, enabled, created_utc, updated_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?)",
            (iid, tenant_id, user_id, title, description, prompt,
             json.dumps(tool_plan), data_source, schedule_cron, email_to, now, now),
        )
        con.commit()
    return iid


def list_insights(tenant_id: str, user_id: str) -> List[Dict[str, Any]]:
    ensure_schema()
    with _connect() as con:
        rows = con.execute(
            "SELECT id, title, description, schedule_cron, email_to, last_run_utc, "
            "last_result, last_error, enabled, data_source, created_utc, updated_utc "
            "FROM ai_insights WHERE tenant_id=? AND user_id=? ORDER BY updated_utc DESC",
            (tenant_id, user_id),
        ).fetchall()
    return [
        {"id": r[0], "title": r[1], "description": r[2], "schedule_cron": r[3],
         "email_to": r[4], "last_run_utc": r[5], "last_result": r[6],
         "last_error": r[7], "enabled": bool(r[8]), "data_source": r[9],
         "created_utc": r[10], "updated_utc": r[11]}
        for r in rows
    ]


def get_insight(insight_id: str) -> Optional[Dict[str, Any]]:
    ensure_schema()
    with _connect() as con:
        row = con.execute(
            "SELECT id, tenant_id, user_id, title, description, prompt, tool_plan, "
            "data_source, schedule_cron, email_to, last_run_utc, last_result, "
            "last_error, enabled, created_utc, updated_utc "
            "FROM ai_insights WHERE id=?",
            (insight_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "tenant_id": row[1], "user_id": row[2], "title": row[3],
        "description": row[4], "prompt": row[5],
        "tool_plan": json.loads(row[6]) if row[6] else [],
        "data_source": row[7], "schedule_cron": row[8], "email_to": row[9],
        "last_run_utc": row[10], "last_result": row[11], "last_error": row[12],
        "enabled": bool(row[13]),
        "created_utc": row[14], "updated_utc": row[15],
    }


def update_insight_run(insight_id: str, result: Optional[str], error: Optional[str]) -> None:
    ensure_schema()
    with _connect() as con:
        con.execute(
            "UPDATE ai_insights SET last_run_utc=?, last_result=?, last_error=?, updated_utc=? "
            "WHERE id=?",
            (_now_utc(), result, error, _now_utc(), insight_id),
        )
        con.commit()


def record_insight_run(
    insight_id: str,
    *,
    triggered_by: str,
    started_utc: str,
    finished_utc: str,
    ok: bool,
    content: Optional[str],
    error: Optional[str],
    tool_results: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """Persist a single insight execution to ai_insight_runs and
    auto-prune the per-insight history to RUN_HISTORY_CAP rows.
    Returns the inserted row id. Safe to call from the scheduler
    thread (each connection is its own transaction).
    """
    ensure_schema()
    tr_json = json.dumps(tool_results or [], default=str) if tool_results is not None else None
    with _connect() as con:
        cur = con.execute(
            "INSERT INTO ai_insight_runs"
            " (insight_id, triggered_by, started_utc, finished_utc, ok, content, error, tool_results)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (insight_id, str(triggered_by or "manual"), started_utc, finished_utc,
             1 if ok else 0, content, error, tr_json),
        )
        new_id = int(cur.lastrowid or 0)
        # FIFO trim: keep the newest RUN_HISTORY_CAP rows for this insight.
        con.execute(
            "DELETE FROM ai_insight_runs WHERE insight_id=? AND id NOT IN ("
            "  SELECT id FROM ai_insight_runs WHERE insight_id=? "
            "  ORDER BY id DESC LIMIT ?"
            ")",
            (insight_id, insight_id, RUN_HISTORY_CAP),
        )
        con.commit()
    return new_id


def list_insight_runs(insight_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Return runs for an insight, newest first. `tool_results` returns
    as parsed JSON (or [] on parse error)."""
    ensure_schema()
    if limit <= 0:
        limit = RUN_HISTORY_CAP
    if limit > RUN_HISTORY_CAP:
        limit = RUN_HISTORY_CAP
    out: List[Dict[str, Any]] = []
    with _connect() as con:
        rows = con.execute(
            "SELECT id, insight_id, triggered_by, started_utc, finished_utc, ok, content, error, tool_results"
            " FROM ai_insight_runs WHERE insight_id=? ORDER BY id DESC LIMIT ?",
            (insight_id, limit),
        ).fetchall()
    for r in rows:
        tr_raw = r[8]
        try:
            tr = json.loads(tr_raw) if tr_raw else []
        except Exception:
            tr = []
        out.append({
            "id": r[0],
            "insight_id": r[1],
            "triggered_by": r[2],
            "started_utc": r[3],
            "finished_utc": r[4],
            "ok": bool(r[5]),
            "content": r[6],
            "error": r[7],
            "tool_results": tr,
        })
    return out


def delete_insight_run(run_id: int) -> None:
    ensure_schema()
    with _connect() as con:
        con.execute("DELETE FROM ai_insight_runs WHERE id=?", (int(run_id),))
        con.commit()


def all_enabled_insights() -> List[Dict[str, Any]]:
    """Scheduler reads this every tick. Returns insights with a non-empty
    schedule that are enabled."""
    ensure_schema()
    with _connect() as con:
        rows = con.execute(
            "SELECT id FROM ai_insights WHERE enabled=1 AND schedule_cron != ''"
        ).fetchall()
    out = []
    for r in rows:
        item = get_insight(r[0])
        if item:
            out.append(item)
    return out


def delete_insight(insight_id: str) -> None:
    ensure_schema()
    with _connect() as con:
        con.execute("DELETE FROM ai_insights WHERE id=?", (insight_id,))
        con.commit()


# --- presets (customer-editable starter-query palette) --------------------

def get_presets(tenant_id: str) -> Optional[Dict[str, Any]]:
    """Return the customer's SAVED palette JSON for this tenant, or None if
    they haven't customized it yet (caller then serves the built-in defaults).
    """
    ensure_schema()
    with _connect() as con:
        row = con.execute(
            "SELECT palette_json FROM ai_presets WHERE tenant_id=?",
            (tenant_id or "default",),
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def save_presets(tenant_id: str, palette: Dict[str, Any]) -> None:
    """Persist the customer's edited palette for this tenant (upsert)."""
    ensure_schema()
    payload = json.dumps(palette, default=str)
    with _connect() as con:
        con.execute(
            "INSERT INTO ai_presets (tenant_id, palette_json, updated_utc) VALUES (?,?,?) "
            "ON CONFLICT(tenant_id) DO UPDATE SET palette_json=excluded.palette_json, "
            "updated_utc=excluded.updated_utc",
            (tenant_id or "default", payload, _now_utc()),
        )
        con.commit()


def reset_presets(tenant_id: str) -> None:
    """Delete the customer's custom palette so defaults are served again."""
    ensure_schema()
    with _connect() as con:
        con.execute("DELETE FROM ai_presets WHERE tenant_id=?", (tenant_id or "default",))
        con.commit()
