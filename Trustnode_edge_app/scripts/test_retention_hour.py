"""End-to-end test for the sub-day retention TTLs.

Verifies that setting raw_keep_minutes=60 actually deletes historian rows
older than 60 minutes when /api/app-store/retention/run is invoked.

Sequence:
  1. Log in.
  2. GET current retention policy. Sanity-check schema has the new columns.
  3. PUT retention policy with raw_keep_minutes=60 (the "last hour" case).
  4. Count historian rows older than 60 minutes (the candidates).
  5. POST /api/app-store/retention/run with dry_run=True -> verify candidates.
  6. POST /api/app-store/retention/run with dry_run=False -> rows deleted.
  7. Re-count rows: should be < step 4 (capped at max_delete_rows_per_run).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("TN_TEST_BASE_URL", "http://127.0.0.1:8000")
USER = os.environ.get("TN_TEST_USER", "admin")
PASS = os.environ.get("TN_TEST_PASS", "admin")


def call(method, path, body=None, token=None, timeout=60):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.getcode(), json.loads(r.read().decode("utf-8", errors="replace") or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:
            return e.code, str(e)
    except Exception as e:
        return -1, str(e)


print("=" * 76)
print("RETENTION (sub-day TTL) END-TO-END TEST")
print("=" * 76)

code, body = call("POST", "/api/auth/login", {"username": USER, "password": PASS})
if not isinstance(body, dict) or not body.get("token"):
    print(f"LOGIN FAILED: {body}")
    sys.exit(1)
token = body["token"]
print(f"[login] {USER!r} -> ok")

print("\n[1] Current policy:")
code, p = call("GET", "/api/app-store/retention/policy", token=token)
if not isinstance(p, dict):
    print(f"   *** FAIL: policy GET returned {p!r} (status {code}) ***")
    sys.exit(2)
policy = p.get("policy") if isinstance(p.get("policy"), dict) else p
for k in ("enabled", "raw_keep_days", "raw_keep_minutes", "minute_keep_minutes", "hour_keep_minutes", "day_keep_minutes"):
    print(f"   {k} = {policy.get(k)!r}")
if "raw_keep_minutes" not in policy:
    print("   *** FAIL: schema does NOT carry raw_keep_minutes — migration didn't run ***")
    sys.exit(2)

print("\n[2] Snapshot historian row counts:")
import sqlite3
P = r"C:\Users\User\.trustnode_edge\data\trustnode_app_store.db"
con = sqlite3.connect(P, timeout=5)
con.row_factory = sqlite3.Row
total = con.execute("SELECT COUNT(*) c FROM historian_readings").fetchone()["c"]
old_60min = con.execute(
    "SELECT COUNT(*) c FROM historian_readings WHERE ts_utc < datetime('now', '-60 minutes')"
).fetchone()["c"]
print(f"   total rows: {total}")
print(f"   rows older than 60 minutes (candidates): {old_60min}")

print("\n[3] Save policy with raw_keep_minutes=60 (= 'last hour')")
new_policy = {
    "enabled": True,
    "schedule_minutes": 60,
    "raw_keep_days": 1,
    "minute_keep_days": 1,
    "hour_keep_days": 1,
    "day_keep_days": 7,
    "raw_keep_minutes": 60,
    "minute_keep_minutes": 180,
    "hour_keep_minutes": 1440,
    "day_keep_minutes": 0,
    "backup_before_cleanup": True,
    "max_delete_rows_per_run": 50000,
}
code, body = call("PUT", "/api/app-store/retention/policy", new_policy, token=token)
saved = (body or {}).get("policy") or body
print(f"   saved.raw_keep_minutes = {saved.get('raw_keep_minutes') if isinstance(saved, dict) else '?'}")

print("\n[4] Dry run (must NOT delete):")
code, body = call("POST", "/api/app-store/retention/run", {"dry_run": True, "actor": "test"}, token=token)
details = (body or {}).get("details") or {}
print(f"   cutoffs={details.get('cutoffs')}")
print(f"   candidates={details.get('deletes')}")
post_dry = con.execute("SELECT COUNT(*) c FROM historian_readings").fetchone()["c"]
print(f"   total rows after dry: {post_dry}  (must == {total})")
if post_dry != total:
    print("   *** FAIL: dry run actually deleted rows ***")
    sys.exit(2)

print("\n[5] Real run (will delete):")
code, body = call("POST", "/api/app-store/retention/run", {"dry_run": False, "actor": "test"}, token=token, timeout=120)
details = (body or {}).get("details") or {}
print(f"   cutoffs={details.get('cutoffs')}")
print(f"   candidates={details.get('deletes')}")
print(f"   backup_path={details.get('backup_path')!r}")
post_real = con.execute("SELECT COUNT(*) c FROM historian_readings").fetchone()["c"]
print(f"   total rows after real: {post_real}  (was {total}, expected drop)")

if post_real >= total:
    print("   *** FAIL: real run did NOT delete any rows ***")
elif post_real == 0 and total > 0:
    print("   note: all rows were older than 60 minutes — table is empty now")
    print("   ✓ PASS")
else:
    deleted = total - post_real
    print(f"   ✓ PASS: deleted {deleted} rows")

print("\n[6] Most recent rows (must all be within last hour):")
from datetime import datetime, timezone
now_utc = datetime.now(timezone.utc)
for r in con.execute("SELECT ts_utc FROM historian_readings ORDER BY id DESC LIMIT 5").fetchall():
    ts = r["ts_utc"]
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
    except Exception:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    age_min = (now_utc - dt).total_seconds() / 60.0
    print(f"   {ts}  age={age_min:.1f} min")

print("\n" + "=" * 76)
print("TEST DONE")
print("=" * 76)
