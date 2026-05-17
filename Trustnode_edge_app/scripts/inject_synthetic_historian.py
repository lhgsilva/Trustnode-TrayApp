"""Inject synthetic historian samples for end-to-end smoke testing.

Writes one row per second to THREE fake tags simultaneously, into:
  - Local SQLite:  trustnode_app_store.db
                   * historian_readings  (per-sample rows)
                   * live_latest         (one row per (tenant,gw,tag))
  - Cloud Supabase: public.plc_readings  + public.live_latest

Why this exists:
  When the edge isn't polling a real PLC (no gateway configured, or you
  just want to exercise the cloud-side pipeline without a hardware
  dependency) the smoke_edge_to_cloud_dashboard script has no data to
  measure. This injector creates a measurable workload along the same
  table shapes the real edge writes.

Run:
  python inject_synthetic_historian.py                 # 60s default
  python inject_synthetic_historian.py --seconds 30
  python inject_synthetic_historian.py --tenant default
  python inject_synthetic_historian.py --no-local      # cloud only
  python inject_synthetic_historian.py --no-cloud      # local only

Reads cloud credentials from Trustnode_edge_app/.env (gitignored).
"""
from __future__ import annotations

import argparse
import io as _io
import math
import os
import random
import sqlite3
import sys as _sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# UTF-8 console for Windows.
try:
    _sys.stdout = _io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8", line_buffering=True)
except Exception:
    pass

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LOCAL_DB = Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db"
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

# Three synthetic tags. The smoke script will see these show up in
# /api/app-store/live with their tenant_id and timestamps.
GATEWAY_ID   = "gw-smoke-synth"
GATEWAY_NAME = "Synthetic Smoke Gateway"
DEVICE_NAME  = "Synthetic Smoke Device"
PLC_IP       = "127.0.0.1"
DB_NAME      = "smoke_synthetic"
SOURCE       = "smoke_synthetic"
SCHEMA       = "public"


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=60,
                    help="How long to stream samples (default 60)")
    ap.add_argument("--rate-hz", type=float, default=1.0,
                    help="Samples per second per tag (default 1.0)")
    ap.add_argument("--tenant", default="default",
                    help="tenant_id stamped on every row (default 'default')")
    ap.add_argument("--no-local", action="store_true", help="Skip local SQLite writes")
    ap.add_argument("--no-cloud", action="store_true", help="Skip Supabase writes")
    return ap.parse_args()


def load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if "=" in s and not s.startswith("#"):
            k, v = s.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


# ---------------------------------------------------------------------------
# Synthetic waveform generators
# ---------------------------------------------------------------------------
def generate_sample(tag: str, tick: int) -> tuple[float, str]:
    """Return (numeric_value, quality_label) for the given tag/tick."""
    if tag == "SmokeSimSaw":
        # 0..100 sawtooth, 30-second period
        return float((tick * 100.0 / 30.0) % 100.0), "GOOD"
    if tag == "SmokeSimSine":
        # 50 +/- 25 sine, 20-second period
        return 50.0 + 25.0 * math.sin(2 * math.pi * tick / 20.0), "GOOD"
    if tag == "SmokeSimNoise":
        # uniform random 0..1000
        return random.uniform(0.0, 1000.0), "GOOD"
    return 0.0, "GOOD"


TAGS = ["SmokeSimSaw", "SmokeSimSine", "SmokeSimNoise"]


