"""Live diagnostic for gateway Start/Stop responsiveness.

For each configured gateway (PLC + power meter), this script:
  1. Reads the current status (running or stopped).
  2. Starts it (if stopped) — times the HTTP call + when the runtime
     status first reports running=true + when the first historian write
     lands + when the cloud-mirror first reports a write.
  3. Stops it — times the HTTP call + when running=false sticks.
  4. Reports whether the UI footer model (the same data the React app
     consumes) is internally consistent.

Reads the auth secret from the local SQLite to mint a JWT directly so
we don't need the operator's password.

Usage:
    python scripts/diag_gateway_control.py
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import requests


BACKEND_BASE = os.environ.get("TRUSTNODE_BACKEND", "http://127.0.0.1:8000")
HTTP_TIMEOUT_SECONDS = 25.0  # generous, gateway stop can take a few seconds

# Mirror app.auth._b64url_encode / create_access_token so the JWT we mint
# is byte-identical to one issued by /api/auth/login.
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def resolve_local_db() -> Path:
    base = os.environ.get("TRUSTNODE_DATA_DIR", "").strip()
    if base:
        return Path(base) / "trustnode_app_store.db"
    return Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db"


def mint_admin_jwt() -> str:
    db_path = resolve_local_db()
    if not db_path.is_file():
        raise SystemExit(f"local DB not found at {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    secret = conn.execute("SELECT secret FROM auth_settings WHERE id=1").fetchone()[0]
    # Find an admin to impersonate. The edge stores users in `cp_users`
    # (control-plane table — also used by the local login path).
    user_row = conn.execute(
        "SELECT username, role FROM cp_users WHERE status='active' "
        "AND LOWER(role) IN ('admin','super') ORDER BY username LIMIT 1"
    ).fetchone()
    if not user_row:
        # Last-ditch fallback: any active user
        user_row = conn.execute(
            "SELECT username, role FROM cp_users WHERE status='active' ORDER BY username LIMIT 1"
        ).fetchone()
    conn.close()
    if not user_row:
        raise SystemExit("no enabled user in app_users — can't mint token")
    username, role = str(user_row[0]), str(user_row[1] or "admin")
    now = int(time.time())
    payload = {
        "sub": username,
        "role": role,
        "permissions": {},
        "modules": [],
        "tenant_id": "default",
        "iat": now,
        "exp": now + 3600,
    }
    header = {"alg": "HS256", "typ": "JWT"}
    p1 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p2 = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(str(secret).encode("utf-8"), f"{p1}.{p2}".encode("utf-8"), hashlib.sha256).digest()
    print(f"   minted token for user={username!r} role={role!r}")
    return f"{p1}.{p2}.{_b64url(sig)}"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
class API:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def get(self, path: str) -> tuple[int, dict | list | None, float]:
        t0 = time.perf_counter()
        r = requests.get(f"{self.base}{path}", headers=self.h, timeout=HTTP_TIMEOUT_SECONDS)
        dt = time.perf_counter() - t0
        try:
            data = r.json()
        except Exception:
            data = None
        return r.status_code, data, dt

    def post(self, path: str, body: dict | None = None) -> tuple[int, dict | list | None, float]:
        t0 = time.perf_counter()
        r = requests.post(
            f"{self.base}{path}", headers=self.h,
            data=json.dumps(body or {}), timeout=HTTP_TIMEOUT_SECONDS,
        )
        dt = time.perf_counter() - t0
        try:
            data = r.json()
        except Exception:
            data = None
        return r.status_code, data, dt


# ---------------------------------------------------------------------------
# Helpers to read live state
# ---------------------------------------------------------------------------
def fetch_plc_statuses(api: API) -> list[dict]:
    code, data, _ = api.get("/api/plc/gateways/status")
    if code != 200 or not isinstance(data, list):
        return []
    return data


def find_status(plc_rows: list[dict], gid: str) -> dict | None:
    for r in plc_rows:
        if str(r.get("gateway_id") or "") == gid:
            return r
    return None


def fetch_power_status(api: API) -> dict:
    code, data, _ = api.get("/api/power/status")
    if code != 200 or not isinstance(data, dict):
        return {}
    status = data.get("status") if isinstance(data.get("status"), dict) else data
    # Normalize: the endpoint returns `devices: [list]`. Build a {id: row} map
    # so callers can look up by device_id cheaply.
    dev_list = status.get("devices") or []
    if isinstance(dev_list, list):
        dev_map = {str(d.get("device_id") or ""): d for d in dev_list}
        status = dict(status)
        status["devices"] = dev_map
    return status


def fetch_power_config(api: API) -> dict:
    code, data, _ = api.get("/api/power/config")
    if code != 200 or not isinstance(data, dict):
        return {}
    # The endpoint wraps the actual config in {"ok": true, "config": {...}}.
    return data.get("config") if isinstance(data.get("config"), dict) else data


def count_recent_historian(api: API, gateway_id: str, since_iso: str) -> int:
    """Count historian rows for a gateway with ts_utc >= since_iso. Goes
    through the same endpoint the dashboard uses so we measure user-visible
    state, not just DB internals."""
    # We can only easily filter by gateway in /api/app-store/historian; it
    # returns the latest N rows. Walk them and count by ts.
    code, data, _ = api.get(f"/api/app-store/historian?limit=500&gateway={gateway_id}")
    if code != 200 or not isinstance(data, dict):
        return 0
    rows = data.get("rows") or []
    n = 0
    for r in rows:
        ts = str(r.get("ts") or "")
        if ts >= since_iso:
            n += 1
    return n


# ---------------------------------------------------------------------------
# Diagnostics per gateway
# ---------------------------------------------------------------------------
def now_iso_utc() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def wait_until(predicate, *, timeout: float, poll: float = 0.25) -> tuple[bool, float]:
    """Poll `predicate()` until it returns truthy or timeout elapses.
    Returns (success, elapsed_seconds)."""
    t0 = time.perf_counter()
    while True:
        try:
            ok = predicate()
        except Exception:
            ok = False
        if ok:
            return True, time.perf_counter() - t0
        if time.perf_counter() - t0 >= timeout:
            return False, time.perf_counter() - t0
        time.sleep(poll)


def diag_plc_gateway(api: API, gateway_cfg: dict) -> None:
    gid = str(gateway_cfg.get("id") or "")
    gname = str(gateway_cfg.get("name") or gid)
    print(f"\n=== PLC gateway: {gname} (id={gid}) ===")

    # Read current state
    statuses = fetch_plc_statuses(api)
    st = find_status(statuses, gid) or {}
    was_running = bool(st.get("running"))
    print(f"   initial: running={was_running}  last_check={st.get('last_check_utc','-')}"
          f"  db_last_write={st.get('db_last_write_utc','-')}")

    # If running, stop first, then start to time the start path on a clean slot.
    if was_running:
        print("   stopping (to reset before start test)…")
        code, body, dt = api.post("/api/plc/gateways/stop", {"gateway_id": gid})
        print(f"     stop HTTP  {code} in {dt*1000:.0f} ms  body={body}")
        ok, elapsed = wait_until(
            lambda: not (find_status(fetch_plc_statuses(api), gid) or {}).get("running"),
            timeout=15.0,
        )
        print(f"     running=false confirmed: {ok}  observed after {elapsed*1000:.0f} ms")

    # Build the start payload the same way the frontend does
    db_by_id = globals().get("_DB_BY_ID") or {}
    db = db_by_id.get(str(gateway_cfg.get("database_id") or ""))
    if not db:
        print(f"   SKIP start — gateway has no database_id or DB not found")
        return

    def _to_sink(conn: dict) -> dict:
        return {
            "id": conn.get("id") or "", "name": conn.get("name") or "",
            "engine": conn.get("engine"),
            "host": conn.get("host") or "", "port": int(conn.get("port") or 0),
            "database": conn.get("database") or "",
            "username": conn.get("username") or "", "password": conn.get("password") or "",
            "sqlite_path": conn.get("sqlite_path") or "", "file_path": conn.get("file_path") or "",
            "legacy_url": conn.get("legacy_url") or "", "legacy_api_token": conn.get("legacy_api_token") or "",
            "source": conn.get("source") or "", "site": conn.get("site") or "",
            "area": conn.get("area") or "", "equipment": conn.get("equipment") or "",
            "schema": conn.get("schema") or "public",
            "table": conn.get("table") or "plc_readings",
            "tls": bool(conn.get("tls")),
        }

    primary_sink = _to_sink(db)
    parallel_sinks = [
        _to_sink(c) for c in db_by_id.values()
        if c.get("id") != db.get("id")
        and c.get("enabled") is not False and c.get("use_gateway") is not False
        and str(c.get("engine") or "").lower() in ("csv_file", "txt_file", "sqlite")
    ]
    payload = {
        "gateway_id": gid,
        "config": {
            "gateway_type": gateway_cfg.get("gateway_type"),
            "plc_ip": gateway_cfg.get("plc_ip") or "",
            "opc_url": gateway_cfg.get("opc_url") or "",
            "tags": gateway_cfg.get("tags") or [],
            "collection_trigger_mode": "any",
            "collection_triggers": [],
            "interval_ms": int(gateway_cfg.get("interval_ms") or 1000),
            "equipment": db.get("equipment") or "",
            "site": db.get("site") or "",
            "area": db.get("area") or "",
        },
        "db_sink": primary_sink,
        "db_sinks": [primary_sink, *parallel_sinks],
    }

    # Start
    before_iso = now_iso_utc()
    print("   starting…")
    code, body, dt = api.post("/api/plc/gateways/start", payload)
    print(f"     start HTTP {code} in {dt*1000:.0f} ms  started={isinstance(body,dict) and body.get('started')}")
    if not (isinstance(body, dict) and body.get("started")):
        print(f"     ERROR body: {body}")
        return

    # When does running=true land in the status endpoint?
    ok, t_running_visible = wait_until(
        lambda: (find_status(fetch_plc_statuses(api), gid) or {}).get("running") is True,
        timeout=10.0,
    )
    print(f"     running=true visible: {ok}  after {t_running_visible*1000:.0f} ms")

    # When does the first new historian row appear?
    ok, t_first_write = wait_until(
        lambda: count_recent_historian(api, gid, before_iso) >= 1,
        timeout=15.0,
    )
    if ok:
        print(f"     first historian write: {t_first_write*1000:.0f} ms after start")
    else:
        print(f"     NO historian writes in 15 s (expected ~{int(payload.get('interval_ms',1000))} ms)")

    # Cloud mirror — does db_last_write_utc include a 'cloud' write soon after?
    st = find_status(fetch_plc_statuses(api), gid) or {}
    print(f"     db_write_count={st.get('db_write_count')}"
          f"  db_pending_count={st.get('db_pending_count')}"
          f"  collection_blocked={st.get('collection_blocked')}")

    # Stop again and time it
    print("   stopping…")
    code, body, dt = api.post("/api/plc/gateways/stop", {"gateway_id": gid})
    print(f"     stop HTTP  {code} in {dt*1000:.0f} ms  body={body}")
    ok, elapsed = wait_until(
        lambda: not (find_status(fetch_plc_statuses(api), gid) or {}).get("running"),
        timeout=15.0,
    )
    print(f"     running=false visible: {ok}  after {elapsed*1000:.0f} ms")

    # UI-model consistency check
    final = find_status(fetch_plc_statuses(api), gid) or {}
    inconsistent = []
    if final.get("running") and not final.get("db_last_write_utc"):
        inconsistent.append("running=true but no db_last_write_utc")
    if final.get("running") is False and final.get("collection_blocked"):
        inconsistent.append("stopped but collection_blocked still set")
    if inconsistent:
        print(f"     ⚠ UI-model inconsistencies: {inconsistent}")
    else:
        print(f"     final status clean: running={final.get('running')}")

    # Restore original state if it was running
    if was_running:
        print("   restoring original running state…")
        code, body, dt = api.post("/api/plc/gateways/start", payload)
        print(f"     restore HTTP {code} in {dt*1000:.0f} ms")


def diag_meter(api: API, device_cfg: dict) -> None:
    did = str(device_cfg.get("id") or "")
    dname = str(device_cfg.get("name") or did)
    print(f"\n=== Power meter: {dname} (id={did}) ===")

    # Read current state via power_manager status
    pstatus = fetch_power_status(api)
    st_per_dev = (pstatus.get("devices") or {})
    st = st_per_dev.get(did) or {}
    was_running = bool(st.get("enabled") or device_cfg.get("enabled"))
    print(f"   initial: enabled={was_running}  connected={st.get('connected')}"
          f"  last_check={st.get('last_check_utc','-')}")

    if was_running:
        print("   stopping (to reset before start test)…")
        code, body, dt = api.post(f"/api/power/devices/{did}/stop", {})
        print(f"     stop HTTP  {code} in {dt*1000:.0f} ms")
        ok, elapsed = wait_until(
            lambda: not (fetch_power_status(api).get("devices") or {}).get(did, {}).get("enabled"),
            timeout=10.0,
        )
        print(f"     enabled=false visible: {ok}  after {elapsed*1000:.0f} ms")

    # Start
    before_iso = now_iso_utc()
    print("   starting…")
    code, body, dt = api.post(f"/api/power/devices/{did}/start", {})
    print(f"     start HTTP {code} in {dt*1000:.0f} ms")
    if code != 200:
        print(f"     ERROR body: {body}")
        return

    ok, t_running_visible = wait_until(
        lambda: (fetch_power_status(api).get("devices") or {}).get(did, {}).get("enabled") is True,
        timeout=10.0,
    )
    print(f"     enabled=true visible: {ok}  after {t_running_visible*1000:.0f} ms")

    ok, t_first_write = wait_until(
        lambda: count_recent_historian(api, did, before_iso) >= 1,
        timeout=20.0,
    )
    if ok:
        print(f"     first historian write: {t_first_write*1000:.0f} ms after start")
    else:
        print(f"     NO historian writes in 20 s — meter may not be reachable or registers wrong")

    # Stop again
    print("   stopping…")
    code, body, dt = api.post(f"/api/power/devices/{did}/stop", {})
    print(f"     stop HTTP  {code} in {dt*1000:.0f} ms")
    ok, elapsed = wait_until(
        lambda: not (fetch_power_status(api).get("devices") or {}).get(did, {}).get("enabled"),
        timeout=10.0,
    )
    print(f"     enabled=false visible: {ok}  after {elapsed*1000:.0f} ms")

    # Restore
    if was_running:
        print("   restoring original running state…")
        code, body, dt = api.post(f"/api/power/devices/{did}/start", {})
        print(f"     restore HTTP {code} in {dt*1000:.0f} ms")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    print(f"== Gateway control diagnostic — backend {BACKEND_BASE} ==")
    code = requests.get(f"{BACKEND_BASE}/api/health", timeout=5).status_code
    print(f"   /api/health -> {code}")
    if code != 200:
        print("   backend not reachable; abort.")
        return 1
    token = mint_admin_jwt()
    api = API(BACKEND_BASE, token)

    # 1. Discover configured gateways. The interesting configs live in the
    # `config_documents_scoped` table keyed by `<tenant>|-|<edge_id>|<user>`.
    # The non-scoped `config_documents` only carries a sample/placeholder, so
    # going through /api/app-store/bootstrap returns nothing useful. We read
    # SQLite directly to pick the most recently updated scope.
    import sqlite3
    conn = sqlite3.connect(f"file:{resolve_local_db()}?mode=ro", uri=True, timeout=5)
    row = conn.execute(
        "SELECT scope_key, payload_json FROM config_documents_scoped "
        "WHERE domain='gateway_configurations' AND length(payload_json) > 10 "
        "ORDER BY updated_utc DESC LIMIT 1"
    ).fetchone()
    if not row:
        print("   no scoped gateway_configurations found — abort")
        return 2
    scope_key, gw_json = row[0], row[1]
    gateway_configs = json.loads(gw_json)
    db_row = conn.execute(
        "SELECT payload_json FROM config_documents_scoped "
        "WHERE scope_key=? AND domain='database_configurations'",
        (scope_key,),
    ).fetchone()
    if db_row:
        db_payload = json.loads(db_row[0])
        db_connections = db_payload if isinstance(db_payload, list) else (
            db_payload.get("connections") or db_payload.get("databases") or []
        )
    else:
        db_connections = []
    db_by_id = {str(c.get("id")): c for c in db_connections if c.get("id")}
    globals()["_DB_BY_ID"] = db_by_id
    print(f"\n-- Using scope: {scope_key} --")
    print(f"-- Found {len(gateway_configs)} PLC gateway config(s) | "
          f"{len(db_by_id)} DB connection(s) --")

    pcfg = fetch_power_config(api)
    meters = pcfg.get("devices") or []
    print(f"-- Found {len(meters)} power meter device(s) --")
    conn.close()

    # 2. Diagnose each
    for g in gateway_configs:
        if str(g.get("gateway_type") or "") == "modbus_tcp_meter":
            continue  # handled separately as a meter
        try:
            diag_plc_gateway(api, g)
        except Exception as exc:
            print(f"   EXC during diag for {g.get('id')}: {exc}")

    for d in meters:
        try:
            diag_meter(api, d)
        except Exception as exc:
            print(f"   EXC during diag for meter {d.get('id')}: {exc}")

    print("\n== diagnostic complete ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
