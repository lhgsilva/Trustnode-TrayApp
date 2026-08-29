# -*- coding: utf-8 -*-
"""A long Power Overview window must show the WHOLE window, not the last minute.

2026-08-26, from the field: "the power overview is not showing the seconds
reading for bigger range of time line hours or the day."

Cause: the overview asks for pre-aggregated buckets, and those are written ONLY
by the retention worker, ONLY for the window it is about to delete from raw.
Everything newer than the raw cutoff has no buckets, and an edge with no
retention policy has none at all. The agg query came back empty, the code fell
back to a capped raw pull (~8 000 rows), and a 24 h axis got a few minutes of
data - at 1 Hz across ~25 power tags, 8 000 rows is about five minutes.

This seeds a full day of 1 Hz power data with NO retention policy - the state a
normal edge is in - and asserts the bucketed read covers the whole day at a
density a chart can actually draw.
"""
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

TMP = tempfile.mkdtemp(prefix="tn-overview-")
os.environ["TRUSTNODE_SKIP_DOTENV"] = "1"
os.environ["TRUSTNODE_DATA_DIR"] = TMP
os.environ["TRUSTNODE_APP_STORE_PATH"] = os.path.join(TMP, "s.db")
os.environ["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"

FAILS = []


def check(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    extra = (" - " + str(detail)[:110]) if detail else ""
    print("  {0:58s}: {1}{2}".format(name, mark, extra))
    if not ok:
        FAILS.append(name)


from app.state import app_store  # noqa: E402

# --- seed 24 h of 1 Hz power data -----------------------------------------
# Written straight to SQLite: 2 million rows through the normal API would take
# minutes and prove nothing extra.
TAGS = ["active_power_kw", "voltage_v", "current_a", "insight.live_kw",
        "insight.total_kwh"]
END = datetime.now(timezone.utc).replace(microsecond=0)
START = END - timedelta(hours=24)
STEP_S = 1

print("  seeding 24 h of 1 Hz data for {0} tags...".format(len(TAGS)))
t0 = time.monotonic()
db = app_store._db_path
con = sqlite3.connect(db, timeout=60)
con.execute("PRAGMA journal_mode=WAL")
rows = []
ts = START
n = 0
while ts < END:
    stamp = ts.strftime("%Y-%m-%d %H:%M:%S.000")
    for i, tag in enumerate(TAGS):
        rows.append(("default", stamp, "pm-test", "Test Meter", "Meter",
                     "127.0.0.1", "Power Management", tag,
                     100.0 + i + (n % 60) * 0.1, None, "REAL", 192, "GOOD",
                     "power_modbus" if not tag.startswith("insight.") else "power_insight",
                     stamp))
    n += 1
    ts += timedelta(seconds=STEP_S)
    if len(rows) >= 50000:
        con.executemany(
            "INSERT INTO historian_readings (tenant_id, ts_utc, gateway_id, gateway_name,"
            " device_name, plc_ip, database_name, tag_name, value, value_text, data_type,"
            " quality, quality_label, source, created_utc)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        rows = []
if rows:
    con.executemany(
        "INSERT INTO historian_readings (tenant_id, ts_utc, gateway_id, gateway_name,"
        " device_name, plc_ip, database_name, tag_name, value, value_text, data_type,"
        " quality, quality_label, source, created_utc)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
con.commit()
total = con.execute("SELECT COUNT(*) FROM historian_readings").fetchone()[0]
agg_n = con.execute("SELECT COUNT(*) FROM historian_agg_minute").fetchone()[0]
con.close()
print("  seeded {0:,} raw rows in {1:.1f}s".format(total, time.monotonic() - t0))

check("the rollup tables are empty, as on a normal edge", agg_n == 0, agg_n)

FROM = START.strftime("%Y-%m-%d %H:%M:%S")
TO = END.strftime("%Y-%m-%d %H:%M:%S")

# --- what the overview actually asks for -----------------------------------
print("\n[a 24 hour window, minute buckets]")
t0 = time.monotonic()
out = app_store.get_historian_agg_rows(
    bucket="minute", from_utc=FROM, to_utc=TO,
    tag="active_power", limit=15000, source="power_modbus,power_insight")
took = time.monotonic() - t0
check("the 24 h window returns data at all", len(out) > 0, len(out))
check("  and it is fast enough for a chart refresh", took < 8.0,
      "{0:.2f}s".format(took))

stamps = sorted({str(r.get("ts") or "") for r in out})
if stamps:
    span_h = 0.0
    try:
        a = datetime.strptime(stamps[0], "%Y-%m-%d %H:%M:%S")
        b = datetime.strptime(stamps[-1], "%Y-%m-%d %H:%M:%S")
        span_h = (b - a).total_seconds() / 3600.0
    except Exception:
        pass
    print("  {0} buckets spanning {1:.1f} h".format(len(stamps), span_h))
    check("  it COVERS the whole day, not the last few minutes", span_h >= 23.0,
          "{0:.1f} h".format(span_h))
    check("  at roughly one bucket per minute", len(stamps) >= 1400,
          "{0} buckets".format(len(stamps)))
    check("  and stays chart-sized (not raw density)", len(stamps) <= 1500,
          "{0} buckets".format(len(stamps)))

# every bucket must carry a real average, plus min/max for a band.
# Take a MIDDLE bucket: the newest and oldest are partial minutes at the
# window boundary, so their sample counts are legitimately short.
sample = out[len(out) // 2] if out else {}
check("  buckets carry avg/min/max and a sample count",
      sample.get("value") is not None and sample.get("value_min") is not None
      and int(sample.get("sample_count") or 0) > 0,
      {k: sample.get(k) for k in ("value", "value_min", "value_max", "sample_count")})
check("  a minute bucket really aggregates ~60 samples",
      50 <= int(sample.get("sample_count") or 0) <= 70, sample.get("sample_count"))

# --- the insight tags the KPI strip reads ----------------------------------
print("\n[the insight tags follow the same path]")
ins = app_store.get_historian_agg_rows(
    bucket="minute", from_utc=FROM, to_utc=TO,
    tag="insight.", limit=15000, source="power_modbus,power_insight")
check("insight tags return over the full window", len(ins) > 0, len(ins))
ins_tags = {str(r.get("tag") or "") for r in ins}
check("  covering every insight tag seeded",
      {"insight.live_kw", "insight.total_kwh"} <= ins_tags, sorted(ins_tags)[:5])

# --- hour buckets for a week ----------------------------------------------
print("\n[an hour-bucket window]")
hr = app_store.get_historian_agg_rows(
    bucket="hour", from_utc=FROM, to_utc=TO,
    tag="active_power", limit=15000, source="power_modbus,power_insight")
hr_stamps = sorted({str(r.get("ts") or "") for r in hr})
check("hour buckets cover the day", len(hr_stamps) >= 23, len(hr_stamps))
check("  and collapse to ~24 points", len(hr_stamps) <= 25, len(hr_stamps))

# --- BOTH timestamp encodings must be readable -----------------------------
# The historian holds two: "YYYY-MM-DD HH:MM:SS.mmm" from the PLC/meter writers
# and, historically, isoformat() "…THH:MM:SS.ffffff+00:00" from the power
# manager. 'T' sorts after ' ', so a plain text range comparison excluded EVERY
# power row - a power chart over any window came back empty.
print()
print("[legacy ISO-T power rows are still readable]")
import sqlite3 as _sq2
_c2 = _sq2.connect(app_store._db_path, timeout=30)
_iso = []
_t = END - timedelta(hours=3)
while _t < END - timedelta(hours=2):
    _stamp = _t.isoformat()           # exactly what the old writer produced
    _iso.append(("default", _stamp, "pm-old", "Legacy Meter", "Meter", "127.0.0.1",
                 "Power Management", "legacy_power_kw", 42.5, None, "REAL", 192,
                 "GOOD", "power_modbus", _stamp))
    _t += timedelta(seconds=1)
_c2.executemany(
    "INSERT INTO historian_readings (tenant_id, ts_utc, gateway_id, gateway_name,"
    " device_name, plc_ip, database_name, tag_name, value, value_text, data_type,"
    " quality, quality_label, source, created_utc)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _iso)
_c2.commit()
_c2.close()
print("  seeded {0} rows in the legacy ISO-T format".format(len(_iso)))

legacy = app_store.get_historian_agg_rows(
    bucket="minute", from_utc=FROM, to_utc=TO,
    tag="legacy_power", limit=5000, source="power_modbus,power_insight")
check("legacy ISO-T rows are found by a normal range query", len(legacy) > 0,
      "{0} bucket(s)".format(len(legacy)))
# middle bucket again - the edges are partial minutes
_mid = legacy[len(legacy) // 2] if legacy else {}
check("  and they bucket by minute like everything else",
      55 <= int(_mid.get("sample_count") or 0) <= 65, _mid.get("sample_count"))
check("  with the right value",
      abs(float(_mid.get("value") or 0) - 42.5) < 0.01, _mid.get("value"))

# --- an existing rollup must NOT be re-scanned -----------------------------
# Re-bucketing raw on every refresh would be its own outage on a multi-million
# row historian. When the rollup already reaches the end of the window, the
# live scan must not run at all.
print()
print("[a covered window skips the raw scan]")
import sqlite3 as _sq
_c = _sq.connect(app_store._db_path, timeout=30)
_c.execute(
    "INSERT OR REPLACE INTO historian_agg_minute (bucket_utc, gateway_id, gateway_name,"
    " device_name, plc_ip, database_name, tag_name, avg_value, min_value, max_value,"
    " sample_count, quality_min, quality_max, created_utc, updated_utc)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (TO, "pm-test", "Test Meter", "Meter", "127.0.0.1", "Power Management",
     "active_power_kw", 111.0, 110.0, 112.0, 60, 192, 192, TO, TO))
_c.commit()
_c.close()

_calls = {"n": 0}
_orig = app_store.bucket_raw_historian_rows


def _counting(*a, **kw):
    _calls["n"] += 1
    return _orig(*a, **kw)


app_store.bucket_raw_historian_rows = _counting
try:
    covered = app_store.get_historian_agg_rows(
        bucket="minute", from_utc=FROM, to_utc=TO,
        tag="active_power", limit=15000, source="power_modbus,power_insight")
finally:
    app_store.bucket_raw_historian_rows = _orig
check("a rollup that reaches the window end skips the raw scan",
      _calls["n"] == 0, "{0} raw scan(s)".format(_calls["n"]))
check("  and still returns the rollup rows", len(covered) > 0, len(covered))

# --- the source filter must still bite -------------------------------------
print("\n[filters still apply]")
other = app_store.get_historian_agg_rows(
    bucket="minute", from_utc=FROM, to_utc=TO,
    tag="active_power", limit=100, source="allen_bradley")
check("a different source returns nothing", len(other) == 0, len(other))

print()
print("RESULT: {0}".format("PASS" if not FAILS else "FAIL - " + ", ".join(FAILS)))
sys.exit(0 if not FAILS else 2)
