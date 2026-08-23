"""TrustNode Edge — historian retention, tiered rollups, and backups.

Operator 2026-08-21. Design: docs/historian-retention-and-forwarding-architecture-2026-08-21.md

WHY THIS EXISTS
---------------
The legacy retention job (`AppStore.run_retention`) was measured broken on a
live edge: in 706 scheduled runs it produced ZERO rollup rows while still
deleting raw readings (its rollup window `[minute_cutoff, raw_cutoff)` is empty
whenever minute_keep == raw_keep), its 50k-rows-per-run delete cap could not
keep up with 48 rows/s of ingest, and the whole job ran under the GLOBAL
`AppStore._lock` starting 30 s after boot — stalling `/api/health` and every
config read while it scanned a 7.9M-row table.

This engine replaces it with the model every industrial historian converged on
(PI / Ignition / InfluxDB / TimescaleDB): keep raw for a short window, and roll
it up into progressively coarser tiers that are kept for progressively longer,
composing each tier from the next finer one using statistics that compose
EXACTLY (n / sum / sumsq / min / max / first / last).

HARD INVARIANTS (enforced in code, see _prune_floor_ms)
-------------------------------------------------------
I1  Never delete data that has not been (a) rolled up into every tier that
    depends on it, (b) forwarded to every configured target, and (c) archived
    when the policy asks for it. Deletion floors are the MINIMUM of all of
    those watermarks and the policy cutoff.
I2  Collection is never blocked. This engine NEVER takes `AppStore._lock`; it
    owns a private SQLite connection, works in small paced transactions, and
    backs off whenever the historian writer reports slow flushes.
I3  Idempotent + resumable. Rollup rows are upserted on a natural key and the
    watermark advances in the SAME transaction, so a crash mid-run simply
    redoes the last chunk.
I4  Boot-safe. Nothing runs until the process has been up for
    RETENTION_BOOT_DELAY_S (default 300 s) AND /api/health has served.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import logging

log = logging.getLogger("trustnode.retention")

# --------------------------------------------------------------------------
# Tunables (env-overridable so a site can slow the engine down without a build)
# --------------------------------------------------------------------------

def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, "").strip() or default)))
    except Exception:
        return default


RETENTION_BOOT_DELAY_S = _env_int("TRUSTNODE_RETENTION_BOOT_DELAY_S", 300, 5, 3600)
RETENTION_TICK_S = _env_int("TRUSTNODE_RETENTION_TICK_S", 60, 10, 3600)
# Rollup reads one source chunk at a time so a single statement stays short.
ROLLUP_CHUNK_S = _env_int("TRUSTNODE_RETENTION_ROLLUP_CHUNK_S", 3600, 60, 86400)
# Adaptive delete batch bounds. Start small; grow while batches stay fast.
DELETE_BATCH_MIN = _env_int("TRUSTNODE_RETENTION_DELETE_MIN", 1000, 100, 100000)
DELETE_BATCH_START = _env_int("TRUSTNODE_RETENTION_DELETE_START", 5000, 100, 200000)
DELETE_BATCH_MAX = _env_int("TRUSTNODE_RETENTION_DELETE_MAX", 50000, 1000, 500000)
BATCH_FAST_MS = _env_int("TRUSTNODE_RETENTION_BATCH_FAST_MS", 60, 5, 5000)
BATCH_SLOW_MS = _env_int("TRUSTNODE_RETENTION_BATCH_SLOW_MS", 150, 10, 10000)
# Late-arriving data grace: a bucket is only closed this long after it ends.
ROLLUP_GRACE_S = _env_int("TRUSTNODE_RETENTION_GRACE_S", 120, 5, 86400)
# Disk guard thresholds.
DISK_WARN_PCT = _env_int("TRUSTNODE_RETENTION_DISK_WARN_PCT", 15, 1, 90)
DISK_EMERGENCY_PCT = _env_int("TRUSTNODE_RETENTION_DISK_EMERGENCY_PCT", 5, 1, 50)
DISK_EMERGENCY_GB = _env_int("TRUSTNODE_RETENTION_DISK_EMERGENCY_GB", 5, 1, 500)

MAX_TIERS = 6
MAX_KEEP_S = 5 * 365 * 24 * 3600          # operator ceiling: 5 years
MIN_RAW_KEEP_S = 3600                      # never keep less than one hour of raw

# --------------------------------------------------------------------------
# Duration / resolution vocabulary
# --------------------------------------------------------------------------

_DUR_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|m|min|mins|h|hr|hrs|d|day|days|w|wk|weeks?|mo|mon|months?|y|yr|years?)\s*$", re.I)
_DUR_UNITS = {
    "s": 1, "sec": 1, "secs": 1,
    "m": 60, "min": 60, "mins": 60,
    "h": 3600, "hr": 3600, "hrs": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "wk": 604800, "week": 604800, "weeks": 604800,
    "mo": 2592000, "mon": 2592000, "month": 2592000, "months": 2592000,   # 30 d
    "y": 31536000, "yr": 31536000, "year": 31536000, "years": 31536000,   # 365 d
}

FOREVER = -1


def parse_duration(text: Any, default: Optional[int] = None) -> Optional[int]:
    """'7d' -> 604800. 'forever'/'' -> FOREVER. Returns None when unparseable."""
    if text is None:
        return default
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return int(text)
    s = str(text).strip().lower()
    if s in ("forever", "always", "never", "keep", "unlimited", "inf"):
        return FOREVER
    if s == "":
        return default
    m = _DUR_RE.match(s)
    if not m:
        return default
    return int(float(m.group(1)) * _DUR_UNITS[m.group(2).lower()])


def format_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return ""
    if seconds == FOREVER:
        return "forever"
    for unit, size in (("y", 31536000), ("mo", 2592000), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


# Resolutions the policy editor offers. Each must divide the next one evenly for
# hierarchical composition to be EXACT (validated in validate_policy).
RESOLUTION_CHOICES: List[Tuple[str, int]] = [
    ("5s", 5), ("10s", 10), ("15s", 15), ("30s", 30),
    ("1m", 60), ("5m", 300), ("10m", 600), ("15m", 900), ("30m", 1800),
    ("1h", 3600), ("2h", 7200), ("4h", 14400), ("6h", 21600), ("12h", 43200),
    ("1d", 86400),
]
_RES_BY_LABEL = {label: secs for label, secs in RESOLUTION_CHOICES}
AGGREGATES = ("avg", "min", "max", "last", "first", "sum")


def parse_resolution(text: Any) -> Optional[int]:
    if text is None:
        return None
    s = str(text).strip().lower()
    if s in _RES_BY_LABEL:
        return _RES_BY_LABEL[s]
    return parse_duration(s, None)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ms_to_sql(ms: int) -> str:
    """historian_readings.ts_utc is TEXT 'YYYY-MM-DD HH:MM:SS.fff' (UTC, no tz)."""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


# --------------------------------------------------------------------------
# Policy model
# --------------------------------------------------------------------------

class PolicyError(ValueError):
    """Raised with an operator-readable message; surfaced as HTTP 422."""


DEFAULT_OTHER_DATA = {
    "app_logs_keep": "90d",
    "audit_keep": "5y",
    "telemetry_cycles_keep": "7d",
    "outbox_sent_keep": "1d",
    "reports_files_keep": "2y",
    "reports_metadata_keep": "forever",
    "retention_runs_keep": "1y",
}

DEFAULT_MAINTENANCE = {
    "window_local": "",              # "" = any time
    "catch_up_outside_window": True,
    "max_run_minutes": 30,
    "pace_ms_per_batch": 20,
    "archive_before_prune": False,
    "archive_location": "",
}

DEFAULT_BACKUPS = {
    "enabled": True,
    "config_daily_keep": 14,         # small (MB) — safe to keep many
    "historian_weekly_keep": 0,      # 0 = off (a full historian copy is GBs)
    "location": "",                  # "" = <data_dir>/backups
}


def _norm_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except Exception:
        return default


def validate_policy(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise + validate a policy document. Raises PolicyError with a message
    the UI shows verbatim. Returns the canonical stored form."""
    if not isinstance(raw, dict):
        raise PolicyError("Policy must be an object.")

    name = str(raw.get("name") or "").strip() or "Retention policy"
    pid = str(raw.get("id") or "").strip() or f"pol-{uuid.uuid4().hex[:10]}"

    raw_block = raw.get("raw") if isinstance(raw.get("raw"), dict) else {}
    raw_keep = parse_duration(raw_block.get("keep"), None)
    if raw_keep is None:
        raise PolicyError("Raw data: enter how long to keep full-resolution data (e.g. 7d).")
    if raw_keep == FOREVER:
        raise PolicyError(
            "Raw data cannot be kept forever — that is the 'no policy' setting. "
            "Deactivate the policy instead, or choose a raw window."
        )
    if raw_keep < MIN_RAW_KEEP_S:
        raise PolicyError("Raw data must be kept for at least 1 hour.")
    if raw_keep > MAX_KEEP_S:
        raise PolicyError("Raw data cannot be kept longer than 5 years.")

    tiers_in = raw.get("tiers") if isinstance(raw.get("tiers"), list) else []
    if len(tiers_in) > MAX_TIERS:
        raise PolicyError(f"At most {MAX_TIERS} aggregate levels are supported.")

    tiers: List[Dict[str, Any]] = []
    prev_keep = raw_keep
    prev_res = 0
    for idx, t in enumerate(tiers_in):
        if not isinstance(t, dict):
            raise PolicyError(f"Level {idx + 1} is malformed.")
        keep = parse_duration(t.get("keep"), None)
        res = parse_resolution(t.get("resolution"))
        agg = str(t.get("aggregate") or "avg").strip().lower()
        label = f"Level {idx + 1}"
        if res is None or res <= 0:
            raise PolicyError(f"{label}: choose a resolution (e.g. 1m).")
        if res < 5:
            raise PolicyError(f"{label}: the finest supported resolution is 5 seconds.")
        if keep is None:
            raise PolicyError(f"{label}: enter how long to keep this level (e.g. 30d).")
        if keep == FOREVER:
            keep = MAX_KEEP_S
        if keep > MAX_KEEP_S:
            raise PolicyError(f"{label}: the maximum retention is 5 years.")
        if keep <= prev_keep:
            prev_label = "raw data" if idx == 0 else f"level {idx}"
            raise PolicyError(
                f"{label}: must be kept LONGER than {prev_label} "
                f"({format_duration(keep)} is not longer than {format_duration(prev_keep)})."
            )
        if res <= prev_res:
            raise PolicyError(
                f"{label}: resolution must be COARSER than the level above "
                f"({format_duration(res)} is not coarser than {format_duration(prev_res)})."
            )
        if prev_res and res % prev_res != 0:
            raise PolicyError(
                f"{label}: {format_duration(res)} must be a whole multiple of "
                f"{format_duration(prev_res)} so averages stay exact. "
                f"Try {format_duration(prev_res * max(2, round(res / prev_res)))}."
            )
        if agg not in AGGREGATES:
            raise PolicyError(f"{label}: unknown aggregate '{agg}'.")
        # A tier must be coarser than the raw window is long, or it never fills.
        tiers.append({"keep": format_duration(keep), "keep_s": keep,
                      "resolution": format_duration(res), "resolution_s": res,
                      "aggregate": agg})
        prev_keep, prev_res = keep, res

    text_block = raw.get("text_tags") if isinstance(raw.get("text_tags"), dict) else {}
    text_keep = parse_duration(text_block.get("keep"), tiers[-1]["keep_s"] if tiers else raw_keep)
    if text_keep is not None and text_keep != FOREVER:
        text_keep = min(text_keep, MAX_KEEP_S)

    maint_in = raw.get("maintenance") if isinstance(raw.get("maintenance"), dict) else {}
    maintenance = dict(DEFAULT_MAINTENANCE)
    window = str(maint_in.get("window_local") or "").strip()
    if window and not re.match(r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$", window):
        raise PolicyError("Maintenance window must look like 01:00-05:00 (or be empty for any time).")
    maintenance.update({
        "window_local": window,
        "catch_up_outside_window": bool(maint_in.get("catch_up_outside_window", True)),
        "max_run_minutes": _norm_int(maint_in.get("max_run_minutes"), 30, 1, 720),
        "pace_ms_per_batch": _norm_int(maint_in.get("pace_ms_per_batch"), 20, 0, 5000),
        "archive_before_prune": bool(maint_in.get("archive_before_prune", False)),
        "archive_location": str(maint_in.get("archive_location") or "").strip(),
    })
    if maintenance["archive_before_prune"] and not maintenance["archive_location"]:
        raise PolicyError("Archive before delete is on — choose a folder to write archives to.")

    other_in = raw.get("other_data") if isinstance(raw.get("other_data"), dict) else {}
    other: Dict[str, Any] = {}
    for key, dflt in DEFAULT_OTHER_DATA.items():
        val = other_in.get(key, dflt)
        secs = parse_duration(val, parse_duration(dflt))
        if secs is None:
            raise PolicyError(f"Other data: '{key}' is not a valid duration.")
        other[key] = format_duration(secs)

    bk_in = raw.get("backups") if isinstance(raw.get("backups"), dict) else {}
    backups = dict(DEFAULT_BACKUPS)
    backups.update({
        "enabled": bool(bk_in.get("enabled", True)),
        "config_daily_keep": _norm_int(bk_in.get("config_daily_keep"), 14, 0, 365),
        "historian_weekly_keep": _norm_int(bk_in.get("historian_weekly_keep"), 0, 0, 52),
        "location": str(bk_in.get("location") or "").strip(),
    })

    return {
        "id": pid,
        "name": name,
        "raw": {"keep": format_duration(raw_keep), "keep_s": raw_keep},
        "tiers": tiers,
        "text_tags": {"keep": format_duration(text_keep), "keep_s": text_keep},
        "maintenance": maintenance,
        "other_data": other,
        "backups": backups,
    }


BUILTIN_PRESETS: List[Dict[str, Any]] = [
    {
        "id": "preset-balanced",
        "name": "Balanced — 5 years of history",
        "description": "Full detail for 2 days, then averages. The usual choice for a 1-second line.",
        "raw": {"keep": "2d"},
        "tiers": [
            {"keep": "30d", "resolution": "1m", "aggregate": "avg"},
            {"keep": "1y", "resolution": "15m", "aggregate": "avg"},
            {"keep": "5y", "resolution": "1h", "aggregate": "avg"},
        ],
    },
    {
        "id": "preset-detail",
        "name": "Detail first — keep raw for a week",
        "description": "A week of full detail for troubleshooting, then a year of minute data.",
        "raw": {"keep": "7d"},
        "tiers": [
            {"keep": "90d", "resolution": "1m", "aggregate": "avg"},
            {"keep": "2y", "resolution": "30m", "aggregate": "avg"},
        ],
    },
    {
        "id": "preset-lean",
        "name": "Lean — smallest footprint",
        "description": "For small disks: one day raw, then coarse averages kept for a year.",
        "raw": {"keep": "1d"},
        "tiers": [
            {"keep": "14d", "resolution": "5m", "aggregate": "avg"},
            {"keep": "1y", "resolution": "1h", "aggregate": "avg"},
        ],
    },
]


# --------------------------------------------------------------------------
# Storage (schema + policy CRUD + watermarks). Uses a PRIVATE connection —
# never AppStore._lock, never AppStore._connect.
# --------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS historian_rollup (
  resolution_s INTEGER NOT NULL,
  tenant_id    TEXT NOT NULL,
  gateway_id   TEXT NOT NULL,
  tag_name     TEXT NOT NULL,
  bucket_ms    INTEGER NOT NULL,
  n            INTEGER NOT NULL DEFAULT 0,
  sum_v        REAL, sumsq_v REAL, min_v REAL, max_v REAL,
  first_v      REAL, first_ms INTEGER, last_v REAL, last_ms INTEGER,
  last_text    TEXT,
  q_min        INTEGER, q_max INTEGER, q_bad_n INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (resolution_s, tenant_id, gateway_id, tag_name, bucket_ms)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_rollup_res_tenant_bucket
  ON historian_rollup(resolution_s, tenant_id, bucket_ms);

CREATE TABLE IF NOT EXISTS historian_text_events (
  tenant_id  TEXT NOT NULL,
  gateway_id TEXT NOT NULL,
  tag_name   TEXT NOT NULL,
  ts_ms      INTEGER NOT NULL,
  value_text TEXT,
  PRIMARY KEY (tenant_id, gateway_id, tag_name, ts_ms)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS historian_retention_state (
  target_id          TEXT NOT NULL,
  tier_key           TEXT NOT NULL,
  tenant_id          TEXT NOT NULL DEFAULT '*',
  materialized_to_ms INTEGER,
  forwarded_to_ms    INTEGER,
  pruned_to_ms       INTEGER,
  archived_to_ms     INTEGER,
  last_run_utc       TEXT,
  last_status        TEXT,
  last_error         TEXT,
  PRIMARY KEY (target_id, tier_key, tenant_id)
);

CREATE TABLE IF NOT EXISTS retention_policy_v2 (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  policy_json TEXT NOT NULL,
  version     INTEGER NOT NULL DEFAULT 1,
  is_active   INTEGER NOT NULL DEFAULT 0,
  created_utc TEXT NOT NULL,
  updated_utc TEXT NOT NULL,
  updated_by  TEXT NOT NULL DEFAULT ''
);
"""


class RetentionStore:
    """All persistence for the engine. One private connection per call keeps
    lock scope tiny and avoids sharing a handle across threads."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    # -- connection -------------------------------------------------------
    def connect(self, *, readonly: bool = False, timeout: float = 15.0) -> sqlite3.Connection:
        if readonly:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=timeout)
        else:
            conn = sqlite3.connect(self.db_path, timeout=timeout)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=15000")
            if not readonly:
                # NORMAL keeps fsync cost low; the historian writer already runs
                # the DB in WAL so this only applies to our own transactions.
                conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        return conn

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            conn = self.connect()
            try:
                conn.executescript(SCHEMA_SQL)
                conn.commit()
                self._schema_ready = True
            finally:
                conn.close()

    # -- policies ---------------------------------------------------------
    def list_policies(self) -> List[Dict[str, Any]]:
        self.ensure_schema()
        conn = self.connect(readonly=True)
        try:
            rows = conn.execute(
                "SELECT id, name, policy_json, version, is_active, created_utc, updated_utc, updated_by "
                "FROM retention_policy_v2 ORDER BY name ASC"
            ).fetchall()
        finally:
            conn.close()
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                doc = json.loads(r["policy_json"])
            except Exception:
                continue
            doc.update({
                "id": r["id"], "name": r["name"], "version": int(r["version"]),
                "is_active": bool(r["is_active"]), "created_utc": r["created_utc"],
                "updated_utc": r["updated_utc"], "updated_by": r["updated_by"],
            })
            out.append(doc)
        return out

    def get_active_policy(self) -> Optional[Dict[str, Any]]:
        for p in self.list_policies():
            if p.get("is_active"):
                return p
        return None

    def save_policy(self, doc: Dict[str, Any], actor: str) -> Dict[str, Any]:
        """Insert or update. Validation happens in the caller (validate_policy)."""
        self.ensure_schema()
        now = _utc_now_text()
        conn = self.connect()
        try:
            existing = conn.execute(
                "SELECT version, created_utc, is_active FROM retention_policy_v2 WHERE id=?",
                (doc["id"],),
            ).fetchone()
            version = (int(existing["version"]) + 1) if existing else 1
            created = existing["created_utc"] if existing else now
            is_active = int(existing["is_active"]) if existing else 0
            conn.execute(
                "INSERT INTO retention_policy_v2 (id, name, policy_json, version, is_active, created_utc, updated_utc, updated_by) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, policy_json=excluded.policy_json, "
                "version=excluded.version, updated_utc=excluded.updated_utc, updated_by=excluded.updated_by",
                (doc["id"], doc["name"], json.dumps(doc), version, is_active, created, now, actor),
            )
            conn.commit()
        finally:
            conn.close()
        out = dict(doc)
        out.update({"version": version, "is_active": bool(is_active),
                    "created_utc": created, "updated_utc": now, "updated_by": actor})
        return out

    def activate_policy(self, policy_id: str, actor: str) -> Optional[Dict[str, Any]]:
        """Empty policy_id deactivates everything (the 'no policy' state)."""
        self.ensure_schema()
        now = _utc_now_text()
        conn = self.connect()
        try:
            conn.execute("UPDATE retention_policy_v2 SET is_active=0")
            if policy_id:
                cur = conn.execute(
                    "UPDATE retention_policy_v2 SET is_active=1, updated_utc=?, updated_by=? WHERE id=?",
                    (now, actor, policy_id),
                )
                if not cur.rowcount:
                    conn.rollback()
                    return None
            conn.commit()
        finally:
            conn.close()
        return self.get_active_policy()

    def delete_policy(self, policy_id: str) -> bool:
        self.ensure_schema()
        conn = self.connect()
        try:
            cur = conn.execute("DELETE FROM retention_policy_v2 WHERE id=?", (policy_id,))
            conn.commit()
            return bool(cur.rowcount)
        finally:
            conn.close()

    # -- watermarks -------------------------------------------------------
    def get_state(self, target_id: str, tier_key: str, tenant_id: str = "*") -> Dict[str, Any]:
        self.ensure_schema()
        conn = self.connect(readonly=True)
        try:
            row = conn.execute(
                "SELECT * FROM historian_retention_state WHERE target_id=? AND tier_key=? AND tenant_id=?",
                (target_id, tier_key, tenant_id),
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else {
            "target_id": target_id, "tier_key": tier_key, "tenant_id": tenant_id,
            "materialized_to_ms": None, "forwarded_to_ms": None,
            "pruned_to_ms": None, "archived_to_ms": None,
            "last_run_utc": None, "last_status": None, "last_error": None,
        }

    def all_states(self) -> List[Dict[str, Any]]:
        self.ensure_schema()
        conn = self.connect(readonly=True)
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM historian_retention_state ORDER BY target_id, tier_key, tenant_id"
            ).fetchall()]
        finally:
            conn.close()

    @staticmethod
    def _set_state_in(conn: sqlite3.Connection, target_id: str, tier_key: str,
                      tenant_id: str, **fields: Any) -> None:
        """Upsert a watermark INSIDE the caller's transaction — that is what makes
        rollup+watermark atomic (invariant I3)."""
        conn.execute(
            "INSERT INTO historian_retention_state (target_id, tier_key, tenant_id) VALUES (?,?,?) "
            "ON CONFLICT(target_id, tier_key, tenant_id) DO NOTHING",
            (target_id, tier_key, tenant_id),
        )
        cols = [k for k in fields if k in (
            "materialized_to_ms", "forwarded_to_ms", "pruned_to_ms", "archived_to_ms",
            "last_run_utc", "last_status", "last_error")]
        if not cols:
            return
        sets = ", ".join(f"{c}=?" for c in cols)
        conn.execute(
            f"UPDATE historian_retention_state SET {sets} WHERE target_id=? AND tier_key=? AND tenant_id=?",
            [fields[c] for c in cols] + [target_id, tier_key, tenant_id],
        )

    def set_state(self, target_id: str, tier_key: str, tenant_id: str = "*", **fields: Any) -> None:
        conn = self.connect()
        try:
            self._set_state_in(conn, target_id, tier_key, tenant_id, **fields)
            conn.commit()
        finally:
            conn.close()

    # -- job history (reuses retention_runs so the existing UI table works) --
    def log_run(self, status: str, details: Dict[str, Any], dry_run: bool = False) -> None:
        conn = self.connect()
        try:
            conn.execute(
                "INSERT INTO retention_runs (run_utc, dry_run, status, details_json) VALUES (?,?,?,?)",
                (_utc_now_text(), 1 if dry_run else 0, status, json.dumps(details, default=str)),
            )
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()

    # -- helpers ----------------------------------------------------------
    def tenants(self) -> List[str]:
        """Loose index scan — every historian index leads with tenant_id, so this
        is a handful of seeks instead of a 7.9M-row DISTINCT scan."""
        conn = self.connect(readonly=True)
        try:
            rows = conn.execute(
                "WITH RECURSIVE t(x) AS ("
                "  SELECT MIN(tenant_id) FROM historian_readings"
                "  UNION ALL"
                "  SELECT (SELECT MIN(tenant_id) FROM historian_readings WHERE tenant_id > t.x)"
                "  FROM t WHERE t.x IS NOT NULL"
                ") SELECT x FROM t WHERE x IS NOT NULL"
            ).fetchall()
            return [str(r[0]) for r in rows if r[0] is not None]
        except Exception:
            return []
        finally:
            conn.close()

    def oldest_raw_ms(self, tenant_id: str) -> Optional[int]:
        conn = self.connect(readonly=True)
        try:
            row = conn.execute(
                "SELECT ts_utc FROM historian_readings WHERE tenant_id=? ORDER BY ts_utc ASC LIMIT 1",
                (tenant_id,),
            ).fetchone()
            if not row or not row[0]:
                return None
            return _sql_ts_to_ms(str(row[0]))
        except Exception:
            return None
        finally:
            conn.close()

    def newest_raw_ms(self, tenant_id: str) -> Optional[int]:
        conn = self.connect(readonly=True)
        try:
            row = conn.execute(
                "SELECT ts_utc FROM historian_readings WHERE tenant_id=? ORDER BY ts_utc DESC LIMIT 1",
                (tenant_id,),
            ).fetchone()
            if not row or not row[0]:
                return None
            return _sql_ts_to_ms(str(row[0]))
        except Exception:
            return None
        finally:
            conn.close()


