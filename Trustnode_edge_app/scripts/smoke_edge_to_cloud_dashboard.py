"""End-to-end smoke: PLC poll → local DB → cloud DB → cloud portal API.

Pragmatic pass criteria (in order; first failure short-circuits):

  1. Edge backend healthy           (GET http://127.0.0.1:8000/api/health)
  2. At least one gateway running   (GET /api/status, .gateways[*].running)
  3. Local historian grows in 30s   (sqlite COUNT(*) before/after)
  4. Cloud historian grows in 30s   (Postgres COUNT(*) before/after, tenant=default)
  5. Cloud /api/app-store/live OK   (returns rows for the caller's tenant)
  6. Newest cloud row younger than 30s (edge→cloud lag)

Run:
    python smoke_edge_to_cloud_dashboard.py
    python smoke_edge_to_cloud_dashboard.py --wait 60      # longer window
    python smoke_edge_to_cloud_dashboard.py --tenant default

Reads cloud credentials from Trustnode_edge_app/.env (gitignored).
"""
from __future__ import annotations

import argparse
import io as _io
import json
import os
import sqlite3
import sys as _sys
import time
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
LOCAL_API = "http://127.0.0.1:8000"
CLOUD_API = "https://trustnode.lsapps.app"
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=30,
                    help="Seconds between baseline and follow-up measurements")
    ap.add_argument("--tenant", default="default",
                    help="Cloud tenant_id to check (default: 'default')")
    ap.add_argument("--cloud-user", default="admin",
                    help="Cloud admin username for /api/auth/login")
    ap.add_argument("--cloud-pass", default="admin",
                    help="Cloud admin password")
    ap.add_argument("--edge-user", default="admin",
                    help="Local edge admin username for /api/auth/login")
    ap.add_argument("--edge-pass", default="admin",
                    help="Local edge admin password")
    ap.add_argument("--skip-gateway-check", action="store_true",
                    help="Don't require a running gateway (useful when "
                         "data is being injected synthetically for testing)")
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
# Tiny helpers (we deliberately avoid requests; urllib is in stdlib)
# ---------------------------------------------------------------------------
def http_json(url: str, method: str = "GET", body: dict | None = None,
              token: str | None = None, timeout: int = 15) -> tuple[int, dict | None]:
    import urllib.request, urllib.error
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(text) if text else None
            except json.JSONDecodeError:
                return resp.status, {"_raw": text}
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", errors="replace")
            return exc.code, json.loads(text) if text else None
        except Exception:
            return exc.code, None
    except Exception:
        return -1, None


def parse_iso_to_ms(text: str) -> float | None:
    if not text:
        return None
    raw = str(text).replace("Z", "+00:00")
    if " " in raw and "T" not in raw:
        raw = raw.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


# ---------------------------------------------------------------------------
# Individual checks. Each returns (ok: bool, label: str, detail: str).
# ---------------------------------------------------------------------------
def check_edge_health() -> tuple[bool, str, str]:
    status, body = http_json(f"{LOCAL_API}/api/health", timeout=5)
    if status == 200 and isinstance(body, dict):
        return True, "Edge /api/health 200", body.get("api_build", "?")
    return False, "Edge /api/health UNREACHABLE",  f"status={status}"


def edge_login(username: str, password: str) -> str | None:
    """Log into the local edge backend and return a Bearer token (or None)."""
    status, body = http_json(
        f"{LOCAL_API}/api/auth/login",
        method="POST", body={"username": username, "password": password},
    )
    if status == 200 and isinstance(body, dict):
        return body.get("token")
    return None


def check_edge_gateways_running(token: str | None) -> tuple[bool, str, str]:
    # /api/plc/gateways/status returns a list of per-gateway runtime rows on
    # this build. The PLC router prefix is /api/plc, not /api directly.
    status, body = http_json(f"{LOCAL_API}/api/plc/gateways/status", token=token, timeout=8)
    if status == 401:
        return False, "Edge gateway status returned 401", "edge admin login failed; pass --edge-user/--edge-pass"
    if status != 200 or not isinstance(body, list):
        return False, "Edge gateway status unavailable", f"status={status}"
    gws = body
    running = [g for g in gws if (g.get("running") is True
                                  or str(g.get("status") or "").lower() in ("running", "ok"))]
    if running:
        names = ", ".join(str(g.get("gateway_id") or g.get("name") or "?") for g in running[:3])
        more = f" (+{len(running) - 3} more)" if len(running) > 3 else ""
        return True, f"Edge: {len(running)}/{len(gws)} gateways running", names + more
    return False, "Edge: zero gateways running", f"{len(gws)} configured, none active"