# ---------------------------------------------------------------------------
# Local SQLite writer
# ---------------------------------------------------------------------------
class LocalWriter:
    def __init__(self, tenant: str) -> None:
        if not LOCAL_DB.exists():
            raise SystemExit(f"local DB not found: {LOCAL_DB}")
        # Open with WAL-aware busy timeout so we don't fight the edge process.
        self.conn = sqlite3.connect(f"file:{LOCAL_DB}?mode=rw", uri=True, timeout=15.0)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.tenant = tenant

    def write_batch(self, now_utc: str, tick: int) -> int:
        rows_for_historian = []
        for tag in TAGS:
            v, q = generate_sample(tag, tick)
            rows_for_historian.append((
                now_utc, GATEWAY_ID, GATEWAY_NAME, DEVICE_NAME, PLC_IP, DB_NAME,
                tag, v, 192, q, SOURCE, now_utc, self.tenant,
            ))
        self.conn.executemany("""
            INSERT INTO historian_readings
              (ts_utc, gateway_id, gateway_name, device_name, plc_ip, database_name,
               tag_name, value, quality, quality_label, source, created_utc, tenant_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows_for_historian)
        # NOTE: Local SQLite has NO live_latest table — the edge keeps the
        # latest-per-tag values in an in-memory cache (_local_live_latest_cache
        # in services/app_store.py). The cache is rebuilt from
        # historian_readings on the next live-query, so the rows we just
        # inserted will surface through the edge's normal /api/app-store/live
        # endpoint without us having to touch a per-tag table.
        self.conn.commit()
        return len(rows_for_historian)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Cloud Supabase writer
# ---------------------------------------------------------------------------
class CloudWriter:
    def __init__(self, tenant: str) -> None:
        import psycopg  # noqa: F401  (imported here so --no-cloud skips the dep)
        self.conn = self._connect()
        self.tenant = tenant
        # Cache: does plc_readings have a 'seq' column? It's NOT NULL in some
        # deployments. We'll detect and either set it or skip.
        cur = self.conn.cursor()
        cur.execute("""SELECT column_name FROM information_schema.columns
                       WHERE table_schema=%s AND table_name=%s""",
                    (SCHEMA, "plc_readings"))
        self.plc_cols = {r[0] for r in cur.fetchall()}

    @staticmethod
    def _connect():
        import psycopg
        return psycopg.connect(
            host=os.environ["TRUSTNODE_CLOUD_DB_HOST"],
            port=int(os.environ["TRUSTNODE_CLOUD_DB_PORT"]),
            user=os.environ["TRUSTNODE_CLOUD_DB_USER"],
            password=os.environ["TRUSTNODE_CLOUD_DB_PASSWORD"],
            dbname=os.environ.get("TRUSTNODE_CLOUD_DB_NAME", "postgres"),
            sslmode=os.environ.get("TRUSTNODE_CLOUD_DB_SSLMODE", "require"),
            connect_timeout=15,
        )

    def write_batch(self, now_utc: str, tick: int) -> int:
        cur = self.conn.cursor()
        # Build a row that matches whatever columns plc_readings actually has,
        # so we work on both legacy and new schemas.
        for tag in TAGS:
            v, q = generate_sample(tag, tick)
            cols = []
            vals = []
            mapping = {
                "ts_utc": now_utc, "tag_name": tag, "value": v,
                "quality": 192, "quality_label": q, "source": SOURCE,
                "gateway_id": GATEWAY_ID, "gateway_name": GATEWAY_NAME,
                "device_name": DEVICE_NAME, "plc_ip": PLC_IP,
                "database_name": DB_NAME, "tenant_id": self.tenant,
                "site": "", "area": "", "equipment": "",
                "seq": int(time.time() * 1_000_000) % 2_000_000_000,
                "raw_payload": "{}", "created_utc": now_utc,
                "local_id": None,
            }
            for k, val in mapping.items():
                if k in self.plc_cols:
                    cols.append(k); vals.append(val)
            placeholders = ", ".join(["%s"] * len(cols))
            col_list = ", ".join(f'"{c}"' for c in cols)
            cur.execute(
                f'INSERT INTO {SCHEMA}.plc_readings ({col_list}) VALUES ({placeholders})',
                vals,
            )
        # live_latest upsert. live_latest has a unique constraint on
        # (tenant_id, gateway_id, tag_name) on the cloud schema.
        for tag in TAGS:
            v, q = generate_sample(tag, tick)
            cur.execute(f'''
                INSERT INTO {SCHEMA}.live_latest
                  (tenant_id, gateway_id, gateway_name, device_name, plc_ip,
                   database_name, tag_name, value, quality, quality_label,
                   source, ts_utc, updated_utc)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tenant_id, gateway_id, tag_name) DO UPDATE SET
                  value=EXCLUDED.value, quality=EXCLUDED.quality,
                  quality_label=EXCLUDED.quality_label, source=EXCLUDED.source,
                  ts_utc=EXCLUDED.ts_utc, updated_utc=EXCLUDED.updated_utc,
                  gateway_name=EXCLUDED.gateway_name, device_name=EXCLUDED.device_name,
                  plc_ip=EXCLUDED.plc_ip, database_name=EXCLUDED.database_name
            ''', (
                self.tenant, GATEWAY_ID, GATEWAY_NAME, DEVICE_NAME, PLC_IP,
                DB_NAME, tag, v, 192, q, SOURCE, now_utc, now_utc,
            ))
        self.conn.commit()
        return len(TAGS)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def main() -> int:
    args = parse_args()
    load_env()

    print("─" * 70)
    print(" TrustNode synthetic historian injector")
    print(f"   duration : {args.seconds}s @ {args.rate_hz} Hz   tenant={args.tenant}")
    print(f"   targets  : local={not args.no_local}  cloud={not args.no_cloud}")
    print(f"   tags     : {', '.join(TAGS)}  (gateway_id={GATEWAY_ID})")
    print("─" * 70)

    local = None
    cloud = None
    try:
        if not args.no_local:
            local = LocalWriter(args.tenant)
            print(f"  local SQLite OPEN   {LOCAL_DB}")
        if not args.no_cloud:
            cloud = CloudWriter(args.tenant)
            print(f"  cloud Supabase OPEN {os.environ.get('TRUSTNODE_CLOUD_DB_HOST')}")
    except Exception as exc:
        print(f"  !! init failed: {exc}")
        return 1

    period = 1.0 / max(0.1, args.rate_hz)
    end_t = time.monotonic() + args.seconds
    tick = 0
    local_rows = 0
    cloud_rows = 0
    started_at = time.monotonic()

    while time.monotonic() < end_t:
        # Use a fresh UTC ts per tick. Format matches the historian's "%Y-%m-%d %H:%M:%S.%f+00".
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "+00"
        try:
            if local:
                local_rows += local.write_batch(now_utc, tick)
        except Exception as exc:
            print(f"  !! local write failed at tick {tick}: {exc}")
            return 2
        try:
            if cloud:
                cloud_rows += cloud.write_batch(now_utc, tick)
        except Exception as exc:
            print(f"  !! cloud write failed at tick {tick}: {exc}")
            return 3
        if tick % 5 == 0:
            elapsed = time.monotonic() - started_at
            print(f"  tick={tick:3d}  elapsed={elapsed:5.1f}s   local_rows={local_rows:>4d}  cloud_rows={cloud_rows:>4d}")
        tick += 1
        # Sleep the remainder of this tick.
        next_tick = started_at + tick * period
        sleep_for = next_tick - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)

    if local:
        local.close()
    if cloud:
        cloud.close()

    print("─" * 70)
    print(f"  DONE. wrote local_rows={local_rows}  cloud_rows={cloud_rows}  over {args.seconds}s")
    print(f"  gateway_id={GATEWAY_ID}  tags={TAGS}")
    print("─" * 70)
    return 0


if __name__ == "__main__":
    _sys.exit(main())
