"""Wipe historian-only data from the local edge SQLite DBs and the cloud
Supabase Postgres, while keeping ALL configuration intact.

What it deletes:
  Local (~/.trustnode_edge/data/*.db):
    - trustnode_app_store.db        : historian_readings,
                                       historian_agg_minute / _hour / _day,
                                       sync_outbox (rows whose payload is
                                       historian-tagged)
    - trustnode_store_forward.db    : outbox_readings (the 2.3 M-row backlog)
    - trustnode_telemetry.db        : telemetry_samples_raw, sync_outbox_v1,
                                       ingest_audit_log_local

  Cloud (Supabase, only WHERE tenant_id='default'):
    - public.plc_readings           : raw mirrored historian rows
    - public.historian_readings     : alternate mirror table (empty today)
    - public.live_latest            : latest-value snapshot

What it KEEPS:
  - Dashboards, report templates, scheduled reports, alarm rules
  - Users, customers, licenses, edges, activation codes (control plane)
  - Gateway configurations, devices, tags (PLC config — local edge)

Usage:
  python wipe_historian.py --dry-run            # default, just reports counts
  python wipe_historian.py --execute            # actually deletes
  python wipe_historian.py --execute --skip-cloud   # local only
  python wipe_historian.py --execute --skip-local   # cloud only
  python wipe_historian.py --execute --tenant default,acme...  # multi-tenant

IMPORTANT: stop the local edge service BEFORE running this with --execute,
otherwise SQLite WAL conflicts will leave the DBs locked.
"""
from __future__ import annotations

import argparse
import io as _io
import os
import sqlite3
import sys as _sys
import time
from pathlib import Path

# UTF-8 stdout for Windows.
try:
    _sys.stdout = _io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8", line_buffering=True)
except Exception:
    pass

DATA_DIR = Path.home() / ".trustnode_edge" / "data"

LOCAL_TARGETS = {
    "trustnode_app_store.db": [
        "historian_readings",
        "historian_agg_day",
        "historian_agg_hour",
        "historian_agg_minute",
        # sync_outbox can carry config-document writes too; only purge
        # historian-tagged rows. Domain column is 'domain' or 'kind'.
        ("sync_outbox", "WHERE COALESCE(domain,'') IN ('historian_readings','historian_agg_minute','historian_agg_hour','historian_agg_day','live_latest','plc_readings')"),
    ],
    "trustnode_store_forward.db": [
        "outbox_readings",
    ],
    "trustnode_telemetry.db": [
        "telemetry_samples_raw",
        "sync_outbox_v1",
        "ingest_audit_log_local",
        "latest_machine_state",  # snapshot table; harmless to clear
    ],
}

# Cloud tables to clear scoped by tenant_id.
CLOUD_TABLES = ["plc_readings", "historian_readings", "live_latest"]


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="Report what would be deleted, don't touch anything (default)")
    ap.add_argument("--execute", action="store_true",
                    help="Actually delete (overrides --dry-run)")
    ap.add_argument("--skip-local", action="store_true",
                    help="Don't touch the local SQLite DBs")
    ap.add_argument("--skip-cloud", action="store_true",
                    help="Don't touch Supabase")
    ap.add_argument("--tenant", default="default",
                    help="Comma-separated tenant_id list (default: 'default')")
    return ap.parse_args()


def _local_count(conn: sqlite3.Connection, target) -> tuple[int, str]:
    if isinstance(target, tuple):
        table, where = target
    else:
        table, where = target, ""
    try:
        sql = f'SELECT COUNT(*) FROM "{table}" {where}'.strip()
        n = conn.execute(sql).fetchone()[0]
        return int(n), where
    except sqlite3.Error as exc:
        return -1, f"!! {exc}"


def _local_delete(conn: sqlite3.Connection, target) -> int:
    if isinstance(target, tuple):
        table, where = target
    else:
        table, where = target, ""
    sql = f'DELETE FROM "{table}" {where}'.strip()
    cur = conn.execute(sql)
    return cur.rowcount