def count_local_historian() -> int:
    if not LOCAL_DB.exists():
        return -1
    try:
        with sqlite3.connect(f"file:{LOCAL_DB}?mode=ro", uri=True, timeout=10) as c:
            return int(c.execute("SELECT COUNT(*) FROM historian_readings").fetchone()[0])
    except sqlite3.Error:
        return -2


def newest_local_ts() -> str:
    if not LOCAL_DB.exists():
        return ""
    try:
        with sqlite3.connect(f"file:{LOCAL_DB}?mode=ro", uri=True, timeout=10) as c:
            row = c.execute("SELECT MAX(ts_utc) FROM historian_readings").fetchone()
            return str(row[0] or "")
    except sqlite3.Error:
        return ""


def cloud_conn():
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


def count_cloud_plc_readings(tenant: str) -> tuple[int, str]:
    try:
        with cloud_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*), COALESCE(MAX(ts_utc)::text, '') "
                "FROM public.plc_readings WHERE tenant_id = %s",
                (tenant,),
            )
            n, mx = cur.fetchone()
            return int(n), str(mx or "")
    except Exception as exc:
        return -1, f"!! {exc}"


def check_cloud_live_api(tenant: str, username: str, password: str) -> tuple[bool, str, str]:
    status, body = http_json(
        f"{CLOUD_API}/api/auth/login",
        method="POST", body={"username": username, "password": password},
    )
    if status != 200 or not isinstance(body, dict) or not body.get("token"):
        return False, "Cloud login FAILED", f"status={status}"
    token = body["token"]

    status, body = http_json(
        f"{CLOUD_API}/api/app-store/live?limit=50",
        token=token, timeout=10,
    )
    if status != 200 or not isinstance(body, dict):
        return False, "/api/app-store/live FAILED", f"status={status}"
    rows = body.get("rows") or []
    rows_for_tenant = [r for r in rows if str(r.get("tenant_id") or "") in (tenant, "")]
    if not rows_for_tenant:
        return False, "/api/app-store/live empty for tenant", f"got {len(rows)} rows, none for tenant='{tenant}'"
    newest = rows_for_tenant[0]
    ts = str(newest.get("ts") or newest.get("ts_utc") or "")
    return True, f"/api/app-store/live OK ({len(rows_for_tenant)} rows)", f"newest ts={ts}"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def banner(text: str) -> None:
    print()
    print("=" * 78)
    print(f" {text}")
    print("=" * 78)


def line(label: str, ok: bool | None, detail: str = "") -> None:
    if ok is True:
        marker = "[ PASS ]"
    elif ok is False:
        marker = "[ FAIL ]"
    else:
        marker = "[ ---- ]"
    print(f"  {marker}  {label:<58s}{('  '+detail) if detail else ''}")


