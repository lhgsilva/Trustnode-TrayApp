"""Capture the full state of a local edge that 'should be working'.

Dumps everything an engineer needs to compare working-vs-broken without
guessing. Read-only against the running backend + local SQLite. No
permission prompts, no production writes.

What it captures (each section labeled so the output is greppable):

  ENV          — running backend PID, exe path, start time, env vars
  HEALTH       — /api/health
  AUTH         — login probe to confirm credentials
  CONFIG       — /api/plc/config (the LEGACY single-gateway)
                 — config_documents(gateway_configurations) unscoped
                 — config_documents_scoped(gateway_configurations)
  RUNTIME      — /api/plc/status, /api/plc/gateways/status,
                 the 'workers' dict via /api/plc/snapshot
  WIRE TEST    — Direct pycomm3 probe to PLC IP using the same args
                 the running backend uses
  RECENT       — last 10 writes per gateway in historian_readings
                 — sync_outbox depth, last sync state
                 — last 30 app_logs entries
  WIDGETS      — every dashboard widget per user/scope, with its
                 (gateway_id, tag_name, plc_endpoint)
  COMPARISON   — for each running gateway, list what its samples
                 actually carry (gateway_id, plc_ip) so we can see
                 if the chart filter would match

Usage from Trustnode_edge_app/:
    python scripts/diag_edge_state.py
"""
from __future__ import annotations
import io, json, os, sqlite3, sys, time
from pathlib import Path

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import requests

EDGE = "http://127.0.0.1:8000"


def section(label: str) -> None:
    print(f"\n{'='*4} {label} {'='*max(4, 70-len(label)-6)}")