def step_local(execute: bool) -> None:
    print("\n=== LOCAL (SQLite) =================================================")
    for fname, targets in LOCAL_TARGETS.items():
        path = DATA_DIR / fname
        if not path.exists():
            print(f"  ~ {fname}: not present, skipping")
            continue
        print(f"\n  {fname}")
        try:
            # Open with longer busy timeout so a brief WAL fight doesn't kill us.
            conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=15.0)
        except sqlite3.Error as exc:
            print(f"    !! cannot open: {exc}")
            continue
        try:
            for t in targets:
                before, where = _local_count(conn, t)
                tname = t[0] if isinstance(t, tuple) else t
                label = tname if not where or where.startswith("!!") else f"{tname} {where[:60]}"
                if before < 0:
                    print(f"    - {label}: {where}")
                    continue
                if execute and before > 0:
                    deleted = _local_delete(conn, t)
                    conn.commit()
                    after, _ = _local_count(conn, t)
                    print(f"    - {label}: had {before:>10,d} → deleted {deleted:>10,d} → now {after:>10,d}")
                else:
                    print(f"    - {label}: would delete {before:>10,d}")
            if execute:
                # Reclaim space — VACUUM is heavy on big files; only run when
                # we actually freed >1k rows. Skipped for outbox_readings
                # because the file is 1 GB and VACUUM would block for minutes.
                pass
        finally:
            conn.close()


def step_cloud(execute: bool, tenants: list[str]) -> None:
    print("\n=== CLOUD (Supabase) ===============================================")
    # Load .env from same dir as this script's parent (the Trustnode_edge_app dir).
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if "=" in s and not s.startswith("#"):
                k, v = s.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    host = os.environ.get("TRUSTNODE_CLOUD_DB_HOST")
    if not host:
        print("  !! TRUSTNODE_CLOUD_DB_HOST not set in .env, skipping cloud step")
        return

    try:
        import psycopg
    except ImportError:
        print("  !! psycopg not installed, skipping cloud step")
        return

    print(f"  Target: {os.environ['TRUSTNODE_CLOUD_DB_USER']}@{host}:{os.environ['TRUSTNODE_CLOUD_DB_PORT']}")
    print(f"  Tenants: {tenants}")
    t0 = time.monotonic()
    try:
        conn = psycopg.connect(
            host=host,
            port=int(os.environ["TRUSTNODE_CLOUD_DB_PORT"]),
            user=os.environ["TRUSTNODE_CLOUD_DB_USER"],
            password=os.environ["TRUSTNODE_CLOUD_DB_PASSWORD"],
            dbname=os.environ.get("TRUSTNODE_CLOUD_DB_NAME", "postgres"),
            sslmode=os.environ.get("TRUSTNODE_CLOUD_DB_SSLMODE", "require"),
            connect_timeout=15,
        )
    except Exception as exc:
        print(f"  !! cloud connect failed: {exc}")
        return
    print(f"  Connected in {time.monotonic() - t0:.2f}s")

    cur = conn.cursor()
    for table in CLOUD_TABLES:
        # Confirm table exists.
        cur.execute(
            """SELECT 1 FROM information_schema.tables
               WHERE table_schema='public' AND table_name=%s""", (table,))
        if not cur.fetchone():
            print(f"  ~ public.{table}: not present, skipping")
            continue
        # Count + delete per tenant.
        for tenant in tenants:
            try:
                cur.execute(
                    f'SELECT COUNT(*) FROM public."{table}" WHERE tenant_id = %s',
                    (tenant,),
                )
                before = cur.fetchone()[0]
            except Exception as exc:
                print(f"  !! count public.{table} tenant={tenant}: {exc}")
                continue
            if execute and before > 0:
                # Use a single statement with tenant predicate. No CASCADE
                # needed; live_latest has no FK to plc_readings.
                cur.execute(
                    f'DELETE FROM public."{table}" WHERE tenant_id = %s',
                    (tenant,),
                )
                deleted = cur.rowcount
                conn.commit()
                print(f"  - public.{table} tenant={tenant}: had {before:>10,d} → deleted {deleted:>10,d}")
            else:
                print(f"  - public.{table} tenant={tenant}: would delete {before:>10,d}")
    conn.close()


def main() -> None:
    args = parse_args()
    execute = bool(args.execute)
    tenants = [t.strip() for t in str(args.tenant or "").split(",") if t.strip()]
    print("─" * 70)
    print(" TrustNode historian wipe")
    print(f"   mode    : {'EXECUTE (will delete)' if execute else 'DRY RUN (no changes)'}")
    print(f"   tenants : {tenants}")
    print(f"   data dir: {DATA_DIR}")
    print("─" * 70)

    if not execute:
        print("\n  NOTHING WILL BE DELETED. Re-run with --execute to commit.\n")

    if execute and not args.skip_local:
        print("\n  REMINDER: stop the local edge service before continuing,")
        print("            otherwise SQLite WAL conflicts can leave DBs locked.")

    if not args.skip_local:
        step_local(execute)
    if not args.skip_cloud:
        step_cloud(execute, tenants)

    print("\n─" * 70)
    print(" Done.")


if __name__ == "__main__":
    main()