def main() -> int:
    args = parse_args()
    load_env()

    print()
    print("─" * 78)
    print("  TrustNode EDGE → CLOUD → DASHBOARD end-to-end smoke")
    print(f"  local edge  : {LOCAL_API}")
    print(f"  cloud portal: {CLOUD_API}")
    print(f"  cloud DB    : {os.environ.get('TRUSTNODE_CLOUD_DB_HOST', '(no .env)')}")
    print(f"  tenant      : {args.tenant}")
    print(f"  wait window : {args.wait}s")
    print("─" * 78)

    failures: list[str] = []

    banner("Step 1 — Edge backend reachable")
    ok, lbl, det = check_edge_health()
    line(lbl, ok, det)
    if not ok:
        failures.append(lbl)
        print("\n  Edge not reachable — stop here. Is the local edge service running?")
        return 1

    banner("Step 2 — At least one gateway running")
    if args.skip_gateway_check:
        line("Gateway check SKIPPED via --skip-gateway-check", None,
             "(synthetic-injection mode; not a real-PLC test)")
        edge_token = edge_login(args.edge_user, args.edge_pass)
        ok = True
    else:
        edge_token = edge_login(args.edge_user, args.edge_pass)
        if edge_token is None:
            line(f"Local edge login (user={args.edge_user})", False, "/api/auth/login did not return a token")
            failures.append("edge-login-failed")
            return 2
        ok, lbl, det = check_edge_gateways_running(edge_token)
        line(lbl, ok, det)
    if not ok:
        failures.append(lbl)
        print("\n  No gateway is polling. Start a gateway in the edge UI, then retry.")
        return 2

    banner(f"Step 3 — Local historian grows in {args.wait}s")
    local_before = count_local_historian()
    local_before_ts = newest_local_ts()
    line(f"local historian_readings count (T0)", None, f"{local_before:,} rows  newest={local_before_ts}")

    banner(f"Step 4 — Cloud plc_readings grows in {args.wait}s (tenant={args.tenant})")
    cloud_before, cloud_before_ts = count_cloud_plc_readings(args.tenant)
    if cloud_before < 0:
        line("cloud cloud DB unreachable", False, cloud_before_ts)
        failures.append("cloud-db-unreachable")
        return 3
    line(f"cloud plc_readings count (T0)", None, f"{cloud_before:,} rows  newest={cloud_before_ts}")

    print(f"\n  ...waiting {args.wait}s for fresh samples to land...")
    time.sleep(args.wait)

    local_after = count_local_historian()
    local_after_ts = newest_local_ts()
    cloud_after, cloud_after_ts = count_cloud_plc_readings(args.tenant)

    banner("Step 3 — Local historian delta")
    line(f"local historian_readings count (T1)", None, f"{local_after:,} rows  newest={local_after_ts}")
    delta_local = local_after - local_before
    grew_local = delta_local > 0
    line(f"local delta T1−T0 (must be > 0)", grew_local, f"+{delta_local:,} rows in {args.wait}s")
    if not grew_local:
        failures.append("local-historian-not-growing")

    banner("Step 4 — Cloud plc_readings delta")
    line(f"cloud plc_readings count (T1)", None, f"{cloud_after:,} rows  newest={cloud_after_ts}")
    delta_cloud = cloud_after - cloud_before
    grew_cloud = delta_cloud > 0
    line(f"cloud delta T1−T0 (must be > 0)", grew_cloud, f"+{delta_cloud:,} rows in {args.wait}s")
    if not grew_cloud:
        failures.append("cloud-plc_readings-not-growing")

    banner("Step 5 — Cloud /api/app-store/live serves rows for the dashboard")
    ok, lbl, det = check_cloud_live_api(args.tenant, args.cloud_user, args.cloud_pass)
    line(lbl, ok, det)
    if not ok:
        failures.append("cloud-live-api")

    banner("Step 6 — Edge → cloud lag (newest cloud row vs now)")
    now_ms = time.time() * 1000.0
    newest_ms = parse_iso_to_ms(cloud_after_ts)
    if newest_ms is None:
        line("cloud lag unknown (no parseable ts)", False, cloud_after_ts)
        failures.append("cloud-lag-unknown")
    else:
        lag_s = max(0.0, (now_ms - newest_ms) / 1000.0)
        ok_lag = lag_s <= 30.0
        line(f"cloud newest row is {lag_s:.1f}s old", ok_lag, "(< 30 s expected)")
        if not ok_lag:
            failures.append("cloud-lag-too-high")

    # Summary
    print()
    print("─" * 78)
    if not failures:
        print("  RESULT: PASS   end-to-end is healthy. Cloud dashboards will see live data.")
        print("─" * 78)
        return 0
    print(f"  RESULT: FAIL   {len(failures)} check(s) failed:")
    for f in failures:
        print(f"     - {f}")
    print("─" * 78)
    return 10 + len(failures)


if __name__ == "__main__":
    _sys.exit(main())