def main() -> int:
    # --- 1. ENV / process
    section("ENV")
    try:
        import psutil
        # find pid on :8000
        pid = None
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == 8000 and c.status == psutil.CONN_LISTEN:
                pid = c.pid; break
        if pid:
            p = psutil.Process(pid)
            print(f"  PID:        {pid}")
            print(f"  exe:        {p.exe()}")
            print(f"  started:    {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(p.create_time()))}")
            envd = p.environ() or {}
            keys_of_interest = [
                "TRUSTNODE_DATA_DIR", "TRUSTNODE_APP_STORE_PATH",
                "TRUSTNODE_TENANT_ID", "TRUSTNODE_CLOUD_API_URL",
                "TRUSTNODE_PREFER_CLOUD_READS",
                "TRUSTNODE_CLOUD_BOOTSTRAP_USER",
            ]
            for k in keys_of_interest:
                v = envd.get(k, "")
                if k.endswith("PASSWORD") or k.endswith("KEY"):
                    v = f"[len {len(v)}]" if v else "(unset)"
                print(f"  env {k} = {v or '(unset)'}")
    except Exception as exc:
        print(f"  psutil error: {exc} (install with `pip install psutil` for full ENV)")

    # --- 2. health + auth
    section("HEALTH")
    try:
        r = requests.get(f"{EDGE}/api/health", timeout=5)
        print(f"  http={r.status_code} body={r.text[:200]}")
    except Exception as exc:
        print(f"  FAILED: {exc}")
        return 2

    section("AUTH")
    token = ""
    for pw in ("admin", "Apolo020@", os.environ.get("TRUSTNODE_EDGE_ADMIN_PASSWORD", "")):
        if not pw: continue
        try:
            r = requests.post(f"{EDGE}/api/auth/login",
                              json={"username": "admin", "password": pw},
                              timeout=8)
            if r.status_code == 200:
                token = r.json().get("token") or ""
                print(f"  login OK with password='{pw if pw == 'admin' else '***'}'")
                break
            else:
                print(f"  login http={r.status_code} with '{pw if pw == 'admin' else '***'}'")
        except Exception as exc:
            print(f"  login error: {exc}")
    if not token:
        print("  ABORT: no admin password worked")
        return 2
    H = {"Authorization": f"Bearer {token}"}

    # --- 3. CONFIG (legacy + scoped)
    section("CONFIG (legacy single-gateway)")
    r = requests.get(f"{EDGE}/api/plc/config", headers=H, timeout=8)
    cfg = r.json() if r.status_code == 200 else {}
    print(f"  gateway_type:   {cfg.get('gateway_type')}")
    print(f"  plc_ip:         {cfg.get('plc_ip')}")
    print(f"  opc_url:        {cfg.get('opc_url')}")
    print(f"  interval_ms:    {cfg.get('interval_ms')}")
    print(f"  tags ({len(cfg.get('tags') or [])}): {cfg.get('tags')}")
    print(f"  triggers:       {len(cfg.get('collection_triggers') or [])}")

    # local SQLite
    db_path = Path.home() / ".trustnode_edge" / "data" / "trustnode_app_store.db"
    if not db_path.is_file():
        print(f"\n  local SQLite NOT FOUND at {db_path}")
        db_path = None

    if db_path:
        section("CONFIG (local SQLite unscoped gateway_configurations)")
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            r = con.execute("SELECT version, updated_utc, payload_json FROM config_documents WHERE domain='gateway_configurations'").fetchone()
            if r:
                p = json.loads(r["payload_json"]) if isinstance(r["payload_json"], str) else r["payload_json"]
                print(f"  version={r['version']} updated={r['updated_utc']}")
                if isinstance(p, list):
                    for g in p:
                        print(f"    id={g.get('id'):25s} name={g.get('name'):20s} type={g.get('gateway_type'):16s} plc_ip={g.get('plc_ip')} opc_url={g.get('opc_url')} tags={len(g.get('tags') or [])}")
            else:
                print("  (no row)")

            section("CONFIG (local SQLite SCOPED gateway_configurations per user/edge)")
            for r in con.execute("SELECT scope_key, version, updated_utc, payload_json FROM config_documents_scoped WHERE domain='gateway_configurations'").fetchall():
                print(f"  scope={r['scope_key']!r} v={r['version']} updated={r['updated_utc']}")
                p = json.loads(r["payload_json"]) if isinstance(r["payload_json"], str) else r["payload_json"]
                if isinstance(p, list):
                    for g in p:
                        print(f"    id={g.get('id'):25s} name={g.get('name'):20s} type={g.get('gateway_type'):16s} plc_ip={g.get('plc_ip')} opc_url={g.get('opc_url')} tags={len(g.get('tags') or [])}")
                else:
                    print(f"    (payload not a list, {type(p).__name__})")
        finally:
            con.close()

    # --- 4. RUNTIME
    section("RUNTIME /api/plc/status (legacy)")
    r = requests.get(f"{EDGE}/api/plc/status", headers=H, timeout=8)
    st = r.json()
    for k in ("running", "gateway_type", "plc_ip", "db_write_count",
              "db_last_write_utc", "db_pending_count",
              "collection_blocked", "collection_block_reason"):
        print(f"  {k}: {st.get(k)}")
    print(f"  last_error: {st.get('last_error')}")

    section("RUNTIME /api/plc/gateways/status (multi-gateway workers)")
    r = requests.get(f"{EDGE}/api/plc/gateways/status", headers=H, timeout=8)
    rows = r.json() if r.status_code == 200 else []
    if isinstance(rows, list) and rows:
        for row in rows:
            print(f"  gw={row.get('gateway_id'):25s} running={row.get('running')} "
                  f"writes={row.get('db_write_count')} last_err={(row.get('last_error') or '')[:120]}")
    else:
        print(f"  EMPTY  ({len(rows) if isinstance(rows, list) else 'not a list'})")
        print("  → Multi-gateway workers dict is empty even though a gateway runs.")
        print("    Either: the gateway was started via the LEGACY route ('/api/plc/start')")
        print("    rather than the multi-gateway route ('/api/plc/gateways/start');")
        print("    OR the worker registry in plc_manager.workers is bypassed.")

    section("RUNTIME /api/plc/snapshot (live readings buffer)")
    r = requests.get(f"{EDGE}/api/plc/snapshot", headers=H, timeout=8)
    snap = r.json() if r.status_code == 200 else []
    if isinstance(snap, list):
        print(f"  rows: {len(snap)}")
        for row in snap[:5]:
            print(f"    {row}")

    # --- 5. WIRE TEST (replicate plc_manager exactly)
    if cfg.get("gateway_type") == "allen_bradley" and cfg.get("plc_ip"):
        section("WIRE TEST (pycomm3 with init_tags=True, init_program_tags=False)")
        try:
            from pycomm3 import LogixDriver
            ip = cfg["plc_ip"]
            for path in [ip, f"{ip}/0", f"{ip}/1"]:
                try:
                    plc = LogixDriver(path, init_tags=True, init_program_tags=False)
                    plc.open()
                    info = plc.info
                    known = sorted((plc.tags or {}).keys())
                    print(f"  path={path:30s} OPEN OK  info={info.get('name')} controller_tags={known}")
                    # Try reading the first 3 configured tags
                    tags_to_try = (cfg.get("tags") or [])[:3]
                    if tags_to_try:
                        results = plc.read(*tags_to_try)
                        if not isinstance(results, list): results = [results]
                        for tag, res in zip(tags_to_try, results):
                            print(f"      read('{tag}'): value={getattr(res,'value',None)!r} error={getattr(res,'error',None)}")
                    plc.close()
                    break  # don't try the next path if this one worked
                except Exception as exc:
                    print(f"  path={path:30s} FAIL: {type(exc).__name__}: {str(exc)[:160]}")
        except ImportError:
            print("  pycomm3 not importable in this Python; skipping wire test")

    # --- 6. RECENT (writes, sync, app_logs)
    if db_path:
        section("RECENT writes by gateway (last 60s)")
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            for r in con.execute(
                "SELECT gateway_id, plc_ip, count(*) AS rows, max(ts_utc) AS latest "
                "FROM historian_readings WHERE ts_utc > datetime('now','-60 seconds') "
                "GROUP BY gateway_id, plc_ip ORDER BY 3 DESC"
            ):
                print(f"  gw={r[0]:25s} ip={r[1]:25s} rows={r[2]:5d} latest={r[3]}")
            print()
            print("  (broader window: last 30 minutes)")
            for r in con.execute(
                "SELECT gateway_id, plc_ip, count(*) AS rows, max(ts_utc) AS latest "
                "FROM historian_readings WHERE ts_utc > datetime('now','-30 minutes') "
                "GROUP BY gateway_id, plc_ip ORDER BY 3 DESC"
            ):
                print(f"  gw={r[0]:25s} ip={r[1]:25s} rows={r[2]:5d} latest={r[3]}")

            section("RECENT app_logs (last 40 lines, level=error|warning|info)")
            for r in con.execute(
                "SELECT ts_utc, level, category, gateway_id, message "
                "FROM app_logs ORDER BY ts_utc DESC LIMIT 40"
            ):
                msg = str(r[4] or "")
                if len(msg) > 200: msg = msg[:200] + "..."
                print(f"  [{r[1]:7s}] {r[0]} cat={r[2] or '-':12s} gw={r[3] or '-':20s} {msg}")

            section("SYNC outbox depth + data_sync_state")
            for r in con.execute("SELECT status, count(*) FROM sync_outbox GROUP BY status"):
                print(f"  status={r[0]:15s} rows={r[1]}")
            for r in con.execute("SELECT * FROM data_sync_state"):
                print(f"  {dict(zip([d[0] for d in con.execute('PRAGMA table_info(data_sync_state)').fetchall()], r))}")
        finally:
            con.close()

    # --- 7. WIDGETS per scope
    if db_path:
        section("WIDGETS per dashboard scope (with their gateway_id/tag/plc_endpoint)")
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            for r in con.execute("SELECT scope_key, payload_json FROM config_documents_scoped WHERE domain='dashboard_configurations'"):
                p = json.loads(r[1]) if isinstance(r[1], str) else r[1]
                widgets = p.get("widgets") if isinstance(p, dict) else (p if isinstance(p, list) else [])
                print(f"  scope={r[0]!r}: {len(widgets)} widget(s)")
                for w in widgets:
                    cfg_w = w.get("config", {})
                    print(f"    type={w.get('type'):22s} gw={cfg_w.get('gateway_id'):25s} tag={cfg_w.get('tag_name'):30s} plc_endpoint={cfg_w.get('plc_endpoint')!r}")
        finally:
            con.close()

    # --- 8. Live /api/v1/latest (the WebSocket-equivalent)
    section("LIVE /api/v1/latest (most recent rows the backend has in memory)")
    r = requests.get(f"{EDGE}/api/v1/latest?limit=20", headers=H, timeout=8)
    print(f"  http={r.status_code}")
    try:
        rows = r.json().get("rows", [])
        print(f"  rows: {len(rows)}")
        for row in rows[:10]:
            print(f"    gw={row.get('gateway_id')} ip={row.get('plc_ip')} tag={row.get('tag_name')} value={row.get('value')} ts={row.get('ts_utc')}")
    except Exception:
        print(f"  body: {r.text[:300]}")

    print("\n" + "="*70)
    print("Done. Send this whole output back and we can pinpoint the exact gap.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
