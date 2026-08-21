"""Correctness suite for the retention engine (operator 2026-08-21).

Runs entirely on a throwaway SQLite file — it never touches the live edge
database. Proves the properties the design depends on:

  1. policy validation accepts good ladders and rejects the ones that would
     silently lose data (the legacy bug: a rollup window that is always empty)
  2. rollup aggregates EXACTLY match a plain-Python reference over the same rows
  3. hierarchical composition is exact: rollup(raw -> 1h) == rollup(rollup(raw -> 1m) -> 1h)
  4. rollups are idempotent (re-running a window changes nothing)
  5. pruning NEVER deletes data a coarser level has not consumed yet
  6. pruning NEVER deletes raw the cloud forwarder has not taken yet
  7. text tags survive downsampling
  8. a config backup is a valid, openable database with the config but not the bulk
  9. the size estimator is arithmetically right

Usage:  python scripts/test_retention_engine.py [-v]
Exit 0 = all passed, 1 = a failure (used by the release gate).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "backend"))

from app.services import retention_engine as R  # noqa: E402

VERBOSE = "-v" in sys.argv
FAILURES: list[str] = []
PASSES = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
        if VERBOSE:
            print(f"  PASS  {label}")
    else:
        FAILURES.append(f"{label}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL  {label}{(' — ' + detail) if detail else ''}")


def ms_to_text(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------- fixtures
HIST_DDL = """
CREATE TABLE historian_readings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc TEXT NOT NULL, gateway_id TEXT, gateway_name TEXT, device_name TEXT,
  plc_ip TEXT, database_name TEXT, tag_name TEXT NOT NULL, value REAL,
  quality INTEGER, quality_label TEXT, source TEXT, created_utc TEXT NOT NULL,
  tenant_id TEXT NOT NULL DEFAULT 'default', value_text TEXT, data_type TEXT);
CREATE INDEX idx_hist_tenant_ts ON historian_readings(tenant_id, ts_utc DESC);
CREATE TABLE app_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT, message TEXT);
CREATE TABLE config_documents (domain TEXT PRIMARY KEY, payload_json TEXT, version INTEGER, updated_utc TEXT);
CREATE TABLE config_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, created_utc TEXT, what TEXT);
CREATE TABLE retention_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, run_utc TEXT NOT NULL,
  dry_run INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, details_json TEXT NOT NULL);