def _sql_ts_to_ms(text: str) -> Optional[int]:
    s = (text or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(s[:26], fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------
# Rollup SQL. Validated against a Python reference in scripts/test_retention_engine.py
# --------------------------------------------------------------------------

# first/last without window functions: MIN/MAX over a zero-padded-timestamp
# prefix picks the earliest/latest row deterministically, and substr() peels the
# value back off. 20 digits + 1 separator => value starts at offset 22.
_TS_S = "CAST(strftime('%s', ts_utc) AS INTEGER)"
_PAD = "printf('%020d', {ts})||char(31)||"

_ROLLUP_FROM_RAW = f"""
INSERT INTO historian_rollup
  (resolution_s, tenant_id, gateway_id, tag_name, bucket_ms,
   n, sum_v, sumsq_v, min_v, max_v, first_v, first_ms, last_v, last_ms, last_text,
   q_min, q_max, q_bad_n)
SELECT
  :res, tenant_id, COALESCE(gateway_id,''), tag_name,
  ({_TS_S} / :res) * :res * 1000,
  COUNT(value), SUM(value), SUM(value*value), MIN(value), MAX(value),
  CAST(NULLIF(substr(MIN({_PAD.format(ts=_TS_S)}IFNULL(CAST(value AS TEXT),'')), 22), '') AS REAL),
  MIN({_TS_S}) * 1000,
  CAST(NULLIF(substr(MAX({_PAD.format(ts=_TS_S)}IFNULL(CAST(value AS TEXT),'')), 22), '') AS REAL),
  MAX({_TS_S}) * 1000,
  NULLIF(substr(MAX({_PAD.format(ts=_TS_S)}IFNULL(value_text,'')), 22), ''),
  MIN(quality), MAX(quality), SUM(CASE WHEN quality < 192 THEN 1 ELSE 0 END)
FROM historian_readings
WHERE tenant_id = :tenant AND ts_utc >= :from_ts AND ts_utc < :to_ts
GROUP BY tenant_id, COALESCE(gateway_id,''), tag_name, ({_TS_S} / :res) * :res
ON CONFLICT(resolution_s, tenant_id, gateway_id, tag_name, bucket_ms) DO UPDATE SET
  n = excluded.n, sum_v = excluded.sum_v, sumsq_v = excluded.sumsq_v,
  min_v = excluded.min_v, max_v = excluded.max_v,
  first_v = excluded.first_v, first_ms = excluded.first_ms,
  last_v = excluded.last_v, last_ms = excluded.last_ms, last_text = excluded.last_text,
  q_min = excluded.q_min, q_max = excluded.q_max, q_bad_n = excluded.q_bad_n
"""

# Composition tier(k) <- tier(k-1). Exact because we carry n/sum/sumsq/min/max
# and pick first/last by their recorded timestamps.
_PADR = "printf('%020d', {ts})||char(31)||"
_ROLLUP_FROM_TIER = f"""
INSERT INTO historian_rollup
  (resolution_s, tenant_id, gateway_id, tag_name, bucket_ms,
   n, sum_v, sumsq_v, min_v, max_v, first_v, first_ms, last_v, last_ms, last_text,
   q_min, q_max, q_bad_n)
SELECT
  :res, tenant_id, gateway_id, tag_name,
  (bucket_ms / (:res * 1000)) * (:res * 1000),
  SUM(n), SUM(sum_v), SUM(sumsq_v), MIN(min_v), MAX(max_v),
  CAST(NULLIF(substr(MIN({_PADR.format(ts='first_ms')}IFNULL(CAST(first_v AS TEXT),'')), 22), '') AS REAL),
  MIN(first_ms),
  CAST(NULLIF(substr(MAX({_PADR.format(ts='last_ms')}IFNULL(CAST(last_v AS TEXT),'')), 22), '') AS REAL),
  MAX(last_ms),
  NULLIF(substr(MAX({_PADR.format(ts='last_ms')}IFNULL(last_text,'')), 22), ''),
  MIN(q_min), MAX(q_max), SUM(q_bad_n)
FROM historian_rollup
WHERE resolution_s = :src_res AND tenant_id = :tenant
  AND bucket_ms >= :from_ms AND bucket_ms < :to_ms
GROUP BY tenant_id, gateway_id, tag_name, (bucket_ms / (:res * 1000)) * (:res * 1000)
ON CONFLICT(resolution_s, tenant_id, gateway_id, tag_name, bucket_ms) DO UPDATE SET
  n = excluded.n, sum_v = excluded.sum_v, sumsq_v = excluded.sumsq_v,
  min_v = excluded.min_v, max_v = excluded.max_v,
  first_v = excluded.first_v, first_ms = excluded.first_ms,
  last_v = excluded.last_v, last_ms = excluded.last_ms, last_text = excluded.last_text,
  q_min = excluded.q_min, q_max = excluded.q_max, q_bad_n = excluded.q_bad_n
"""

_TEXT_EVENTS_SQL = f"""
INSERT INTO historian_text_events (tenant_id, gateway_id, tag_name, ts_ms, value_text)
SELECT tenant_id, COALESCE(gateway_id,''), tag_name, {_TS_S} * 1000, value_text
FROM historian_readings
WHERE tenant_id = :tenant AND ts_utc >= :from_ts AND ts_utc < :to_ts
  AND value_text IS NOT NULL AND value_text <> ''
GROUP BY tenant_id, COALESCE(gateway_id,''), tag_name, value_text, ({_TS_S} / 60) * 60
ON CONFLICT(tenant_id, gateway_id, tag_name, ts_ms) DO NOTHING
"""


# --------------------------------------------------------------------------
# Backups — online (sqlite3 backup API), never a raw file copy of a live DB
# --------------------------------------------------------------------------

# Tables excluded from a CONFIG backup: the time-series bulk. Everything else
# (settings, users, gateways, dashboards, batches, report metadata, licences,
# retention policies) is what an operator needs to rebuild the system.
_BULK_TABLES = {
    "historian_readings", "historian_rollup", "historian_text_events",
    "historian_agg_minute", "historian_agg_hour", "historian_agg_day",
    "app_logs",
}

BACKUP_KIND_CONFIG = "config"
BACKUP_KIND_FULL = "full"
BACKUP_KIND_SAFETY = "safety"


class BackupManager:
    """Consistent, online backups.

    The legacy implementation was `shutil.copy2()` of the live multi-GB DB while
    the PLC writer held a transaction — it copied only the `.db` (never the
    `-wal`), so a "backup" could be torn. Both paths here use SQLite's own
    backup API / a transactional table copy, which are safe on a live database.
    """

    def __init__(self, store: "RetentionStore", backup_dir_fn: Callable[[], str]) -> None:
        self.store = store
        self._backup_dir_fn = backup_dir_fn

    # -- paths ------------------------------------------------------------
    def backup_dir(self, location: str = "") -> str:
        target = (location or "").strip() or self._backup_dir_fn()
        os.makedirs(target, exist_ok=True)
        return target

    @staticmethod
    def _stamp() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # -- create -----------------------------------------------------------
    def create_config_backup(self, location: str = "", label: str = "") -> Dict[str, Any]:
        """Small, fast, always safe: schema + every non-bulk table."""
        t0 = time.time()
        out_dir = self.backup_dir(location)
        safe = "".join(c for c in str(label or "") if c.isalnum() or c in "_-")[:32]
        name = f"trustnode_config_{self._stamp()}{('_' + safe) if safe else ''}.db"
        final = os.path.join(out_dir, name)
        tmp = final + ".part"
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        dst = sqlite3.connect(tmp)
        try:
            src = self.store.connect(readonly=True)
            try:
                objects = src.execute(
                    "SELECT name, type, sql FROM sqlite_master "
                    "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                tables = [r["name"] for r in objects
                          if r["type"] == "table" and r["name"] not in _BULK_TABLES]
                # 1. schema for the tables we keep
                for r in objects:
                    if r["type"] == "table" and r["name"] in tables:
                        dst.execute(r["sql"])
            finally:
                src.close()
            # 2. rows, in one transaction, reading the live DB read-only
            dst.execute("PRAGMA journal_mode=WAL")
            dst.execute("ATTACH DATABASE ? AS src", (self.store.db_path,))
            dst.execute("BEGIN")
            copied = 0
            for t in tables:
                try:
                    cur = dst.execute(f'INSERT INTO main."{t}" SELECT * FROM src."{t}"')
                    copied += max(0, int(cur.rowcount or 0))
                except Exception as exc:
                    log.warning("config backup: table %s skipped (%s)", t, exc)
            dst.execute("COMMIT")
            # 3. indexes/views/triggers last (faster, and never blocks the copy)
            src = self.store.connect(readonly=True)
            try:
                for r in src.execute(
                    "SELECT name, type, sql, tbl_name FROM sqlite_master "
                    "WHERE type IN ('index','view','trigger') AND sql IS NOT NULL "
                    "AND name NOT LIKE 'sqlite_%'"
                ).fetchall():
                    if r["tbl_name"] in _BULK_TABLES:
                        continue
                    try:
                        dst.execute(r["sql"])
                    except Exception:
                        pass
            finally:
                src.close()
            dst.execute("DETACH DATABASE src")
            dst.commit()
        finally:
            try:
                dst.close()
            except Exception:
                pass
        os.replace(tmp, final)
        size = os.path.getsize(final)
        took = time.time() - t0
        log.info("config backup written %s (%.1f MB, %d rows, %.1fs)", name, size / 1e6, copied, took)
        return {"ok": True, "kind": BACKUP_KIND_CONFIG, "filename": name, "path": final,
                "size_bytes": size, "rows": copied, "took_s": round(took, 2),
                "created_utc": _utc_now_text()}

    def create_full_backup(self, location: str = "", label: str = "",
                           should_stop: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
        """Whole database, including history, via the online backup API."""
        t0 = time.time()
        out_dir = self.backup_dir(location)
        safe = "".join(c for c in str(label or "") if c.isalnum() or c in "_-")[:32]
        name = f"trustnode_app_store_{self._stamp()}{('_' + safe) if safe else ''}.db"
        final = os.path.join(out_dir, name)
        tmp = final + ".part"
        src = self.store.connect(readonly=True, timeout=30.0)
        dst = sqlite3.connect(tmp)
        cancelled = False
        try:
            def _progress(status: int, remaining: int, total: int) -> None:
                nonlocal cancelled
                if should_stop and should_stop():
                    cancelled = True
                    raise sqlite3.OperationalError("backup cancelled")
            # pages/sleep keep the copy from monopolising the file: it yields
            # between chunks so the PLC writer keeps its cadence.
            src.backup(dst, pages=2048, progress=_progress, sleep=0.02)
            dst.close()
            src.close()
            os.replace(tmp, final)
        except Exception as exc:
            for h in (dst, src):
                try:
                    h.close()
                except Exception:
                    pass
            try:
                os.remove(tmp)
            except Exception:
                pass
            if cancelled:
                return {"ok": False, "message": "Backup cancelled (shutting down)."}
            return {"ok": False, "message": f"Backup failed: {exc}"}
        size = os.path.getsize(final)
        took = time.time() - t0
        log.info("full backup written %s (%.2f GB, %.1fs)", name, size / 1e9, took)
        return {"ok": True, "kind": BACKUP_KIND_FULL, "filename": name, "path": final,
                "size_bytes": size, "took_s": round(took, 2), "created_utc": _utc_now_text()}

    # -- list / rotate / delete -------------------------------------------
    @staticmethod
    def classify(filename: str) -> str:
        low = filename.lower()
        if "before_restore" in low or "_safety" in low:
            return BACKUP_KIND_SAFETY
        if low.startswith("trustnode_config_"):
            return BACKUP_KIND_CONFIG
        return BACKUP_KIND_FULL

    def list_backups(self, location: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        out_dir = self.backup_dir(location)
        rows: List[Dict[str, Any]] = []
        try:
            for name in os.listdir(out_dir):
                if not name.lower().endswith(".db"):
                    continue
                path = os.path.join(out_dir, name)
                if not os.path.isfile(path):
                    continue
                st = os.stat(path)
                rows.append({
                    "filename": name, "path": path, "size_bytes": int(st.st_size),
                    "kind": self.classify(name),
                    "modified_utc": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                                            .strftime("%Y-%m-%d %H:%M:%S"),
                })
        except Exception:
            return []
        rows.sort(key=lambda r: r["modified_utc"], reverse=True)
        return rows[: max(1, min(int(limit or 200), 1000))]

    def rotate(self, location: str, kind: str, keep: int) -> List[str]:
        """Rotate WITHIN a class. The legacy code kept 'the newest 10 .db files'
        of any kind, which happily deleted the pre-restore safety copy."""
        if keep < 0:
            return []
        removed: List[str] = []
        entries = [r for r in self.list_backups(location, limit=1000) if r["kind"] == kind]
        for row in entries[keep:]:
            try:
                os.remove(row["path"])
                removed.append(row["filename"])
            except Exception:
                pass
        if removed:
            log.info("backup rotation (%s): removed %d old file(s)", kind, len(removed))
        return removed

    def delete(self, filename: str, location: str = "") -> Dict[str, Any]:
        clean = os.path.basename(str(filename or "").strip())
        if not clean or clean in (".", ".."):
            return {"ok": False, "message": "Invalid backup filename."}
        path = os.path.join(self.backup_dir(location), clean)
        if not os.path.isfile(path):
            return {"ok": False, "message": f"Backup not found: {clean}"}
        try:
            os.remove(path)
            return {"ok": True, "message": f"Backup deleted: {clean}"}
        except Exception as exc:
            return {"ok": False, "message": f"Delete failed: {exc}"}

    # -- restore (staged; applied by apply_pending_db_swap at next start) ---
    def stage_restore(self, filename: str, location: str = "") -> Dict[str, Any]:
        clean = os.path.basename(str(filename or "").strip())
        if not clean or clean in (".", ".."):
            return {"ok": False, "message": "Invalid backup filename."}
        source = os.path.join(self.backup_dir(location), clean)
        if not os.path.isfile(source):
            return {"ok": False, "message": f"Backup not found: {clean}"}
        # Verify the candidate BEFORE we promise anything.
        try:
            probe = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10.0)
            try:
                result = str((probe.execute("PRAGMA quick_check").fetchone() or ["?"])[0])
                if result.lower() != "ok":
                    return {"ok": False, "message": f"Backup failed integrity check: {result}"}
                names = {r[0] for r in probe.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                if "config_documents" not in names and "config_documents_scoped" not in names:
                    return {"ok": False, "message": "That file is not a TrustNode backup."}
                kind = BACKUP_KIND_CONFIG if "historian_readings" not in names else BACKUP_KIND_FULL
            finally:
                probe.close()
        except Exception as exc:
            return {"ok": False, "message": f"Backup could not be opened: {exc}"}

        pending = self.store.db_path + ".restore_pending"
        try:
            shutil.copy2(source, pending)
            with open(self.store.db_path + ".restore_marker", "w", encoding="utf-8") as fh:
                json.dump({"filename": clean, "kind": kind, "staged_utc": _utc_now_text()}, fh)
        except Exception as exc:
            return {"ok": False, "message": f"Could not stage the restore: {exc}"}
        return {
            "ok": True, "staged": True, "filename": clean, "kind": kind,
            "message": ("Restore staged. It is applied the next time TrustNode starts — "
                        "the current database is kept as a safety copy first."),
        }

    def cancel_restore(self) -> Dict[str, Any]:
        removed = False
        for suffix in (".restore_pending", ".restore_marker"):
            p = self.store.db_path + suffix
            try:
                if os.path.exists(p):
                    os.remove(p)
                    removed = True
            except Exception:
                pass
        return {"ok": True, "cancelled": removed}

    def pending_restore(self) -> Optional[Dict[str, Any]]:
        marker = self.store.db_path + ".restore_marker"
        if not os.path.exists(marker) or not os.path.exists(self.store.db_path + ".restore_pending"):
            return None
        try:
            with open(marker, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {"filename": "(unknown)", "kind": "?"}


def apply_pending_db_swap(db_path: str) -> Optional[str]:
    """Called at BOOT, before anything opens the database.

    A staged restore (or a completed compaction) is swapped in here because it is
    the only moment no connection is open. Returns a human-readable note when
    something was applied, else None. Never raises — a failed swap must not stop
    the app from starting on the existing database.
    """
    note: Optional[str] = None
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        for suffix, kind in ((".restore_pending", "restore"), (".compacted", "compaction")):
            candidate = db_path + suffix
            if not os.path.exists(candidate):
                continue
            # Validate before replacing anything.
            try:
                probe = sqlite3.connect(f"file:{candidate}?mode=ro", uri=True, timeout=10.0)
                try:
                    ok = str((probe.execute("PRAGMA quick_check").fetchone() or ["?"])[0]).lower() == "ok"
                finally:
                    probe.close()
            except Exception:
                ok = False
            if not ok:
                try:
                    os.remove(candidate)
                except Exception:
                    pass
                print(f"[trustnode][retention] pending {kind} discarded — failed integrity check", flush=True)
                continue
            if os.path.exists(db_path):
                safety = f"{db_path}.pre_{kind}_{stamp}"
                try:
                    os.replace(db_path, safety)
                except Exception as exc:
                    print(f"[trustnode][retention] pending {kind} skipped — "
                          f"could not move the live database aside: {exc}", flush=True)
                    continue
            # WAL/SHM of the old database must not survive next to the new file.
            for side in ("-wal", "-shm"):
                try:
                    if os.path.exists(db_path + side):
                        os.remove(db_path + side)
                except Exception:
                    pass
            os.replace(candidate, db_path)
            for extra in (".restore_marker",):
                try:
                    if os.path.exists(db_path + extra):
                        os.remove(db_path + extra)
                except Exception:
                    pass
            note = f"{kind} applied at startup"
            print(f"[trustnode][retention] {note} ({os.path.basename(candidate)})", flush=True)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[trustnode][retention] pending swap check failed: {exc}", flush=True)
    return note


# --------------------------------------------------------------------------
# Size estimation (drives the live figure in the policy editor)
# --------------------------------------------------------------------------

def estimate_policy_size(policy: Dict[str, Any], *, tag_count: int, interval_s: float,
                         bytes_per_raw_row: float, bytes_per_rollup_row: float = 190.0,
                         duty_cycle: float = 1.0) -> Dict[str, Any]:
    """Bytes the policy settles at, using the REAL per-row cost measured on this
    machine — so the number in the editor matches what the disk actually does."""
    tags = max(1, int(tag_count or 1))
    interval = max(0.05, float(interval_s or 1.0))
    duty = max(0.0, min(1.0, float(duty_cycle if duty_cycle is not None else 1.0)))
    rows_per_day_raw = tags * (86400.0 / interval) * duty

    raw_keep_s = int((policy.get("raw") or {}).get("keep_s")
                     or parse_duration((policy.get("raw") or {}).get("keep"), 0) or 0)
    levels: List[Dict[str, Any]] = []
    raw_bytes = (raw_keep_s / 86400.0) * rows_per_day_raw * bytes_per_raw_row
    levels.append({
        "key": "raw", "label": "Full detail (raw)",
        "keep": format_duration(raw_keep_s), "resolution": format_duration(int(interval)) if interval >= 1 else f"{interval}s",
        "rows": int((raw_keep_s / 86400.0) * rows_per_day_raw), "bytes": int(raw_bytes),
    })
    total = raw_bytes
    for idx, tier in enumerate(policy.get("tiers") or []):
        keep_s = int(tier.get("keep_s") or parse_duration(tier.get("keep"), 0) or 0)
        res_s = int(tier.get("resolution_s") or parse_resolution(tier.get("resolution")) or 0)
        if keep_s <= 0 or res_s <= 0:
            continue
        # A tier only stores the span it uniquely covers (older than the level above).
        prev_keep = raw_keep_s if idx == 0 else int(
            (policy["tiers"][idx - 1]).get("keep_s")
            or parse_duration((policy["tiers"][idx - 1]).get("keep"), 0) or 0)
        span_s = max(0, keep_s - prev_keep)
        rows = tags * (span_s / float(res_s)) * duty
        size = rows * bytes_per_rollup_row
        levels.append({
            "key": f"r{res_s}", "label": f"{format_duration(res_s)} {tier.get('aggregate', 'avg')}",
            "keep": format_duration(keep_s), "resolution": format_duration(res_s),
            "rows": int(rows), "bytes": int(size),
        })
        total += size
    no_policy_year = 365.0 * rows_per_day_raw * bytes_per_raw_row
    return {
        "levels": levels,
        "total_bytes": int(total),
        "per_day_raw_bytes": int(rows_per_day_raw * bytes_per_raw_row),
        "no_policy_year_bytes": int(no_policy_year),
        "assumptions": {
            "tag_count": tags, "interval_s": interval, "duty_cycle": duty,
            "bytes_per_raw_row": round(bytes_per_raw_row, 1),
            "bytes_per_rollup_row": round(bytes_per_rollup_row, 1),
        },
    }


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------

class RetentionEngine:
    """Owns the maintenance thread and every job it runs."""

    TARGET_LOCAL = "local"

    def __init__(self, db_path: str, *, backup_dir_fn: Callable[[], str],
                 boot_ready_fn: Optional[Callable[[], bool]] = None,
                 writer_busy_fn: Optional[Callable[[], bool]] = None,
                 cloud_cursor_fn: Optional[Callable[[], Optional[int]]] = None,
                 tag_stats_fn: Optional[Callable[[], Dict[str, Any]]] = None) -> None:
        self.store = RetentionStore(db_path)
        self.backups = BackupManager(self.store, backup_dir_fn)
        self._boot_ready_fn = boot_ready_fn
        self._writer_busy_fn = writer_busy_fn
        self._cloud_cursor_fn = cloud_cursor_fn
        self._tag_stats_fn = tag_stats_fn

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._run_lock = threading.Lock()          # one job run at a time
        self._started_at = time.time()

        self._batch_size = DELETE_BATCH_START
        self._last_tick: Dict[str, Any] = {}
        self._last_backup_config = 0.0
        self._last_backup_full = 0.0
        self._paused_until = 0.0
        self._activity: List[Dict[str, Any]] = []   # rolling in-memory job log
        # Row census for the Storage card: COUNT(*) over the historian costs
        # ~1.3 s at 9 M rows, so it is measured off the request path and served
        # from this snapshot (same pattern as the lock-free /api/health one).
        self._db_stats: Dict[str, Any] = {}
        self._row_costs_cache: Dict[str, Any] = {}
        self._db_stats_lock = threading.Lock()
        self._db_stats_thread: Optional[threading.Thread] = None

    # -- row census (off the request path) --------------------------------
    DB_STATS_TTL_S = 60.0
    ROW_COSTS_TTL_S = 300.0

    def _refresh_db_stats(self) -> None:
        try:
            conn = self.store.connect(readonly=True)
            try:
                rows = int(conn.execute(
                    "SELECT COUNT(*) FROM historian_readings").fetchone()[0] or 0)
            finally:
                conn.close()
            with self._db_stats_lock:
                self._db_stats = {"raw_rows": rows, "measured_mono": time.monotonic(),
                                  "measured_utc": _utc_now_text(), "measuring": False}
        except Exception as exc:
            log.debug("retention row census failed: %s", exc)
        finally:
            with self._db_stats_lock:
                self._db_stats_thread = None

    def _db_stats_cached(self) -> Dict[str, Any]:
        """Last measured row count, refreshing in the background when stale.

        Never blocks the caller: the first call returns `measuring` and the UI
        shows "measuring…" instead of a wrong zero."""
        with self._db_stats_lock:
            snap = dict(self._db_stats)
            fresh = snap and (time.monotonic() - float(snap.get("measured_mono") or 0)) < self.DB_STATS_TTL_S
            busy = self._db_stats_thread is not None
            if not fresh and not busy:
                th = threading.Thread(target=self._refresh_db_stats,
                                      name="tn-retention-census", daemon=True)
                self._db_stats_thread = th
            else:
                th = None
        if th is not None:
            th.start()
        return snap or {"raw_rows": None, "measuring": True}

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.store.ensure_schema()
        self._thread = threading.Thread(target=self._loop, name="tn-retention-engine", daemon=True)
        self._thread.start()
        # Warm the row census off the request path (2026-08-22). Without this the
        # FIRST /retention/v2/status after a restart pays for the COUNT(*) and
        # the cold page cache — measured 5.3 s on a 9 M-row store, which is what
        # the operator sees when they open Backup & Retention right after boot.
        def _warm() -> None:
            self._refresh_db_stats()
            try:
                self.measured_row_costs()
            except Exception:
                pass

        threading.Thread(target=_warm, name="tn-retention-census-warm", daemon=True).start()
        log.info("retention engine armed (first pass in %ds)", RETENTION_BOOT_DELAY_S)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t and t.is_alive():
            try:
                t.join(timeout=3.0)
            except Exception:
                pass

    def wake(self) -> None:
        self._wake.set()

    def _sleep(self, seconds: float) -> bool:
        """Interruptible sleep. False when we should stop."""
        if seconds <= 0:
            return not self._stop.is_set()
        self._wake.wait(timeout=seconds)
        self._wake.clear()
        return not self._stop.is_set()

    def _loop(self) -> None:
        # I4: never compete with boot. Wait out the delay AND the health gate.
        deadline = time.time() + RETENTION_BOOT_DELAY_S
        while not self._stop.is_set() and time.time() < deadline:
            self._stop.wait(timeout=min(5.0, max(0.5, deadline - time.time())))
        while not self._stop.is_set():
            if self._boot_ready_fn is not None:
                try:
                    if not self._boot_ready_fn():
                        self._stop.wait(timeout=10.0)
                        continue
                except Exception:
                    pass
            break
        log.info("retention engine started")
        while not self._stop.is_set():
            try:
                self.run_once(reason="scheduled")
            except Exception as exc:                    # never let the thread die
                log.warning("retention tick failed: %s", exc, exc_info=True)
            if not self._sleep(RETENTION_TICK_S):
                break
        log.info("retention engine stopped")

    # -- gating helpers ---------------------------------------------------
    def _writer_busy(self) -> bool:
        if self._writer_busy_fn is None:
            return False
        try:
            return bool(self._writer_busy_fn())
        except Exception:
            return False

    @staticmethod
    def _in_window(window: str, when: Optional[datetime] = None) -> bool:
        if not window:
            return True
        try:
            start_s, end_s = [p.strip() for p in window.split("-", 1)]
            now = (when or datetime.now()).time()
            sh, sm = [int(x) for x in start_s.split(":")]
            eh, em = [int(x) for x in end_s.split(":")]
            start = sh * 60 + sm
            end = eh * 60 + em
            cur = now.hour * 60 + now.minute
            if start == end:
                return True
            if start < end:
                return start <= cur < end
            return cur >= start or cur < end          # window crosses midnight
        except Exception:
            return True

    def _note(self, job: str, **fields: Any) -> None:
        entry = {"utc": _utc_now_text(), "job": job}
        entry.update(fields)
        self._activity.append(entry)
        if len(self._activity) > 200:
            del self._activity[: len(self._activity) - 200]

    # -- the tick ---------------------------------------------------------
    def run_in_background(self, *, reason: str = "manual", dry_run: bool = False,
                          force: bool = False) -> bool:
        """Start a maintenance pass on its own thread and return immediately.

        2026-08-23: run_once() holds the HTTP request for the whole pass. That is
        fine on a small store and impossible on a real one — a 13 GB historian
        takes many minutes, while the browser gives up after 12 s and shows
        "signal is aborted without reason". The work had actually started and
        kept going; only the answer was lost, so "delete data manually" looked
        broken while it was in fact running.

        Returns False when a pass is already in flight (the caller should poll
        status.engine.busy rather than queue a second one).
        """
        if self._run_lock.locked():
            return False

        def _worker() -> None:
            try:
                self.run_once(reason=reason, dry_run=dry_run, force=force)
            except Exception:
                log.exception("retention: background run failed (%s)", reason)

        threading.Thread(target=_worker, name="tn-retention-manual", daemon=True).start()
        return True

    def run_once(self, *, reason: str = "manual", dry_run: bool = False,
                 force: bool = False) -> Dict[str, Any]:
        """One full maintenance pass. Safe to call from an API handler."""
        if not self._run_lock.acquire(blocking=False):
            return {"ok": False, "skipped": "a maintenance pass is already running"}
        try:
            started = time.time()
            summary: Dict[str, Any] = {
                "reason": reason, "dry_run": dry_run, "started_utc": _utc_now_text(),
                "rollups": [], "prunes": [], "other": {}, "backups": [], "notes": [],
            }
            if time.time() < self._paused_until and not force:
                summary["notes"].append("engine paused (disk emergency backoff)")
                return summary

            policy = self.store.get_active_policy()
            disk = self._disk_status()
            summary["disk"] = disk

            if policy is None:
                summary["notes"].append("no active retention policy — nothing is deleted")
                # Even with no policy we keep the WAL small and take config backups.
                self._checkpoint_wal()
                if not dry_run:
                    summary["backups"] = self._maybe_backup(None)
                self._last_tick = summary
                return summary

            tenants = self.store.tenants()
            summary["tenants"] = tenants
            deadline = started + max(60, int((policy.get("maintenance") or {}).get("max_run_minutes", 30)) * 60)

            # 1. ROLLUPS — always, they are cheap and they unlock pruning.
            for tenant in tenants:
                summary["rollups"].extend(self._rollup_tenant(policy, tenant, deadline, dry_run))
                if self._stop.is_set() or time.time() > deadline:
                    break

            # 2. PRUNE — only inside the window (unless catching up / forced).
            maint = policy.get("maintenance") or {}
            allowed = force or self._in_window(str(maint.get("window_local") or ""))
            behind = self._is_behind(policy, tenants)
            if not allowed and behind and bool(maint.get("catch_up_outside_window", True)):
                allowed = True
                summary["notes"].append("outside maintenance window but behind — catching up")
            if disk.get("emergency"):
                allowed = True
                summary["notes"].append("disk critically low — pruning immediately")
            if allowed:
                for tenant in tenants:
                    summary["prunes"].extend(self._prune_tenant(policy, tenant, deadline, dry_run))
                    if self._stop.is_set() or time.time() > deadline:
                        break
            else:
                summary["notes"].append("outside maintenance window — delete deferred")

            # 3. Other data classes + hygiene + backups.
            if not dry_run:
                summary["other"] = self._prune_other_data(policy)
                self._checkpoint_wal()
                self._incremental_vacuum()
                summary["backups"] = self._maybe_backup(policy)

            summary["took_s"] = round(time.time() - started, 2)
            summary["finished_utc"] = _utc_now_text()
            self._last_tick = summary

            touched = (any(r.get("rows") for r in summary["rollups"])
                       or any(p.get("deleted") for p in summary["prunes"])
                       or summary["backups"])
            if touched or reason != "scheduled":
                self.store.log_run("ok", summary, dry_run=dry_run)
            return summary
        finally:
            self._run_lock.release()

    # -- rollups ----------------------------------------------------------
    def _tier_key(self, res_s: int) -> str:
        return f"r{res_s}"

    def _rollup_tenant(self, policy: Dict[str, Any], tenant: str,
                       deadline: float, dry_run: bool) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        tiers = policy.get("tiers") or []
        if not tiers:
            return out
        newest = self.store.newest_raw_ms(tenant)
        if newest is None:
            return out
        for idx, tier in enumerate(tiers):
            res_s = int(tier.get("resolution_s") or parse_resolution(tier.get("resolution")) or 0)
            if res_s <= 0:
                continue
            key = self._tier_key(res_s)
            state = self.store.get_state(self.TARGET_LOCAL, key)
            src_res = None if idx == 0 else int(
                tiers[idx - 1].get("resolution_s")
                or parse_resolution(tiers[idx - 1].get("resolution")) or 0)
            # Where to start: the watermark, else the oldest data we still hold.
            start_ms = state.get("materialized_to_ms")
            if start_ms is None:
                if src_res is None:
                    start_ms = self.store.oldest_raw_ms(tenant)
                else:
                    start_ms = self._oldest_rollup_ms(tenant, src_res)
                if start_ms is None:
                    continue
            # A bucket is only closed once it has ended plus the late-arrival grace.
            if src_res is None:
                available_to = newest
            else:
                src_state = self.store.get_state(self.TARGET_LOCAL, self._tier_key(src_res))
                available_to = src_state.get("materialized_to_ms") or 0
            closed_to = min(available_to, _now_ms() - ROLLUP_GRACE_S * 1000)
            closed_to = (closed_to // (res_s * 1000)) * (res_s * 1000)
            if closed_to <= start_ms:
                continue
            if dry_run:
                out.append({"tier": key, "tenant": tenant, "would_cover_ms": closed_to - start_ms})
                continue
            rows, cursor = self._rollup_range(tenant, res_s, src_res, start_ms, closed_to, deadline)
            if cursor > (state.get("materialized_to_ms") or -1):
                out.append({"tier": key, "tenant": tenant, "rows": rows,
                            "from": _ms_to_sql(start_ms), "to": _ms_to_sql(cursor)})
                self._note("rollup", tier=key, tenant=tenant, rows=rows,
                           to=_ms_to_sql(cursor))
            if self._stop.is_set() or time.time() > deadline:
                break
        return out

    def _oldest_rollup_ms(self, tenant: str, res_s: int) -> Optional[int]:
        conn = self.store.connect(readonly=True)
        try:
            row = conn.execute(
                "SELECT MIN(bucket_ms) FROM historian_rollup WHERE resolution_s=? AND tenant_id=?",
                (res_s, tenant),
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else None
        except Exception:
            return None
        finally:
            conn.close()

    def _rollup_range(self, tenant: str, res_s: int, src_res: Optional[int],
                      start_ms: int, end_ms: int, deadline: float) -> Tuple[int, int]:
        """Materialise [start_ms, end_ms) in chunks. Returns (rows, cursor).

        The watermark is written in the SAME transaction as the rows (I3), so a
        crash can only ever repeat a chunk, never skip one."""
        key = self._tier_key(res_s)
        chunk_ms = max(res_s, ROLLUP_CHUNK_S) * 1000
        # Chunks must be bucket-aligned or a bucket could straddle two chunks and
        # be written twice with partial data (the upsert would keep the last one).
        chunk_ms = max(chunk_ms - (chunk_ms % (res_s * 1000)), res_s * 1000)
        cursor = start_ms - (start_ms % (res_s * 1000))
        total_rows = 0
        while cursor < end_ms and not self._stop.is_set():
            if time.time() > deadline:
                break
            if self._writer_busy():
                time.sleep(1.0)
                continue
            chunk_end = min(cursor + chunk_ms, end_ms)
            t0 = time.time()
            conn = self.store.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if src_res is None:
                    cur = conn.execute(_ROLLUP_FROM_RAW, {
                        "res": res_s, "tenant": tenant,
                        "from_ts": _ms_to_sql(cursor), "to_ts": _ms_to_sql(chunk_end),
                    })
                    rows = max(0, int(cur.rowcount or 0))
                    try:
                        conn.execute(_TEXT_EVENTS_SQL, {
                            "tenant": tenant,
                            "from_ts": _ms_to_sql(cursor), "to_ts": _ms_to_sql(chunk_end),
                        })
                    except Exception:
                        pass
                else:
                    cur = conn.execute(_ROLLUP_FROM_TIER, {
                        "res": res_s, "src_res": src_res, "tenant": tenant,
                        "from_ms": cursor, "to_ms": chunk_end,
                    })
                    rows = max(0, int(cur.rowcount or 0))
                RetentionStore._set_state_in(
                    conn, self.TARGET_LOCAL, key, "*",
                    materialized_to_ms=chunk_end, last_run_utc=_utc_now_text(),
                    last_status="ok", last_error=None,
                )
                conn.commit()
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                log.warning("rollup %s [%s..%s] failed: %s", key,
                            _ms_to_sql(cursor), _ms_to_sql(chunk_end), exc)
                self.store.set_state(self.TARGET_LOCAL, key, "*",
                                     last_status="error", last_error=str(exc)[:400],
                                     last_run_utc=_utc_now_text())
                break
            finally:
                conn.close()
            total_rows += rows
            cursor = chunk_end
            took_ms = (time.time() - t0) * 1000.0
            if took_ms > 1500:
                time.sleep(min(2.0, took_ms / 1000.0))
        return total_rows, cursor

    # -- pruning ----------------------------------------------------------
    def _is_behind(self, policy: Dict[str, Any], tenants: Sequence[str]) -> bool:
        """True when raw is more than a day past its retention — the signal that
        we must catch up even outside the maintenance window."""
        raw_keep = int((policy.get("raw") or {}).get("keep_s") or 0)
        if raw_keep <= 0:
            return False
        cutoff = _now_ms() - (raw_keep + 86400) * 1000
        for tenant in tenants:
            oldest = self.store.oldest_raw_ms(tenant)
            if oldest is not None and oldest < cutoff:
                return True
        return False

    def _prune_floor_ms(self, policy: Dict[str, Any], tier_index: int) -> Tuple[Optional[int], str]:
        """The oldest timestamp we are allowed to delete up to, and WHY it stops
        there. tier_index -1 == raw. This is invariant I1 in one function."""
        now = _now_ms()
        tiers = policy.get("tiers") or []
        if tier_index < 0:
            keep_s = int((policy.get("raw") or {}).get("keep_s") or 0)
            dependents = [tiers[0]] if tiers else []
        else:
            if tier_index >= len(tiers):
                return None, "unknown tier"
            keep_s = int(tiers[tier_index].get("keep_s")
                         or parse_duration(tiers[tier_index].get("keep"), 0) or 0)
            dependents = [tiers[tier_index + 1]] if tier_index + 1 < len(tiers) else []
        if keep_s <= 0:
            return None, "no retention configured"

        floor = now - keep_s * 1000
        reason = "policy retention"

        # (a) never delete a source a coarser tier has not consumed yet
        for dep in dependents:
            res_s = int(dep.get("resolution_s") or parse_resolution(dep.get("resolution")) or 0)
            if res_s <= 0:
                continue
            st = self.store.get_state(self.TARGET_LOCAL, self._tier_key(res_s))
            mat = st.get("materialized_to_ms")
            if mat is None:
                return None, f"waiting for the {format_duration(res_s)} level to be built"
            if mat < floor:
                floor, reason = mat, f"waiting for the {format_duration(res_s)} level to catch up"

        # (b) never delete raw the cloud mirror has not forwarded yet
        if tier_index < 0 and self._cloud_cursor_fn is not None:
            try:
                cursor_ms = self._cloud_cursor_fn()
            except Exception:
                cursor_ms = None
            if cursor_ms is not None and cursor_ms < floor:
                floor, reason = cursor_ms, "waiting for cloud sync to catch up"

        # (c) archive-before-delete
        if bool((policy.get("maintenance") or {}).get("archive_before_prune")):
            st = self.store.get_state(self.TARGET_LOCAL, "raw" if tier_index < 0
                                      else self._tier_key(int(tiers[tier_index].get("resolution_s") or 0)))
            arch = st.get("archived_to_ms")
            if arch is None:
                return None, "waiting for an archive to be written"
            if arch < floor:
                floor, reason = arch, "waiting for the archive to catch up"
        return floor, reason

    def _prune_tenant(self, policy: Dict[str, Any], tenant: str,
                      deadline: float, dry_run: bool) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        pace = max(0, int((policy.get("maintenance") or {}).get("pace_ms_per_batch", 20))) / 1000.0
        tiers = policy.get("tiers") or []

        floor, reason = self._prune_floor_ms(policy, -1)
        if floor is None:
            out.append({"tier": "raw", "tenant": tenant, "deleted": 0, "held_by": reason})
        else:
            deleted, remaining = self._delete_raw(tenant, floor, deadline, pace, dry_run)
            out.append({"tier": "raw", "tenant": tenant, "deleted": deleted,
                        "remaining": remaining, "before": _ms_to_sql(floor), "limit": reason})
            if deleted:
                self._note("prune", tier="raw", tenant=tenant, deleted=deleted)

        for idx, tier in enumerate(tiers):
            res_s = int(tier.get("resolution_s") or parse_resolution(tier.get("resolution")) or 0)
            if res_s <= 0:
                continue
            f, why = self._prune_floor_ms(policy, idx)
            if f is None:
                out.append({"tier": self._tier_key(res_s), "tenant": tenant, "deleted": 0, "held_by": why})
                continue
            deleted, remaining = self._delete_rollup(tenant, res_s, f, deadline, pace, dry_run)
            out.append({"tier": self._tier_key(res_s), "tenant": tenant, "deleted": deleted,
                        "remaining": remaining, "before": _ms_to_sql(f), "limit": why})
            if self._stop.is_set() or time.time() > deadline:
                break

        # text events follow their own (usually longest) retention
        text_keep = int((policy.get("text_tags") or {}).get("keep_s") or 0)
        if text_keep and text_keep != FOREVER and not dry_run:
            cutoff = _now_ms() - text_keep * 1000
            n = self._delete_simple(
                "DELETE FROM historian_text_events WHERE rowid IN ("
                "  SELECT rowid FROM historian_text_events WHERE tenant_id=? AND ts_ms<? ORDER BY ts_ms ASC LIMIT ?)",
                (tenant, cutoff), deadline, pace)
            if n:
                out.append({"tier": "text", "tenant": tenant, "deleted": n})
        return out

    def _adapt(self, took_ms: float) -> None:
        if took_ms < BATCH_FAST_MS:
            self._batch_size = min(DELETE_BATCH_MAX, int(self._batch_size * 2))
        elif took_ms > BATCH_SLOW_MS:
            self._batch_size = max(DELETE_BATCH_MIN, int(self._batch_size / 2))

    def _count_before(self, sql: str, params: Sequence[Any]) -> int:
        conn = self.store.connect(readonly=True)
        try:
            row = conn.execute(sql, params).fetchone()
            return int(row[0] or 0) if row else 0
        except Exception:
            return 0
        finally:
            conn.close()

    def _delete_raw(self, tenant: str, floor_ms: int, deadline: float,
                    pace: float, dry_run: bool) -> Tuple[int, int]:
        floor_ts = _ms_to_sql(floor_ms)
        remaining = self._count_before(
            "SELECT COUNT(*) FROM historian_readings WHERE tenant_id=? AND ts_utc<?",
            (tenant, floor_ts))
        if dry_run or remaining == 0:
            return 0, remaining
        deleted = 0
        while deleted < remaining and not self._stop.is_set():
            if time.time() > deadline:
                break
            if self._writer_busy():
                time.sleep(1.0)
                continue
            t0 = time.time()
            conn = self.store.connect()
            try:
                # ORDER BY ts_utc ASC = OLDEST FIRST. Without it SQLite walks
                # the (tenant_id, ts_utc DESC) index in its natural DESC order
                # and deletes from the middle of the window outwards, so an
                # interrupted catch-up leaves HOLES: recent days gone while
                # month-old data survives. Measured on a real 8M-row edge.
                # The plan is identical (same covering index, walked backwards),
                # so ordering costs nothing and keeps raw a contiguous window.
                cur = conn.execute(
                    "DELETE FROM historian_readings WHERE id IN ("
                    "  SELECT id FROM historian_readings WHERE tenant_id=? AND ts_utc<?"
                    "  ORDER BY ts_utc ASC LIMIT ?)",
                    (tenant, floor_ts, self._batch_size))
                n = max(0, int(cur.rowcount or 0))
                conn.commit()
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                log.warning("raw prune failed: %s", exc)
                break
            finally:
                conn.close()
            if n == 0:
                break
            deleted += n
            self._adapt((time.time() - t0) * 1000.0)
            if pace:
                time.sleep(pace)
        if deleted:
            self.store.set_state(self.TARGET_LOCAL, "raw", "*", pruned_to_ms=floor_ms,
                                 last_run_utc=_utc_now_text(), last_status="ok")
            log.info("pruned %d raw row(s) older than %s (tenant=%s)", deleted, floor_ts, tenant)
        return deleted, max(0, remaining - deleted)

    def _delete_rollup(self, tenant: str, res_s: int, floor_ms: int,
                       deadline: float, pace: float, dry_run: bool) -> Tuple[int, int]:
        remaining = self._count_before(
            "SELECT COUNT(*) FROM historian_rollup WHERE resolution_s=? AND tenant_id=? AND bucket_ms<?",
            (res_s, tenant, floor_ms))
        if dry_run or remaining == 0:
            return 0, remaining
        deleted = 0
        while deleted < remaining and not self._stop.is_set():
            if time.time() > deadline:
                break
            t0 = time.time()
            conn = self.store.connect()
            try:
                cur = conn.execute(
                    "DELETE FROM historian_rollup WHERE (resolution_s, tenant_id, gateway_id, tag_name, bucket_ms) IN ("
                    "  SELECT resolution_s, tenant_id, gateway_id, tag_name, bucket_ms FROM historian_rollup"
                    "  WHERE resolution_s=? AND tenant_id=? AND bucket_ms<? ORDER BY bucket_ms ASC LIMIT ?)",
                    (res_s, tenant, floor_ms, self._batch_size))
                n = max(0, int(cur.rowcount or 0))
                conn.commit()
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                log.warning("rollup prune failed (res=%s): %s", res_s, exc)
                break
            finally:
                conn.close()
            if n == 0:
                break
            deleted += n
            self._adapt((time.time() - t0) * 1000.0)
            if pace:
                time.sleep(pace)
        if deleted:
            self.store.set_state(self.TARGET_LOCAL, self._tier_key(res_s), "*",
                                 pruned_to_ms=floor_ms, last_run_utc=_utc_now_text(),
                                 last_status="ok")
        return deleted, max(0, remaining - deleted)

    def _delete_simple(self, sql: str, params: Sequence[Any], deadline: float, pace: float) -> int:
        """Paced delete for the small tables (logs, audit, outbox, ...)."""
        total = 0
        while not self._stop.is_set() and time.time() <= deadline:
            conn = self.store.connect()
            try:
                cur = conn.execute(sql, tuple(params) + (self._batch_size,))
                n = max(0, int(cur.rowcount or 0))
                conn.commit()
            except Exception:
                return total
            finally:
                conn.close()
            if n == 0:
                break
            total += n
            if pace:
                time.sleep(pace)
        return total

    # -- other data classes -----------------------------------------------
    def _prune_other_data(self, policy: Dict[str, Any]) -> Dict[str, int]:
        other = policy.get("other_data") or {}
        deadline = time.time() + 60
        out: Dict[str, int] = {}

        def cutoff_text(key: str) -> Optional[str]:
            secs = parse_duration(other.get(key), None)
            if secs is None or secs == FOREVER or secs <= 0:
                return None
            return _ms_to_sql(_now_ms() - secs * 1000)

        c = cutoff_text("app_logs_keep")
        if c:
            n = self._delete_simple(
                "DELETE FROM app_logs WHERE id IN (SELECT id FROM app_logs WHERE ts_utc<? LIMIT ?)",
                (c,), deadline, 0.005)
            if n:
                out["app_logs"] = n
        c = cutoff_text("audit_keep")
        if c:
            for table, col in (("config_audit", "created_utc"), ("cp_security_audit_log", "created_utc")):
                try:
                    n = self._delete_simple(
                        f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {col}<? LIMIT ?)",
                        (c,), deadline, 0.005)
                    if n:
                        out[table] = n
                except Exception:
                    pass
        c = cutoff_text("retention_runs_keep")
        if c:
            n = self._delete_simple(
                "DELETE FROM retention_runs WHERE id IN (SELECT id FROM retention_runs WHERE run_utc<? LIMIT ?)",
                (c,), deadline, 0.005)
            if n:
                out["retention_runs"] = n
        return out

    # -- hygiene ----------------------------------------------------------
    def _checkpoint_wal(self) -> None:
        try:
            conn = sqlite3.connect(self.store.db_path, timeout=3.0)
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            finally:
                conn.close()
        except Exception:
            pass

    def _incremental_vacuum(self) -> None:
        """Only does anything when the file was created with auto_vacuum=INCREMENTAL
        (new installs, or after a compaction). On a legacy file this is a no-op and
        the freed pages are reused by new rows instead — the file stops growing."""
        try:
            conn = sqlite3.connect(self.store.db_path, timeout=5.0)
            try:
                if int(conn.execute("PRAGMA auto_vacuum").fetchone()[0] or 0) == 2:
                    conn.execute("PRAGMA incremental_vacuum(2000)")
                    conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

    # -- backups ----------------------------------------------------------
    def _maybe_backup(self, policy: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        cfg = dict(DEFAULT_BACKUPS)
        if policy:
            cfg.update(policy.get("backups") or {})
        if not cfg.get("enabled", True):
            return []
        done: List[Dict[str, Any]] = []
        location = str(cfg.get("location") or "")
        now = time.time()
        keep_cfg = int(cfg.get("config_daily_keep") or 0)
        if keep_cfg > 0 and now - self._last_backup_config >= 86400:
            existing = [r for r in self.backups.list_backups(location, 1000)
                        if r["kind"] == BACKUP_KIND_CONFIG]
            fresh = existing and (time.time() - _sql_ts_to_ms(existing[0]["modified_utc"]) / 1000.0) < 86400
            if not fresh:
                try:
                    res = self.backups.create_config_backup(location, label="auto")
                    done.append(res)
                    self.backups.rotate(location, BACKUP_KIND_CONFIG, keep_cfg)
                except Exception as exc:
                    log.warning("config backup failed: %s", exc)
            self._last_backup_config = now
        keep_full = int(cfg.get("historian_weekly_keep") or 0)
        if keep_full > 0 and now - self._last_backup_full >= 7 * 86400:
            existing = [r for r in self.backups.list_backups(location, 1000)
                        if r["kind"] == BACKUP_KIND_FULL]
            fresh = existing and (time.time() - _sql_ts_to_ms(existing[0]["modified_utc"]) / 1000.0) < 7 * 86400
            if not fresh:
                try:
                    res = self.backups.create_full_backup(
                        location, label="auto", should_stop=lambda: self._stop.is_set())
                    if res.get("ok"):
                        done.append(res)
                        self.backups.rotate(location, BACKUP_KIND_FULL, keep_full)
                except Exception as exc:
                    log.warning("full backup failed: %s", exc)
            self._last_backup_full = now
        return done

    # -- disk -------------------------------------------------------------
    def _disk_status(self) -> Dict[str, Any]:
        try:
            usage = shutil.disk_usage(os.path.dirname(self.store.db_path))
        except Exception:
            return {}
        free_pct = (usage.free / usage.total * 100.0) if usage.total else 100.0
        free_gb = usage.free / 1e9
        emergency = free_pct < DISK_EMERGENCY_PCT or free_gb < DISK_EMERGENCY_GB
        warn = free_pct < DISK_WARN_PCT
        status = {
            "total_bytes": usage.total, "free_bytes": usage.free,
            "free_pct": round(free_pct, 1), "warn": warn, "emergency": emergency,
        }
        if emergency:
            log.warning("disk critically low: %.1f GB free (%.1f%%) — retention will prune immediately",
                        free_gb, free_pct)
        return status

    # -- compaction -------------------------------------------------------
    def compact(self) -> Dict[str, Any]:
        """`VACUUM INTO` a fresh file (online — writers keep running), switch it to
        incremental auto-vacuum, and stage it for the next start.

        SQLite never shrinks a file on DELETE; the pages go on a freelist and are
        reused. That is fine for steady state, but after the first big cleanup a
        site wants its disk back — this is how."""
        src_size = os.path.getsize(self.store.db_path) if os.path.exists(self.store.db_path) else 0
        try:
            usage = shutil.disk_usage(os.path.dirname(self.store.db_path))
            if usage.free < src_size * 1.2:
                return {"ok": False, "message": (
                    f"Not enough free disk to compact safely: need about "
                    f"{src_size * 1.2 / 1e9:.1f} GB, {usage.free / 1e9:.1f} GB free.")}
        except Exception:
            pass
        target = self.store.db_path + ".compacted"
        for stale in (target, target + ".part"):
            try:
                if os.path.exists(stale):
                    os.remove(stale)
            except Exception:
                pass
        t0 = time.time()
        try:
            conn = self.store.connect(readonly=True, timeout=60.0)
            try:
                conn.execute("VACUUM INTO ?", (target + ".part",))
            finally:
                conn.close()
            post = sqlite3.connect(target + ".part", timeout=60.0)
            try:
                post.execute("PRAGMA auto_vacuum=INCREMENTAL")
                post.execute("VACUUM")          # cheap now: the file is already small
                post.close()
            except Exception:
                try:
                    post.close()
                except Exception:
                    pass
            os.replace(target + ".part", target)
        except Exception as exc:
            for stale in (target, target + ".part"):
                try:
                    if os.path.exists(stale):
                        os.remove(stale)
                except Exception:
                    pass
            return {"ok": False, "message": f"Compaction failed: {exc}"}
        new_size = os.path.getsize(target)
        return {
            "ok": True, "staged": True, "took_s": round(time.time() - t0, 1),
            "size_before_bytes": src_size, "size_after_bytes": new_size,
            "reclaimed_bytes": max(0, src_size - new_size),
            "message": (f"Compacted copy ready — {src_size / 1e9:.2f} GB will become "
                        f"{new_size / 1e9:.2f} GB. It is applied the next time TrustNode starts."),
        }

    def cancel_compaction(self) -> Dict[str, Any]:
        removed = False
        for suffix in (".compacted", ".compacted.part"):
            p = self.store.db_path + suffix
            try:
                if os.path.exists(p):
                    os.remove(p)
                    removed = True
            except Exception:
                pass
        return {"ok": True, "cancelled": removed}

    # -- status -----------------------------------------------------------
    def measured_row_costs(self) -> Dict[str, float]:
        """Bytes/row measured on THIS database, so estimates match reality.

        2026-08-22: this used to run two COUNT(*)s on every call to status(),
        which the Backup & Retention page polls — 2-3 s per request on a 9 M-row
        store, and invisible in the page because it happens after the response
        body's other fields are built. Bytes-per-row is a slowly-moving number,
        so it is cached like the row census and refreshed off the request path."""
        now = time.monotonic()
        with self._db_stats_lock:
            cached = dict(self._row_costs_cache or {})
        if cached and (now - float(cached.get("measured_mono") or 0)) < self.ROW_COSTS_TTL_S:
            return {"bytes_per_raw_row": cached["bytes_per_raw_row"],
                    "bytes_per_rollup_row": cached["bytes_per_rollup_row"]}
        costs = self._measure_row_costs_now()
        with self._db_stats_lock:
            self._row_costs_cache = dict(costs, measured_mono=now)
        return costs

    def _measure_row_costs_now(self) -> Dict[str, float]:
        raw_bytes, rollup_bytes = 1000.0, 190.0
        try:
            conn = self.store.connect(readonly=True)
            try:
                page = int(conn.execute("PRAGMA page_size").fetchone()[0] or 4096)
                pages = int(conn.execute("PRAGMA page_count").fetchone()[0] or 0)
                free = int(conn.execute("PRAGMA freelist_count").fetchone()[0] or 0)
                used = max(0, pages - free) * page
                # Reuse the cached census instead of counting again; fall back to
                # a real count only when the census has not measured yet.
                census = self._db_stats_cached()
                raw_rows = census.get("raw_rows")
                if raw_rows is None:
                    raw_rows = int(conn.execute("SELECT COUNT(*) FROM historian_readings").fetchone()[0] or 0)
                roll_rows = int(conn.execute("SELECT COUNT(*) FROM historian_rollup").fetchone()[0] or 0)
                if int(raw_rows) > 50000:
                    # Rollups and config are a small share while raw dominates.
                    raw_bytes = max(80.0, min(3000.0, used / float(int(raw_rows) + roll_rows * 0.2)))
            finally:
                conn.close()
        except Exception:
            pass
        return {"bytes_per_raw_row": raw_bytes, "bytes_per_rollup_row": rollup_bytes}

    def status(self) -> Dict[str, Any]:
        # Per-stage timing: this endpoint is polled by the Backup & Retention
        # page and has twice been the reason the page rendered nothing. When it
        # is slow, the log must name WHICH stage (2026-08-22).
        _t = time.monotonic()
        _stage_ms: Dict[str, float] = {}

        def _mark(name: str) -> None:
            nonlocal _t
            now = time.monotonic()
            _stage_ms[name] = round((now - _t) * 1000.0, 1)
            _t = now

        self.store.ensure_schema()
        _mark("schema")
        policy = self.store.get_active_policy()
        _mark("policy")
        out: Dict[str, Any] = {
            "engine": {
                "running": bool(self._thread and self._thread.is_alive()),
                "uptime_s": round(time.time() - self._started_at, 1),
                "first_pass_in_s": max(0, round(RETENTION_BOOT_DELAY_S - (time.time() - self._started_at), 0)),
                "batch_size": self._batch_size,
                "busy": self._run_lock.locked(),
            },
            "policy": policy,
            "disk": self._disk_status(),
            "levels": [],
            "database": {},
            "activity": list(reversed(self._activity[-25:])),
            "pending_restore": self.backups.pending_restore(),
            "pending_compaction": os.path.exists(self.store.db_path + ".compacted"),
            "last_tick": {k: v for k, v in (self._last_tick or {}).items()
                          if k in ("reason", "started_utc", "finished_utc", "took_s", "notes")},
        }
        # Cleanup progress, taken from the LAST pass rather than re-counting: a
        # COUNT over a multi-million-row window costs seconds, and this endpoint
        # is polled by the UI every few seconds.
        last = self._last_tick or {}
        prunes = last.get("prunes") or []
        backlog = sum(int(p.get("remaining") or 0) for p in prunes)
        holds = [p.get("held_by") for p in prunes if p.get("held_by")]
        out["cleanup"] = {
            "pending_rows": backlog,
            "deleted_last_pass": sum(int(p.get("deleted") or 0) for p in prunes),
            "in_progress": backlog > 0,
            "held_by": holds[0] if holds else None,
            "measured_utc": last.get("finished_utc") or last.get("started_utc"),
        }
        try:
            db_size = os.path.getsize(self.store.db_path)
        except Exception:
            db_size = 0
        _mark("prelude")
        conn = self.store.connect(readonly=True)
        _mark("connect")
        try:
            page = int(conn.execute("PRAGMA page_size").fetchone()[0] or 4096)
            free = int(conn.execute("PRAGMA freelist_count").fetchone()[0] or 0)
            # rowid order == insertion order for the append-only historian, and
            # retention only ever deletes from the oldest end, so the first/last
            # row by id are the oldest/newest timestamps — 1 ms instead of the
            # 2.1 s a MIN/MAX(ts_utc) full scan costs at 9 M rows.
            _first = conn.execute(
                "SELECT ts_utc FROM historian_readings ORDER BY id ASC LIMIT 1").fetchone()
            _last = conn.execute(
                "SELECT ts_utc FROM historian_readings ORDER BY id DESC LIMIT 1").fetchone()
            oldest = _first[0] if _first else None
            newest = _last[0] if _last else None
            _census = self._db_stats_cached()
            raw_rows = _census.get("raw_rows")
            out["database"] = {
                "path": self.store.db_path, "size_bytes": db_size,
                "reclaimable_bytes": free * page,
                "raw_rows": raw_rows, "oldest_raw_utc": oldest, "newest_raw_utc": newest,
                "raw_rows_measured_utc": _census.get("measured_utc"),
                "raw_rows_measuring": bool(_census.get("measuring") or raw_rows is None),
                "auto_vacuum": int(conn.execute("PRAGMA auto_vacuum").fetchone()[0] or 0),
            }
            out["levels"].append({
                "key": "raw", "label": "Full detail (raw)",
                "keep": (policy or {}).get("raw", {}).get("keep") if policy else "",
                "rows": raw_rows, "oldest_utc": oldest, "newest_utc": newest,
            })
            if policy:
                now = _now_ms()
                for tier in policy.get("tiers") or []:
                    res_s = int(tier.get("resolution_s") or parse_resolution(tier.get("resolution")) or 0)
                    if res_s <= 0:
                        continue
                    row = conn.execute(
                        "SELECT COUNT(*) c, MIN(bucket_ms) mn, MAX(bucket_ms) mx "
                        "FROM historian_rollup WHERE resolution_s=?", (res_s,)).fetchone()
                    st = self.store.get_state(self.TARGET_LOCAL, self._tier_key(res_s))
                    mat = st.get("materialized_to_ms")
                    out["levels"].append({
                        "key": self._tier_key(res_s),
                        "label": f"{format_duration(res_s)} {tier.get('aggregate', 'avg')}",
                        "keep": tier.get("keep"), "resolution": tier.get("resolution"),
                        "rows": int(row["c"] or 0),
                        "oldest_utc": _ms_to_sql(int(row["mn"])) if row["mn"] else None,
                        "newest_utc": _ms_to_sql(int(row["mx"])) if row["mx"] else None,
                        "materialized_to_utc": _ms_to_sql(int(mat)) if mat else None,
                        "lag_s": round((now - int(mat)) / 1000.0, 1) if mat else None,
                        "last_status": st.get("last_status"), "last_error": st.get("last_error"),
                    })
        except Exception as exc:
            out["database"]["error"] = str(exc)
        finally:
            conn.close()
        _mark("db")

        costs = self.measured_row_costs()
        _mark("row_costs")
        out["row_costs"] = costs
        tags = self._tag_stats()
        _mark("tag_stats")
        out["collection"] = tags
        if policy:
            out["estimate"] = estimate_policy_size(
                policy, tag_count=tags.get("tag_count", 1), interval_s=tags.get("interval_s", 1.0),
                bytes_per_raw_row=costs["bytes_per_raw_row"],
                bytes_per_rollup_row=costs["bytes_per_rollup_row"])
        else:
            per_day = tags.get("tag_count", 1) * (86400.0 / max(0.05, tags.get("interval_s", 1.0)))
            out["estimate"] = {
                "levels": [], "total_bytes": None,
                "per_day_raw_bytes": int(per_day * costs["bytes_per_raw_row"]),
                "no_policy_year_bytes": int(365 * per_day * costs["bytes_per_raw_row"]),
            }
        free_bytes = (out.get("disk") or {}).get("free_bytes")
        per_day = (out.get("estimate") or {}).get("per_day_raw_bytes") or 0
        if policy is None and free_bytes and per_day:
            out["days_until_full"] = round(free_bytes / float(per_day), 1)
        else:
            out["days_until_full"] = None
        _mark("estimate")
        total = sum(_stage_ms.values())
        out["timing_ms"] = dict(_stage_ms, total=round(total, 1))
        if total > 2000:
            log.warning("retention status slow: %.0f ms %s", total, _stage_ms)
        return out

    def _tag_stats(self) -> Dict[str, Any]:
        if self._tag_stats_fn is not None:
            try:
                stats = self._tag_stats_fn() or {}
                if stats.get("tag_count"):
                    return stats
            except Exception:
                pass
        # Fallback: measure from the data itself.
        try:
            conn = self.store.connect(readonly=True)
            try:
                row = conn.execute(
                    "SELECT COUNT(DISTINCT tag_name) c FROM ("
                    "  SELECT tag_name FROM historian_readings ORDER BY id DESC LIMIT 5000)").fetchone()
                return {"tag_count": int(row["c"] or 1), "interval_s": 1.0, "source": "sampled"}
            finally:
                conn.close()
        except Exception:
            return {"tag_count": 1, "interval_s": 1.0, "source": "default"}


# --------------------------------------------------------------------------
# Module singleton (set by app.state during boot)
# --------------------------------------------------------------------------

_ENGINE: Optional[RetentionEngine] = None
_ENGINE_LOCK = threading.Lock()


def set_engine(engine: Optional[RetentionEngine]) -> None:
    global _ENGINE
    with _ENGINE_LOCK:
        _ENGINE = engine


def get_engine() -> Optional[RetentionEngine]:
    return _ENGINE


__all__ = [
    "RetentionEngine", "RetentionStore", "PolicyError",
    "validate_policy", "parse_duration", "format_duration", "parse_resolution",
    "RESOLUTION_CHOICES", "AGGREGATES", "BUILTIN_PRESETS", "FOREVER",
    "estimate_policy_size", "get_engine", "set_engine",
    "BackupManager", "apply_pending_db_swap", "DEFAULT_BACKUPS",
    "BACKUP_KIND_CONFIG", "BACKUP_KIND_FULL", "BACKUP_KIND_SAFETY",
]
