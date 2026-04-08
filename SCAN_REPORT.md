# TrustNode Edge — Full System Scan Report
**Date:** 2026-04-08
**Scanned by:** 5 specialist agents (backend, frontend, database, security, performance)
**Coordinator:** TrustNode Master Coordinator

---

## Executive Summary

TrustNode Edge is a **well-conceived industrial IoT gateway** built with a solid architecture: FastAPI backend + React frontend + SQLite-local-first + optional cloud PostgreSQL sink. The store-and-forward pattern, historian with retention rollups, WebSocket streaming, and multi-protocol PLC support (Allen-Bradley, Siemens S7, OPC-UA) are all correctly designed at the architectural level.

The app was started from OpenAI-generated code and has been actively extended. The core data flow works. However, **five critical issues must be fixed before any production deployment**, and the PLC polling engine has architectural bottlenecks that will surface as plants grow beyond a handful of tags.

**Overall health:** Functional for small-scale testing. Not production-ready without addressing the Critical and High items below.

---

## How the App Was Built

### Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  LOCAL EDGE DEVICE (factory floor)                              │
│                                                                  │
│  PLCs ──[pycomm3/snap7/opcua]──► GatewayWorker asyncio tasks   │
│                                         │                        │
│                                         ▼                        │
│                                  _broadcast()                    │
│                                  ├─► SQLite historian_readings   │
│                                  └─► asyncio.Queue (per WS sub) │
│                                                │                 │
│  FastAPI (Uvicorn, port 8000)                  │                 │
│  ├─ /ws/stream ◄─────────────────────────────-┘                 │
│  ├─ /ws/cloud-stream (1Hz full snapshot)                         │
│  ├─ /api/plc/*  (gateway start/stop/config/discovery)           │
│  ├─ /api/app-store/* (historian, config, retention, backups)     │
│  ├─ /api/database/* (connection test, provision, sink switch)    │
│  ├─ /api/auth/* (login, me)                                      │
│  └─ /api/notifications/* (email/SMTP)                           │
│                                                                  │
│  SQLite app_store.db                                             │
│  ├─ config_documents (all config domains as JSON)               │
│  ├─ historian_readings (PLC time-series, local)                  │
│  ├─ app_logs                                                     │
│  ├─ sync_outbox (pending cloud pushes)                           │
│  └─ auth_settings (JWT secret)                                   │
│                                                                  │
│  React SPA (Vite, port 5173 dev / static in production)         │
│  └─ App.jsx (11,002 lines, single component, no router)         │
│     ├─ Local mode: WS /ws/stream → live charts                  │
│     └─ Cloud mode: WS /ws/cloud-stream + HTTP polling           │
└──────────────────────────────────────────────────────────────────┘
          │ cloud sync (background thread, 1-second cadence)
          ▼
┌─────────────────────────────────────┐
│  CLOUD DATABASE (PostgreSQL/Supabase│
│  ├─ historian_readings              │
│  ├─ plc_readings (legacy compat)    │
│  ├─ live_latest (keyed by tag)      │
│  └─ app_logs                        │
└─────────────────────────────────────┘
          │ read-only cloud dashboard
          ▼
┌─────────────────────────────────────┐
│  web_cloud_readonly/ (static site)  │
│  Same React bundle, VITE_READONLY=  │
│  true, forced cloud URL baked in    │
└─────────────────────────────────────┘
```

### Technology Decisions (as-built)

| Layer | Choice | Notes |
|-------|--------|-------|
| Backend | FastAPI + Uvicorn (Python 3.11) | Async ASGI; Pydantic v2 models |
| PLC (Allen-Bradley) | pycomm3 primary, pylogix fallback | pycomm3 batch-reads tags in one CIP packet; fallback reads one tag at a time |
| PLC (Siemens) | python-snap7 2.0.2 | New client per poll cycle — expensive |
| PLC (Generic) | opcua 0.98.13 (legacy, unmaintained) | New client per poll + one node per request |
| Local DB | SQLite via sqlite3 module | No WAL mode; no connection pool |
| Cloud DB | SQLAlchemy + psycopg | New engine per sync cycle |
| Multi-sink | PostgreSQL, MySQL, MSSQL, InfluxDB, CSV, HTTP | Outbox pattern for reliability |
| Auth | Hand-rolled HMAC-SHA256 JWT | Correct implementation; no RBAC enforcement |
| Frontend | React 18 + Vite 5, zero dependencies except Recharts | No router, no state library, 11k-line monolith |
| Real-time | WebSocket per subscriber, bounded queue (200), head-drop | Sound design |
| Cloud deploy | Nginx + systemd on VPS, GitHub Actions CI/CD | SSH deploy via appleboy actions |
| Desktop | Electron tray + pystray legacy wrapper | Both exist in parallel |

### What Works Well (confirmed by all agents)

- Store-and-forward outbox: PLC data survives network outages to cloud
- `executemany` batch historian inserts: efficient, single-transaction per batch
- Retention rollup cascade (raw → minute → hour → day) before deletion
- Bounded WebSocket queues with oldest-drop backpressure
- PBKDF2-SHA256 with 120k iterations; `hmac.compare_digest` everywhere
- Cloud schema migration via `ALTER TABLE ADD COLUMN IF NOT EXISTS`
- `ON CONFLICT(local_id) DO NOTHING` idempotent cloud upserts
- Ref-mirror pattern in React for stale-closure safety in WebSocket handlers
- Alarm deduplication gate (5-second `logDedupeRef`)
- `isForcedReadonlyCloudMode()` consistently applied across all permission gates
- `_safe_name()` regex protects schema/table DDL from injection

---

## Critical Issues — Fix Before Any Production Deployment

### C1: Hardcoded `admin/admin` default credential
**File:** `backend/app/routers/auth.py:22-31`
**Issue:** When no users are configured (first boot or after factory reset), login falls back to username `admin`, password `admin` stored as plaintext. `verify_password()` matches it directly.
**Attack path:** `POST /api/auth/login {"username":"admin","password":"admin"}` → 12-hour admin JWT → full control over gateways, config, historian.
**Fix:** Remove the fallback entirely. Return HTTP 503 "Setup required" if no users are provisioned.

---

### C2: ALL GET requests bypass authentication
**File:** `backend/app/main.py:49-50`
**Issue:** `auth_middleware` skips all `GET` and `HEAD` requests unconditionally. Every read endpoint is unauthenticated.
**Exposed endpoints (no token needed):**
- `GET /api/app-store/bootstrap` → returns **all usernames and PBKDF2 password hashes**
- `GET /api/plc/config` → full PLC IPs, tag lists, OT network topology
- `GET /api/app-store/historian` → all production time-series data
- `GET /api/app-store/live` → live PLC readings
- `GET /api/database/active-sink` → DB host, port, credentials
- `GET /api/plc/status`, `GET /api/plc/snapshot`

**Fix:** Remove the GET bypass. Use FastAPI `Depends()` per router. Exempt only: `GET /api/health`, `POST /api/auth/login`, `GET /api/auth/me`.

---

### C3: No role-based access control enforcement
**File:** `backend/app/main.py` + all routers
**Issue:** JWT payload carries `role` and `permissions` fields but **nothing in the backend reads them**. Any valid token (including "viewer" role) can call:
- `PUT /api/app-store/bootstrap` — rewrite entire config including users
- `POST /api/plc/gateways/start` — start new gateway pointed at any IP on OT network
- `POST /api/app-store/backups/restore` — overwrite live database
- `POST /api/app-store/cleanup-data` — delete historian data

**Fix:** Implement `require_admin` and `require_operator` dependency functions; apply per-endpoint.

---

### C4: CORS wildcard — `allow_origins=["*"]` hardcoded
**File:** `backend/app/main.py:22-28`
**Issue:** `settings.cors_origins` is correctly computed in `config.py` from `TRUSTNODE_CORS_ORIGINS` env var but is **never used**. The app is hardcoded to accept requests from any origin.
**Fix:** One line change: replace `allow_origins=["*"]` with `allow_origins=settings.cors_origins` in `CORSMiddleware`.

---

### C5: `cleanup_data` deletes recent data instead of old data
**File:** `backend/app/services/app_store.py:2517`
**Issue:** The manual cleanup endpoint uses `DELETE FROM historian_readings WHERE ts_utc >= cutoff` where `cutoff = now - timedelta(...)`. This deletes everything **newer** than the cutoff — i.e., the most recent data — and keeps old data. The operator-facing "clean up last day" button destroys the last 24 hours of production readings.
**Fix:** Change `>= cutoff_text` to `< cutoff_text` at lines 2517–2521. One character.

---

## High Priority Issues

### H1: PLC reads block the entire asyncio event loop
**File:** `backend/app/services/plc_manager.py:112`
**Issue:** `_read_from_gateway()` calls pycomm3/snap7/opcua blocking network I/O directly from an `asyncio.Task`, with no `run_in_executor`. A 100ms PLC round-trip stalls all WebSocket sends, HTTP responses, and background tasks for that entire duration. A hung PLC (e.g., snap7 default TCP timeout = 30 seconds) freezes the entire application.
**Fix:** Wrap `_read_from_gateway()` with `loop.run_in_executor(executor, self._read_from_gateway)`. Create a `ThreadPoolExecutor(max_workers=8)` in `PLCManager.__init__`.

---

### H2: Snap7 and OPC-UA reconnect on every single poll cycle
**Files:** `plc_manager.py:450-508` (snap7), `plc_manager.py:511-578` (opcua)
**Issue:** Both protocols create a new client instance, call `connect()`, read all tags, and call `disconnect()` on every poll. S7 TCP session setup takes 10-50ms. OPC-UA handshake takes 100-500ms. This alone violates the <100ms poll SLA.
**Fix:** Persist the client in `GatewayWorker` state; reconnect only when `get_connected()` returns False. For OPC-UA, migrate to `asyncua` and use MonitoredItem subscriptions instead of polling.

---

### H3: pycomm3 opens a new `LogixDriver` with `init_tags=True` every poll
**File:** `plc_manager.py:267-316`
**Issue:** The `with LogixDriver(path, init_tags=True, init_program_tags=True)` block opens a new EtherNet/IP TCP session AND fetches the full tag dictionary on every single poll cycle. This adds 50-200ms per cycle.
**Fix:** Persist the `LogixDriver` instance. Remove `init_tags=True` during steady-state polling. Use it only at startup and on reconnect.

---

### H4: SQLite WAL mode not enabled
**File:** `backend/app/services/app_store.py:_ensure_schema`
**Issue:** SQLite uses the default DELETE journal mode. The cloud sync background thread (reader) and the event-loop historian writes (writer) contend at the file-level exclusive lock. Under sustained load, `SQLITE_BUSY` timeouts (10s) occur. Write throughput is limited to ~200 rows/sec.
**Fix:** Add `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON;` to `_ensure_schema` before the CREATE TABLE script.

---

### H5: Historian writes occur synchronously in `_broadcast()` on the event loop
**File:** `plc_manager.py:1314`
**Issue:** `_broadcast()` is `async def` but calls `app_store.append_historian_rows()` synchronously before fanning out to subscriber queues. This is a blocking SQLite write on the event loop that delays every WebSocket message by the write duration.
**Fix:** Offload with `await asyncio.get_event_loop().run_in_executor(None, app_store.append_historian_rows, rows)`.

---

### H6: `/ws/cloud-stream` serializes up to 9,000 rows on the event loop every second
**File:** `backend/app/main.py:113-127`
**Issue:** The cloud-stream endpoint calls `get_live_rows(5000)` + `get_historian_rows(1500)` + `get_log_rows(2500)` + `get_inspector_snapshot()` on every 1-second tick. All are blocking SQLite calls on the event loop. Then `json.dumps` serializes the full payload.
**Fix:** (1) Offload all DB calls to `run_in_executor`. (2) Switch from `json.dumps` to `orjson.dumps`. (3) Send only delta changes, not full snapshots.

---

### H7: No brute-force protection on login endpoint
**File:** `backend/app/routers/auth.py:42-61`
**Issue:** Unlimited login attempts with no rate limiting, lockout, or failed-attempt logging. Combined with C2 (hashes retrievable without auth), this enables both online and offline attacks.
**Fix:** Add `slowapi` rate limiter: max 5 attempts per 60 seconds per IP. Log failures with IP + username + timestamp.

---

### H8: WebSocket token exposed in URL query parameter
**Files:** `main.py:69`, `main.py:101`, `api.js:74-92`
**Issue:** `?token=<jwt>` is logged by every nginx/proxy/load balancer in the request path. The operator's 12-hour admin JWT appears in server access logs.
**Fix:** Authenticate via first WebSocket message (application-level handshake) instead of URL parameter.

---

### H9: `backendUrl` query parameter open redirect
**File:** `frontend/src/api.js:17-18`
**Issue:** `?backendUrl=https://evil.example.com` causes all API calls (including login credentials) to be sent to an attacker-controlled server.
**Fix:** Remove the `backendUrl` query param override entirely. It is a development convenience that creates a phishing vector in production.

---

### H10: SQLite backup uses `shutil.copy2` (not SQLite backup API)
**File:** `backend/app/services/app_store.py:1677`
**Issue:** File copy while holding only a Python threading lock can produce an internally inconsistent backup if SQLite's page cache has unflushed frames. On Windows, restore (`os.replace`) fails on a locked file.
**Fix:** Replace with `sqlite3.Connection.backup(dest_conn)` or `VACUUM INTO ?`.

---

### H11: Restore does not validate the database before making it live
**File:** `backend/app/services/app_store.py:1686-1703`
**Issue:** The backup file is copied directly to the live path with no integrity check. A truncated or corrupt backup immediately crashes the application.
**Fix:** Before `os.replace`, run `PRAGMA integrity_check` and verify presence of `historian_readings` and `config_documents` tables on the temporary copy.

---

### H12: `trustnode_users` (including passwords) stored in `localStorage`
**File:** `frontend/src/App.jsx:~line 56`, read back in multiple places
**Issue:** The full user array including PBKDF2 password hashes is persisted to `localStorage`. Any browser extension, XSS, or local script can read it.
**Fix:** Never persist user records (especially password fields) to `localStorage`. User data should be fetched from the backend on session restore only.

---

## Medium Priority Issues

### M1: No composite indexes on `historian_readings`
**File:** `app_store.py:_ensure_schema`
**Issue:** Three single-column indexes exist but none is composite. Range queries by `(gateway_id, ts_utc)` or `(tag_name, ts_utc)` do full table scans. All SELECT queries order by `id DESC` — the `ts_utc` index is never used.
**Fix:** Add `CREATE INDEX IF NOT EXISTS idx_hist_gateway_ts ON historian_readings(gateway_id, ts_utc DESC)` and `idx_hist_tag_ts ON historian_readings(tag_name, ts_utc DESC)`.

---

### M2: Cloud sync engine recreated on every 1-second cycle
**File:** `app_store.py:_flush_data_outbox_once`
**Issue:** `create_engine(...)` with a connection pool is called and `dispose()`-d every second. If `dispose()` is skipped due to an early return, connections leak.
**Fix:** Cache the cloud engine keyed on connection parameters. Recreate only when config changes.

---

### M3: Cloud sync transaction rolls back historian on log insert failure
**File:** `app_store.py:1052`
**Issue:** `live_latest` upsert, `historian_readings` insert, and `app_logs` insert are wrapped in one `engine.begin()` transaction. An `app_logs` schema mismatch rolls back historian rows and blocks the outbox indefinitely.
**Fix:** Split into separate transactions per table.

---

### M4: `historian_readings` PostgreSQL schema provisioned via `/database/provision` is missing `local_id`
**File:** `routers/database.py:191`
**Issue:** Tables provisioned via the database router lack the `local_id` column required for `ON CONFLICT(local_id) DO NOTHING` cloud upserts. Cloud sync fails silently against databases provisioned this way.
**Fix:** Add `local_id BIGINT UNIQUE` to the provisioning DDL in `database.py`, matching `app_store.py:83`.

---

### M5: `cleanup_data` has no per-batch deletion limit
**File:** `app_store.py:2480-2528`
**Issue:** The manual cleanup endpoint issues unbounded `DELETE FROM historian_readings` (no LIMIT), acquiring an exclusive lock for the full duration. The scheduled retention enforces `max_delete_rows_per_run`; manual cleanup does not.
**Fix:** Apply the same `DELETE WHERE id IN (SELECT id ... LIMIT ?)` batching used in `run_retention`.

---

### M6: All AppShell memoized views recompute on every WebSocket reading
**File:** `frontend/src/App.jsx`
**Issue:** `dashboardItems`, `tagRows`, `historianRows`, `dataLogView`, etc. all depend on `dataLog` or `liveTagValues`. Since these are new object references on every WS message, all `useMemo` values recompute every second. With 10 dashboard widgets each doing `dataLogView.filter(...)` over 5,000 rows, that's 10 × O(5000) scans per second.
**Fix:** Use stable keys (Map keyed by tag identity), or separate live-display state (`liveTagValues`) from historical log state (`dataLog`) so memoized views only recompute when their specific input actually changes.

---

### M7: `opcua==0.98.13` is unmaintained with known XML vulnerabilities
**File:** `requirements.txt:9`
**Fix:** Replace with `asyncua` (actively maintained, async-native). Refactor the 3 OPC-UA call sites in `plc.py` and `plc_manager.py`.

---

### M8: OPC-UA connects without security policy (anonymous, unencrypted)
**File:** `plc_manager.py:511`
**Issue:** OPC-UA sessions are unauthenticated and unencrypted at the protocol layer. Man-in-the-middle on the OT segment can inject false tag values into the historian.
**Fix:** Configure `SecurityPolicyType.Basic256Sha256` + `MessageSecurityMode.SignAndEncrypt`.

---

### M9: PLC IP addresses not validated before network use
**Files:** `routers/plc.py:96`, `plc_manager.py` gateway start
**Issue:** No `ipaddress.ip_address()` validation. Any string reaches `socket.create_connection()` and `subprocess.run(["ping", ...])`, enabling SSRF probing of internal OT network hosts.
**Fix:** Validate with `ipaddress.ip_address(plc_ip)` and reject non-IP hostnames (or restrict to a configured OT subnet).

---

### M10: Database credentials stored in plaintext in SQLite
**File:** `app_store.py:288-345`
**Issue:** PostgreSQL passwords are stored as plaintext JSON in `config_documents`. Backup files contain them too.
**Fix:** Encrypt sensitive credential fields at rest using `keyring` (OS keychain) or AES-GCM with a key stored outside the SQLite file. At minimum, restrict backup file access.

---

### M11: pylogix fallback reads tags one at a time in a for-loop
**File:** `plc_manager.py:339-378`
**Issue:** `for tag in tags: comm.Read(tag)` — one EtherNet/IP packet per tag. 50 tags = 50 round-trips = 250ms+ at 5ms RTT.
**Fix:** `comm.Read(tags)` with the full list — one packet, same as the legacy `gateway_module.py` already does.

---

### M12: `retentionRuns` holds `self._lock` for the full rollup + delete operation
**File:** `app_store.py:1773`
**Issue:** During a large retention run, all other SQLite operations (historian writes, config reads) are serialized behind the lock. Operators may see the dashboard freeze during scheduled cleanup.
**Fix:** Move rollup computation outside the lock (reads only), acquire lock only for the delete step.

---

## Low Priority / Polish

| # | File | Issue |
|---|------|-------|
| L1 | `main.py:57` | JWT error message returned verbatim to caller — leaks implementation detail. Return generic "Authentication required". |
| L2 | `main.py` | Add `GZipMiddleware(minimum_size=1000)` — 60-80% reduction on JSON responses. |
| L3 | All WS handlers | Replace `json.dumps` with `orjson.dumps` — 3-5x serialization speed. |
| L4 | `health.py:7-20` | `/api/health` exposes build version and capabilities. Move to authenticated `/api/health/detailed`. |
| L5 | `auth.py:55` | Token expiry hardcoded at 12h. Add `TRUSTNODE_TOKEN_EXPIRY_SECONDS` env var. |
| L6 | `api.js:347` | `testDatabaseConnection` calls raw `fetch()` with no timeout — can hang indefinitely. Wrap in `fetchWithTimeout`. |
| L7 | `App.jsx` | WebSocket reconnect uses fixed 1.5s delay. Use exponential backoff (1.5s → 3s → 6s → 12s, cap 30s). |
| L8 | `app_store.py:201` | Backup directory is sibling of live DB. Add `TRUSTNODE_BACKUP_DIR` env override. |
| L9 | `requirements.txt` | Add `--hash=sha256:...` via `pip-compile --generate-hashes` for supply chain integrity. |
| L10 | `ci-cd.yml:123,133` | Pin `appleboy/scp-action` and `appleboy/ssh-action` to full commit SHAs, not floating `@v1`. |
| L11 | `plc_manager.py` | `PLCManager.max_gateways = 5` is hardcoded. Add `TRUSTNODE_MAX_GATEWAYS` env var. |
| L12 | `.env.example` | `TRUSTNODE_AUTH_SECRET` is not documented. Add it with a comment requiring 64 hex chars. |
| L13 | `main.py` | Replace deprecated `@app.on_event("shutdown")` with `lifespan` context manager. |
| L14 | `app_store.py:206` | `_connect()` creates a new `sqlite3.Connection` per call. Use one persistent connection guarded by the existing lock. |
| L15 | `App.jsx` | `trustnode_users` LocalStorage key stores password hashes. Remove entirely — fetch from API only. |

---

## Implementation Sequence

Execute in this order to minimize risk to working functionality:

### Phase 1 — Security (no behaviour change to PLC collection)
1. **C5: Fix cleanup_data direction** — `>= cutoff` → `< cutoff` (1 character, zero risk)
2. **C4: Wire CORS origins** — `allow_origins=settings.cors_origins` (1 line)
3. **C1: Remove admin/admin fallback** — return 503 if no users configured
4. **C3: Add RBAC dependencies** — `require_admin` / `require_operator` via FastAPI Depends
5. **C2: Fix GET auth bypass** — remove GET exemption from auth_middleware; add explicit exemptions
6. **H7: Add login rate limiting** — `slowapi`, 5 req/60s per IP
7. **H9: Remove backendUrl query param** — remove 3 lines from api.js
8. **H12: Remove users from localStorage** — strip from `buildLocalStoragePayload`

### Phase 2 — Data Integrity (no latency changes)
9. **H4: Enable WAL mode** — 3 PRAGMA lines in `_ensure_schema`
10. **H1/H5: Add composite indexes** — 2 CREATE INDEX statements (M1)
11. **H10: Fix backup API** — `sqlite3.Connection.backup()` instead of `shutil.copy2`
12. **H11: Validate restore** — `PRAGMA integrity_check` before `os.replace`
13. **M3: Split cloud sync transaction** — separate `engine.begin()` per table
14. **M4: Add `local_id` to provision DDL** — align database.py with app_store.py schema

### Phase 3 — Performance (PLC collection improvements, test in dev first)
15. **H1: Wrap PLC reads in `run_in_executor`** — `ThreadPoolExecutor(max_workers=8)`, wrap `_read_from_gateway`
16. **H3: Persist pycomm3 LogixDriver** — store in `GatewayWorker`, reconnect on failure
17. **H2: Persist Snap7 client** — store in `GatewayWorker`, reconnect on `get_connected() == False`
18. **M11: Fix pylogix batch read** — `comm.Read(tags)` list call
19. **H5: Move historian write off event loop** — `run_in_executor` in `_broadcast`
20. **H6: Fix cloud-stream WS** — offload DB calls to executor; switch to orjson; send deltas

### Phase 4 — Frontend (can be done in parallel with Phase 3)
21. **M6: Separate liveTagValues from dataLog memoization** — prevents O(n×widgets) scans per second
22. **L7: Exponential WebSocket reconnect backoff**
23. **L6: Wrap testDatabaseConnection in fetchWithTimeout**
24. **M7: Migrate opcua → asyncua** (backend + some frontend discovery calls)

---

## Do-Not-Touch List

The following are working correctly and must NOT be changed:

| What | Why |
|------|-----|
| `conn.executemany()` for historian batch inserts | Correctly batched — do not split into per-row inserts |
| `asyncio.Queue(maxsize=200)` per WebSocket subscriber | Correct backpressure design |
| Retention rollup cascade order (raw→min→hr→day) | Aggregates before deleting — correct |
| Ref-mirror pattern in App.jsx (`devicesRef`, `gatewayConfigsRef`, etc.) | Prevents stale closures — do not add these to useEffect deps |
| `_compact_sync_outbox_for_domains()` on startup | Prevents outbox table unbounded growth |
| `ON CONFLICT(local_id) DO NOTHING` in cloud historian insert | Idempotent retry safety |
| `_safe_name()` regex in database.py | SQL injection guard for DDL |
| pycomm3 `plc.read(*tags)` variadic batch call | Single CIP packet for all tags — correct |
| `isForcedReadonlyCloudMode()` permission gates in App.jsx | Consistently applied — cloud build read-only enforcement |
| Store-and-forward outbox per gateway worker | Core reliability guarantee |
| `hmac.compare_digest` in password and JWT verification | Timing-attack safe — do not replace with `==` |
| Auth secret stored in SQLite with env var override | Correct persistence pattern |

---

## File Reference Quick Map

| Topic | File | Key Lines |
|-------|------|-----------|
| Gateway polling loop | `backend/app/services/plc_manager.py` | 109–137 |
| pycomm3 read | `plc_manager.py` | 267–316 |
| Snap7 read | `plc_manager.py` | 450–508 |
| OPC-UA read | `plc_manager.py` | 511–578 |
| pylogix read (one-tag loop) | `plc_manager.py` | 339–378 |
| WebSocket broadcaster | `plc_manager.py` | 1268–1328 |
| Historian insert | `app_store.py` | 2186–2218 |
| cleanup_data bug | `app_store.py` | 2517 |
| WAL missing | `app_store.py` | `_ensure_schema` |
| Backup copy | `app_store.py` | 1677 |
| Restore no-validate | `app_store.py` | 1686–1703 |
| Cloud sync transaction | `app_store.py` | 1052–1162 |
| Auth GET bypass | `main.py` | 49–50 |
| CORS wildcard | `main.py` | 22–28 |
| admin/admin fallback | `routers/auth.py` | 22–31 |
| JWT implementation | `auth.py` | 55–85 |
| PLC SSRF surface | `routers/plc.py` | 96–136 |
| `local_id` missing in provision | `routers/database.py` | 191 |
| WebSocket URL token | `api.js` | 74–92 |
| backendUrl open redirect | `api.js` | 17–18 |
| localStorage users | `App.jsx` | ~56 |
| WS message handler | `App.jsx` | 2383–2612 |
| Cloud polling dual path | `App.jsx` | 2911+ |
| testDatabaseConnection no timeout | `api.js` | 347 |