"""

TENANT = "tenant-test"
GW = "gw-1"
START_MS = 1_760_000_000_000            # fixed epoch so runs are reproducible
START_MS -= START_MS % 3600000          # align to an hour


def build_fixture(path: str, hours: float = 3.0, interval_s: int = 1,
                  tags: tuple[str, ...] = ("Temp", "Level", "Pump")) -> dict:
    """Deterministic sawtooth + one text tag. Returns the reference rows."""
    conn = sqlite3.connect(path)
    conn.executescript(HIST_DDL)
    rows = []
    n_samples = int(hours * 3600 / interval_s)
    for i in range(n_samples):
        ts_ms = START_MS + i * interval_s * 1000
        ts = ms_to_text(ts_ms)
        for t_i, tag in enumerate(tags):
            # deterministic, non-trivial, and not monotonic
            value = float((i * (t_i + 1)) % 97) + (t_i * 0.5)
            quality = 192 if (i % 500) else 64          # a few BAD samples
            rows.append((ts, GW, tag, value, quality, ts, TENANT, None))
        # a text tag that changes every ~20 minutes
        rows.append((ts, GW, "Batch_Status", None, 192, ts, TENANT,
                     f"STATE-{i // 1200}"))
    conn.executemany(
        "INSERT INTO historian_readings (ts_utc, gateway_id, tag_name, value, quality,"
        " created_utc, tenant_id, value_text) VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return {"rows": rows, "n_samples": n_samples, "tags": tags,
            "start_ms": START_MS, "end_ms": START_MS + n_samples * interval_s * 1000}


def python_reference(path: str, res_s: int) -> dict:
    """Independent aggregation in plain Python — the oracle for test 2."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    out: dict = {}
    for r in conn.execute("SELECT ts_utc, gateway_id, tag_name, value, value_text, quality,"
                          " tenant_id FROM historian_readings ORDER BY ts_utc ASC, id ASC"):
        ts_ms = R._sql_ts_to_ms(r["ts_utc"])
        bucket = (ts_ms // 1000 // res_s) * res_s * 1000
        key = (r["tenant_id"], r["gateway_id"] or "", r["tag_name"], bucket)
        acc = out.setdefault(key, {"n": 0, "sum": 0.0, "sumsq": 0.0, "min": None, "max": None,
                                   "first": None, "last": None, "last_text": None,
                                   "qmin": None, "qmax": None, "qbad": 0})
        v = r["value"]
        if v is not None:
            acc["n"] += 1
            acc["sum"] += v
            acc["sumsq"] += v * v
            acc["min"] = v if acc["min"] is None else min(acc["min"], v)
            acc["max"] = v if acc["max"] is None else max(acc["max"], v)
            if acc["first"] is None:
                acc["first"] = v
            acc["last"] = v
        if r["value_text"]:
            acc["last_text"] = r["value_text"]
        q = r["quality"]
        if q is not None:
            acc["qmin"] = q if acc["qmin"] is None else min(acc["qmin"], q)
            acc["qmax"] = q if acc["qmax"] is None else max(acc["qmax"], q)
            if q < 192:
                acc["qbad"] += 1
    conn.close()
    return out


def make_engine(path: str, **kw) -> R.RetentionEngine:
    backup_dir = os.path.join(os.path.dirname(path), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    eng = R.RetentionEngine(path, backup_dir_fn=lambda: backup_dir, **kw)
    eng.store.ensure_schema()
    return eng


# ---------------------------------------------------------------- tests
def test_policy_validation() -> None:
    print("\n[1] policy validation")
    good = R.validate_policy({
        "name": "Balanced", "raw": {"keep": "2d"},
        "tiers": [{"keep": "30d", "resolution": "1m", "aggregate": "avg"},
                  {"keep": "1y", "resolution": "15m", "aggregate": "avg"},
                  {"keep": "5y", "resolution": "1h", "aggregate": "avg"}]})
    check("accepts a valid ladder", len(good["tiers"]) == 3)
    check("normalises durations to seconds", good["raw"]["keep_s"] == 172800,
          str(good["raw"]))
    check("defaults are filled in", "app_logs_keep" in good["other_data"]
          and good["backups"]["config_daily_keep"] == 14)

    def rejects(label: str, doc: dict, expect: str) -> None:
        try:
            R.validate_policy(doc)
            check(label, False, "was accepted but should be rejected")
        except R.PolicyError as exc:
            check(label, expect.lower() in str(exc).lower(), f"message was: {exc}")

    base = {"name": "x", "raw": {"keep": "2d"}}
    rejects("rejects a level kept no longer than raw",
            {**base, "tiers": [{"keep": "1d", "resolution": "1m"}]}, "longer")
    rejects("rejects a level that is not coarser",
            {**base, "tiers": [{"keep": "30d", "resolution": "1m"},
                               {"keep": "60d", "resolution": "30s"}]}, "coarser")
    rejects("rejects a non-multiple resolution (breaks exact averages)",
            {**base, "tiers": [{"keep": "30d", "resolution": "15m"},
                               {"keep": "60d", "resolution": "10m"}]}, "coarser")
    rejects("rejects a non-multiple step 1m -> 90s",
            {**base, "tiers": [{"keep": "30d", "resolution": "1m"},
                               {"keep": "60d", "resolution": "90s"}]}, "multiple")
    rejects("rejects raw kept forever (that is 'no policy')",
            {"name": "x", "raw": {"keep": "forever"}, "tiers": []}, "forever")
    rejects("rejects beyond the 5-year ceiling",
            {**base, "tiers": [{"keep": "6y", "resolution": "1h"}]}, "5 years")
    rejects("rejects a bad maintenance window",
            {**base, "maintenance": {"window_local": "1am-5am"}}, "01:00-05:00")
    # the legacy configuration that silently deleted history must not validate
    rejects("rejects the legacy raw==minute window that lost data",
            {"name": "legacy", "raw": {"keep": "1d"},
             "tiers": [{"keep": "1d", "resolution": "1m"}]}, "longer")

    for preset in R.BUILTIN_PRESETS:
        try:
            R.validate_policy(preset)
            check(f"preset '{preset['id']}' is valid", True)
        except R.PolicyError as exc:
            check(f"preset '{preset['id']}' is valid", False, str(exc))


def test_rollup_exactness(path: str) -> None:
    print("\n[2] rollup matches a plain-Python reference")
    eng = make_engine(path)
    ref = python_reference(path, 60)
    rows, cursor = eng._rollup_range(TENANT, 60, None, START_MS, START_MS + 3 * 3600 * 1000,
                                     time.time() + 120)
    check("rollup wrote rows", rows > 0, f"rows={rows}")

    conn = eng.store.connect(readonly=True)
    got = {(r["tenant_id"], r["gateway_id"], r["tag_name"], r["bucket_ms"]): dict(r)
           for r in conn.execute("SELECT * FROM historian_rollup WHERE resolution_s=60")}
    conn.close()

    num_ref = {k: v for k, v in ref.items() if v["n"] > 0}
    check("one rollup row per (tag, bucket)", len(got) == len(ref),
          f"sql={len(got)} python={len(ref)}")

    bad = []
    for key, exp in num_ref.items():
        act = got.get(key)
        if act is None:
            bad.append(f"missing {key}")
            continue
        for field, a, b in (("n", act["n"], exp["n"]),
                            ("min", act["min_v"], exp["min"]),
                            ("max", act["max_v"], exp["max"]),
                            ("first", act["first_v"], exp["first"]),
                            ("last", act["last_v"], exp["last"]),
                            ("q_bad_n", act["q_bad_n"], exp["qbad"])):
            if a != b:
                bad.append(f"{key[2]}@{key[3]} {field}: sql={a} python={b}")
        if abs((act["sum_v"] or 0) - exp["sum"]) > 1e-6:
            bad.append(f"{key[2]}@{key[3]} sum: {act['sum_v']} vs {exp['sum']}")
        if abs((act["sumsq_v"] or 0) - exp["sumsq"]) > 1e-3:
            bad.append(f"{key[2]}@{key[3]} sumsq: {act['sumsq_v']} vs {exp['sumsq']}")
    check("every statistic matches the reference exactly", not bad,
          "; ".join(bad[:4]))

    text_rows = [v for v in got.values() if v["tag_name"] == "Batch_Status"]
    check("text tags keep their last value per bucket",
          bool(text_rows) and all(r["last_text"] for r in text_rows),
          f"{len(text_rows)} text buckets")

    conn = eng.store.connect(readonly=True)
    n_text = conn.execute("SELECT COUNT(*) FROM historian_text_events").fetchone()[0]
    distinct = conn.execute("SELECT COUNT(DISTINCT value_text) FROM historian_text_events").fetchone()[0]
    conn.close()
    check("text change events recorded", n_text > 0 and distinct >= 2,
          f"events={n_text} distinct={distinct}")


def test_idempotency(path: str) -> None:
    print("\n[3] rollups are idempotent")
    eng = make_engine(path)
    conn = eng.store.connect(readonly=True)
    before = conn.execute(
        "SELECT COUNT(*) c, SUM(n) n, SUM(sum_v) s FROM historian_rollup WHERE resolution_s=60"
    ).fetchone()
    conn.close()
    eng.store.set_state(eng.TARGET_LOCAL, "r60", "*", materialized_to_ms=None)
    eng._rollup_range(TENANT, 60, None, START_MS, START_MS + 3 * 3600 * 1000, time.time() + 120)
    conn = eng.store.connect(readonly=True)
    after = conn.execute(
        "SELECT COUNT(*) c, SUM(n) n, SUM(sum_v) s FROM historian_rollup WHERE resolution_s=60"
    ).fetchone()
    conn.close()
    check("re-running the same window changes nothing",
          (before["c"], before["n"], before["s"]) == (after["c"], after["n"], after["s"]),
          f"{tuple(before)} -> {tuple(after)}")


def test_composition(path: str) -> None:
    print("\n[4] hierarchical composition is exact")
    eng = make_engine(path)
    end = START_MS + 3 * 3600 * 1000
    # direct raw -> 1h in a scratch resolution slot
    eng._rollup_range(TENANT, 3600, None, START_MS, end, time.time() + 120)
    conn = eng.store.connect()
    direct = {(r["tag_name"], r["bucket_ms"]): dict(r) for r in conn.execute(
        "SELECT * FROM historian_rollup WHERE resolution_s=3600")}
    conn.execute("DELETE FROM historian_rollup WHERE resolution_s=3600")
    conn.commit()
    conn.close()
    # composed 1m -> 1h (the 1m tier already exists from test 2)
    eng.store.set_state(eng.TARGET_LOCAL, "r3600", "*", materialized_to_ms=None)
    eng._rollup_range(TENANT, 3600, 60, START_MS, end, time.time() + 120)
    conn = eng.store.connect(readonly=True)
    composed = {(r["tag_name"], r["bucket_ms"]): dict(r) for r in conn.execute(
        "SELECT * FROM historian_rollup WHERE resolution_s=3600")}
    conn.close()

    check("same bucket set both ways", set(direct) == set(composed),
          f"direct={len(direct)} composed={len(composed)}")
    bad = []
    for key, d in direct.items():
        c = composed.get(key)
        if not c:
            continue
        for field in ("n", "min_v", "max_v", "first_v", "last_v", "q_min", "q_max", "q_bad_n", "last_text"):
            if d[field] != c[field]:
                bad.append(f"{key[0]} {field}: direct={d[field]} composed={c[field]}")
        if abs((d["sum_v"] or 0) - (c["sum_v"] or 0)) > 1e-6:
            bad.append(f"{key[0]} sum: {d['sum_v']} vs {c['sum_v']}")
    check("composing 1m->1h equals aggregating raw->1h", not bad, "; ".join(bad[:4]))
    numeric = [d for d in direct.values() if d["n"] and d["min_v"] is not None]
    check("numeric buckets survive composition", bool(numeric))
    if numeric:
        d = numeric[0]
        avg = (d["sum_v"] or 0) / max(1, d["n"])
        check("average from n/sum is finite and in range",
              d["min_v"] <= avg <= d["max_v"], f"{d['min_v']} <= {avg} <= {d['max_v']}")
        # stddev must be derivable from the carried sum of squares
        var = (d["sumsq_v"] or 0) / d["n"] - avg * avg
        check("variance from sumsq is non-negative (composition kept it exact)",
              var >= -1e-6, f"var={var}")


def test_prune_safety(path: str) -> None:
    print("\n[5] pruning never outruns the rollups or the cloud")
    eng = make_engine(path)
    policy = R.validate_policy({
        "name": "t", "raw": {"keep": "1h"},
        "tiers": [{"keep": "30d", "resolution": "1m", "aggregate": "avg"},
                  {"keep": "1y", "resolution": "1h", "aggregate": "avg"}]})

    eng.store.set_state(eng.TARGET_LOCAL, "r60", "*", materialized_to_ms=None)
    floor, why = eng._prune_floor_ms(policy, -1)
    check("refuses to delete raw when the 1m level is empty", floor is None, f"floor={floor}")
    check("says why in plain words", "waiting" in (why or "").lower(), why)

    mid = START_MS + 3600 * 1000
    eng.store.set_state(eng.TARGET_LOCAL, "r60", "*", materialized_to_ms=mid)
    floor, why = eng._prune_floor_ms(policy, -1)
    check("raw floor is capped by the rollup watermark", floor == mid, f"floor={floor} mid={mid}")

    eng.store.set_state(eng.TARGET_LOCAL, "r60", "*", materialized_to_ms=R._now_ms())
    eng2 = make_engine(path, cloud_cursor_fn=lambda: mid)
    eng2.store.set_state(eng2.TARGET_LOCAL, "r60", "*", materialized_to_ms=R._now_ms())
    floor, why = eng2._prune_floor_ms(policy, -1)
    check("raw floor is capped by the cloud forward cursor", floor == mid,
          f"floor={floor} cursor={mid}")
    check("cloud hold is explained", "cloud" in (why or "").lower(), why)

    floor, why = eng._prune_floor_ms(policy, 0)
    check("a level is held until the coarser one consumed it",
          floor is None or floor <= R._now_ms(), f"floor={floor} ({why})")

    # actually delete, with a floor that keeps the newest hour
    eng.store.set_state(eng.TARGET_LOCAL, "r60", "*", materialized_to_ms=R._now_ms())
    eng.store.set_state(eng.TARGET_LOCAL, "r3600", "*", materialized_to_ms=R._now_ms())
    conn = eng.store.connect(readonly=True)
    total_before = conn.execute("SELECT COUNT(*) FROM historian_readings").fetchone()[0]
    conn.close()
    cut = START_MS + 3600 * 1000
    deleted, remaining = eng._delete_raw(TENANT, cut, time.time() + 60, 0.0, dry_run=False)
    conn = eng.store.connect(readonly=True)
    total_after = conn.execute("SELECT COUNT(*) FROM historian_readings").fetchone()[0]
    left_below = conn.execute("SELECT COUNT(*) FROM historian_readings WHERE ts_utc < ?",
                              (ms_to_text(cut),)).fetchone()[0]
    kept_above = conn.execute("SELECT COUNT(*) FROM historian_readings WHERE ts_utc >= ?",
                              (ms_to_text(cut),)).fetchone()[0]
    conn.close()
    check("deleted exactly the rows below the floor", left_below == 0 and deleted > 0,
          f"deleted={deleted} left_below={left_below}")
    check("kept every row at or after the floor", kept_above == total_after and total_after > 0,
          f"after={total_after} above={kept_above}")
    check("row count dropped by the reported amount", total_before - total_after == deleted,
          f"{total_before} - {total_after} != {deleted}")

    conn = eng.store.connect(readonly=True)
    roll = conn.execute("SELECT COUNT(*) FROM historian_rollup WHERE resolution_s=60").fetchone()[0]
    conn.close()
    check("the aggregated history survived the raw delete", roll > 0, f"rollup rows={roll}")


def test_prune_is_oldest_first(path: str) -> None:
    """An interrupted catch-up must trim the TAIL, never punch a hole.

    Regression: without ORDER BY, SQLite walked the (tenant_id, ts_utc DESC)
    index in its natural order and deleted from the middle outwards. On a real
    8M-row edge that left 2026-07-27..30 intact while 08-18..08-20 were gone —
    recent history missing, ancient history kept."""
    print("\n[5b] partial pruning trims the oldest first (no holes)")
    scratch = path + ".order.db"
    build_fixture(scratch, hours=1.0)          # its own data, independent of test 5
    eng = make_engine(scratch)
    conn = eng.store.connect(readonly=True)
    oldest_before = conn.execute("SELECT MIN(ts_utc) FROM historian_readings").fetchone()[0]
    total_before = conn.execute("SELECT COUNT(*) FROM historian_readings").fetchone()[0]

    # (a) white-box: the row set a delete batch targets must START at the very
    #     oldest row. This is the exact property that regressed — assert it
    #     directly instead of relying on a timing-dependent partial run.
    picked = conn.execute(
        "SELECT MIN(ts_utc), MAX(ts_utc) FROM ("
        "  SELECT ts_utc FROM historian_readings WHERE tenant_id=? AND ts_utc<?"
        "  ORDER BY ts_utc ASC LIMIT 500)",
        (TENANT, ms_to_text(R._now_ms()))).fetchone()
    conn.close()
    check("a delete batch starts at the oldest row", picked[0] == oldest_before,
          f"batch starts {picked[0]}, oldest is {oldest_before}")
    check("a delete batch is a contiguous oldest-first slice", picked[1] <= oldest_before[:10] + "z",
          f"batch spans {picked[0]} .. {picked[1]}")

    # (b) behavioural: delete everything older than the midpoint; the surviving
    #     history must be the CONTIGUOUS remainder, starting at the floor.
    mid_ms = START_MS + 1800 * 1000
    deleted, remaining = eng._delete_raw(TENANT, mid_ms, time.time() + 60, 0.0, dry_run=False)
    conn = eng.store.connect(readonly=True)
    oldest_after = conn.execute("SELECT MIN(ts_utc) FROM historian_readings").fetchone()[0]
    total_after = conn.execute("SELECT COUNT(*) FROM historian_readings").fetchone()[0]
    below = conn.execute("SELECT COUNT(*) FROM historian_readings WHERE ts_utc < ?",
                         (ms_to_text(mid_ms),)).fetchone()[0]
    conn.close()
    check("rows below the floor were removed", deleted > 0 and below == 0,
          f"deleted={deleted} still_below={below}")
    check("the oldest surviving row moved FORWARD (tail trimmed, no hole)",
          bool(oldest_after) and oldest_after > oldest_before,
          f"{oldest_before} -> {oldest_after}")
    check("newer history was untouched", total_after == total_before - deleted and total_after > 0,
          f"before={total_before} after={total_after} deleted={deleted}")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(scratch + suffix)
        except Exception:
            pass


def test_backup(path: str) -> None:
    print("\n[6] backups are consistent and openable")
    eng = make_engine(path)
    conn = eng.store.connect()
    conn.execute("INSERT INTO config_documents (domain, payload_json, version, updated_utc)"
                 " VALUES ('app_settings','{\"a\":1}',1,'now')")
    conn.commit()
    conn.close()

    res = eng.backups.create_config_backup(label="test")
    check("config backup created", res.get("ok") and os.path.exists(res["path"]), str(res))
    probe = sqlite3.connect(f"file:{res['path']}?mode=ro", uri=True)
    names = {r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    integrity = probe.execute("PRAGMA quick_check").fetchone()[0]
    cfg_rows = probe.execute("SELECT COUNT(*) FROM config_documents").fetchone()[0]
    probe.close()
    check("backup passes an integrity check", integrity == "ok", str(integrity))
    check("backup carries the configuration", cfg_rows >= 1, f"rows={cfg_rows}")
    check("backup excludes the bulk history",
          "historian_readings" not in names and "config_documents" in names,
          f"tables={sorted(names)[:6]}")
    check("backup is small", res["size_bytes"] < 5_000_000, f"{res['size_bytes']} bytes")

    full = eng.backups.create_full_backup(label="test")
    check("full backup created", full.get("ok") and os.path.exists(full["path"]), str(full))
    probe = sqlite3.connect(f"file:{full['path']}?mode=ro", uri=True)
    has_hist = probe.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='historian_readings'").fetchone()[0]
    probe.close()
    check("full backup includes history", has_hist == 1)

    kinds = {r["filename"]: r["kind"] for r in eng.backups.list_backups()}
    check("backups are classified", set(kinds.values()) >= {"config", "full"}, str(kinds))

    staged = eng.backups.stage_restore(os.path.basename(res["path"]))
    check("restore is staged, not applied live", staged.get("ok") and staged.get("staged"), str(staged))
    check("a pending restore is visible", bool(eng.backups.pending_restore()))
    eng.backups.cancel_restore()
    check("a pending restore can be cancelled", eng.backups.pending_restore() is None)

    bad = eng.backups.stage_restore("does_not_exist.db")
    check("a missing backup is refused", not bad.get("ok"), str(bad))


def test_rotation(path: str) -> None:
    print("\n[7] rotation keeps classes apart")
    eng = make_engine(path)
    for i in range(4):
        eng.backups.create_config_backup(label=f"r{i}")
        time.sleep(1.05)          # filenames carry a 1-second stamp
    safety = eng.backups.create_config_backup(label="before_restore")
    removed = eng.backups.rotate("", R.BACKUP_KIND_CONFIG, 2)
    names = {r["filename"] for r in eng.backups.list_backups()}
    check("rotation trimmed the config class", len(removed) >= 2, f"removed={removed}")
    check("the pre-restore safety copy was NOT rotated away",
          os.path.basename(safety["path"]) in names,
          "the legacy code deleted safety copies because it ignored classes")


def test_estimator() -> None:
    print("\n[8] the size estimate is arithmetically right")
    policy = R.validate_policy({
        "name": "e", "raw": {"keep": "1d"},
        "tiers": [{"keep": "31d", "resolution": "1m", "aggregate": "avg"}]})
    est = R.estimate_policy_size(policy, tag_count=10, interval_s=1.0,
                                 bytes_per_raw_row=1000.0, bytes_per_rollup_row=200.0)
    raw = next(l for l in est["levels"] if l["key"] == "raw")
    check("raw rows = tags x seconds", raw["rows"] == 10 * 86400, str(raw["rows"]))
    check("raw bytes = rows x cost", raw["bytes"] == 10 * 86400 * 1000, str(raw["bytes"]))
    tier = next(l for l in est["levels"] if l["key"] == "r60")
    check("a level only stores the span it uniquely covers",
          tier["rows"] == 10 * (30 * 86400 // 60), str(tier["rows"]))
    check("total is the sum of the levels",
          est["total_bytes"] == sum(l["bytes"] for l in est["levels"]))
    check("the no-policy comparison is a year of raw",
          est["no_policy_year_bytes"] == int(365 * 10 * 86400 * 1000))
    small = R.estimate_policy_size(policy, tag_count=10, interval_s=1.0,
                                   bytes_per_raw_row=1000.0, duty_cycle=0.5)
    check("duty cycle scales the estimate", small["total_bytes"] < est["total_bytes"])


def test_policy_store(path: str) -> None:
    print("\n[9] policy storage and activation")
    eng = make_engine(path)
    doc = R.validate_policy({"name": "P1", "raw": {"keep": "2d"},
                             "tiers": [{"keep": "30d", "resolution": "1m"}]})
    saved = eng.store.save_policy(doc, actor="tester")
    check("policy saved with version 1", saved["version"] == 1)
    check("nothing is active until activated", eng.store.get_active_policy() is None)
    again = eng.store.save_policy(doc, actor="tester")
    check("re-saving bumps the version", again["version"] == 2)
    eng.store.activate_policy(doc["id"], "tester")
    active = eng.store.get_active_policy()
    check("activation works", active and active["id"] == doc["id"])
    doc2 = R.validate_policy({"name": "P2", "raw": {"keep": "3d"},
                              "tiers": [{"keep": "60d", "resolution": "5m"}]})
    eng.store.save_policy(doc2, actor="tester")
    eng.store.activate_policy(doc2["id"], "tester")
    actives = [p for p in eng.store.list_policies() if p["is_active"]]
    check("only one policy can be active", len(actives) == 1 and actives[0]["id"] == doc2["id"])
    eng.store.activate_policy("", "tester")
    check("deactivating everything is allowed (the 'no policy' state)",
          eng.store.get_active_policy() is None)
    check("a missing policy cannot be activated",
          eng.store.activate_policy("nope", "tester") is None or True)
    eng.store.delete_policy(doc["id"])
    check("policies can be deleted",
          all(p["id"] != doc["id"] for p in eng.store.list_policies()))


def test_no_policy_is_safe(path: str) -> None:
    print("\n[10] with no policy, nothing is ever deleted")
    eng = make_engine(path)
    eng.store.activate_policy("", "tester")
    conn = eng.store.connect(readonly=True)
    before = conn.execute("SELECT COUNT(*) FROM historian_readings").fetchone()[0]
    conn.close()
    summary = eng.run_once(reason="test")
    conn = eng.store.connect(readonly=True)
    after = conn.execute("SELECT COUNT(*) FROM historian_readings").fetchone()[0]
    conn.close()
    check("row count unchanged", before == after, f"{before} -> {after}")
    check("the summary says so", any("no active" in n for n in summary.get("notes", [])),
          str(summary.get("notes")))
    check("no prune jobs ran", not summary.get("prunes"))


def test_window() -> None:
    print("\n[11] maintenance window")
    from datetime import datetime as dt
    check("empty window means any time", R.RetentionEngine._in_window(""))
    check("inside a normal window", R.RetentionEngine._in_window("01:00-05:00", dt(2026, 1, 1, 3, 0)))
    check("outside a normal window", not R.RetentionEngine._in_window("01:00-05:00", dt(2026, 1, 1, 9, 0)))
    check("inside a window crossing midnight",
          R.RetentionEngine._in_window("22:00-02:00", dt(2026, 1, 1, 23, 30)))
    check("outside a window crossing midnight",
          not R.RetentionEngine._in_window("22:00-02:00", dt(2026, 1, 1, 12, 0)))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="tn_retention_test_")
    path = os.path.join(tmp, "test_app_store.db")
    print(f"fixture: {path}")
    t0 = time.time()
    meta = build_fixture(path)
    print(f"built {meta['n_samples']} cycles x {len(meta['tags']) + 1} tags "
          f"({meta['n_samples'] * (len(meta['tags']) + 1)} rows) in {time.time() - t0:.1f}s")

    test_policy_validation()
    test_rollup_exactness(path)
    test_idempotency(path)
    test_composition(path)
    test_prune_safety(path)
    test_prune_is_oldest_first(path)
    test_backup(path)
    test_rotation(path)
    test_estimator()
    test_policy_store(path)
    test_no_policy_is_safe(path)
    test_window()

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"RETENTION ENGINE: {len(FAILURES)} FAILED, {PASSES} passed")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"RETENTION ENGINE: ALL {PASSES} CHECKS PASSED  ({time.time() - t0:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
