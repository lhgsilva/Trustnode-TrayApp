# TrustNode — Product Surfaces, LAN Runtime Access & View Licensing

**Research report + target architecture + rollout plan**
Date: 2026-08-21 · Status: PROPOSAL (no code changed) · Scope: edge backend (`backend/app`), Electron shell (`desktop/`), React app (`frontend/`), LAN-served static UIs (`backend/static/*`), cloud read-only view (`web_cloud_readonly/`, VPS), portal contract, release gate.

> Companion to `docs/LICENSE_PACKAGES.md`, `docs/PORTAL_PACKAGE_EDITOR_SPEC.md`, `docs/CUSTOMER_PORTAL_MULTI_TENANT_PLAN_2026-04-24.md` and `docs/historian-retention-and-forwarding-architecture-2026-08-21.md`. Same method as the collection-engine and retention plans: read the running system first, name every gap with a `file:line`, then change the app in gated phases that never put the collection runtime at risk.

---

## 0. Executive summary

**The product the operator described already exists as code — it is not enforced, not hardened, and not verified.**

The edge binary already serves **three browser surfaces** from a second HTTP listener on the LAN (`lan_socket.py`, `0.0.0.0:8088–8092`, same FastAPI app as the desktop):

| Surface | URL (LAN) | Bundle | What it is |
|---|---|---|---|
| **Full runtime** (admin configuration + dashboards + collection control) | `/trustnode/full/app/` | `frontend/dist` (the desktop React app, shipped inside the EXE) | the complete edge software in a browser |
| **Client View** (read-only React, mobile nav) | `/trustnode/client/app/` | `frontend/dist_client_view` | the React app with admin pages blocked |
| **Slim Lite** (read-only, vanilla JS fork of the cloud Lite) | `/trustnode/lite/app/` | `backend/static/lite_view` | reads via `/api/lite-local/*` |
| shared LAN login page | `/trustnode/login/` | `backend/static/login` | issues the JWT, checks `access_full/lite/client` |

The **cloud read-only view** (`https://trustnode.lsapps.app/lite/`) is a fourth surface: the vanilla-JS Lite talking **directly to Supabase** (Auth + RLS + Realtime), with the edge generating its reports through a Supabase queue. The full React app also runs on the VPS as the cloud portal (`TRUSTNODE_PREFER_CLOUD_READS=true` makes every `/api/app-store/*` read hit Supabase).

Per-user access flags (`access_full`, `access_lite`, `access_client` — "LAN Web Access" in User & Access), view-link share tokens (`cp_edge_view_links`), a concurrent-viewer limit (`max_view_users`, enforced), ED25519-signed licenses pushed by the portal, and a per-install JWT secret all exist.

**What is missing is everything that makes it safe and sellable:**

1. **Module licensing is UI-only.** `license_inspect.has_module()` has **zero callers** in the backend. Nothing server-side checks `lan_access`, `local_web_app`, `cloud_lite_access` or any other module; the Lite/LAN path has no license gate at all. Tiers cannot be enforced until this lands (§3.4).
2. **Role enforcement is sparse server-side.** `plc.py` (gateway start/stop/config, 10 mutating routes), `reports.py` (17), `database.py`, `connections.py`, `notifications.py`, `lan_sharing.py` have **no role checks**; only `app_store.py`/`retention.py` use `_require_admin`. Harmless while the UI is local; unacceptable once the runtime is reachable from other PCs (§3.3).
3. **No TLS anywhere on the edge.** Admin passwords, JWTs (headers and the WebSocket `?token=`) cross the plant LAN in cleartext (§3.5).
4. **The static bundles are served without server-side auth** — the admin bundle is downloadable by any LAN peer; the only gate is a client-side redirect that fails *open* on a network error (`frontend/src/main.jsx:46-49`).
5. **Two landmines that break the full runtime over LAN today:** (a) `_resolve_prefer_cloud_reads()` treats any non-localhost `Host` header as "cloud" (`routers/app_store.py:73-80`, duplicated in `power.py`, `plc.py`), so a browser on `http://192.168.x.x:8088/` reads Supabase instead of the local historian; (b) the LAN-Sharing/Connections pages call `/api/lan-sharing/*` with unauthenticated relative fetches that the middleware only waives for loopback → 401 from any other PC.
6. **The cloud Lite bundle is no longer in git.** Commit `eec3e33` (2026-06-26) deleted `web_cloud_readonly/lite/**`; the CI step that republishes it (`git show HEAD:…`) has failed since; the only copy is the git-ignored fork `0.0.0.1000/web_cloud_readonly/lite/`. Whatever is live at `trustnode.lsapps.app/lite/` cannot be rebuilt from the tracked tree.
7. **Two licence vocabularies.** The live catalog the portal pushes (`control_plane_store.MODULE_CATALOG`: `lan_access`, `local_web_app`, `cloud_lite_access`, `cloud_client_view`, `gateway_runtime_control`, …) and the documented one (`LICENSE_PACKAGES.md`: `view.lan*`, `view.web*`, `studio.*`) disagree; the slim Lite fails **open** when no `view.lan*` key is present.

**Proposal in one sentence:** keep the portal as the single source of truth, reuse the live catalog keys plus **one new key** (`remote_admin_lan`), put **server-side** licence + role + network-origin enforcement in one place, add TLS and a proper "Remote Access" page, fix the two landmines, restore the cloud Lite to git, and fold every surface into the release gate — in five gated phases where collection is never at risk.

**Tiers (commercial names → enforcement):**

| Commercial name | What the user gets | Licence modules (live catalog) | Network |
|---|---|---|---|
| **TrustNode Edge** (runtime + admin) | collection engine, full configuration, dashboards, reports — on the desktop **and**, with the LAN permission, from any PC on the LAN with an admin/engineer login | `gateway_runtime_control`, `gateway_configuration`, `tags`, `users_and_access_control`, … (today's set) + `lan_access` + **`remote_admin_lan`** (new) | loopback always; LAN when `remote_admin_lan` |
| **TrustNode View** (local read-only) | dashboards + reports in the browser on the LAN (web/mobile), reading the locally configured database | `lan_access` + `local_web_app` (+ optional `view_share_links`, new) | LAN |
| **TrustNode Cloud View** (cloud read-only) | dashboards + reports over the internet, reading the cloud database | `cloud_database` + `cloud_lite_access` (+ `cloud_client_view`) | internet (VPS) |

---

## 1. Requirements (restated from the operator, 2026-08-21)

1. The **edge software is the runtime** (collection engine) and also the **admin configuration** tool.
2. A **TrustNode Edge licence permission** must allow users to reach the runtime **from any computer on the LAN with the admin password** — the IPC-in-the-panel case — i.e. the *complete* software, not a reduced one.
3. **TrustNode View**: read-only dashboards + reports on the LAN (web or mobile), licence configured in the portal, synced to the edge, URL configured/exposed in the edge software, data from the **locally configured database**. (Today's "Lite".)
4. **TrustNode Cloud View**: the same, reachable inside and outside the company over the internet, data from the **cloud database**.
5. **All licensing and endpoints stay configured from the portal** by the developers, as today.
6. The read-only app is mostly done — **keep Lite local and Lite cloud working**; find the way to serve the complete runtime on the LAN so users can configure from another PC.
7. **Plan carefully; do not break the working runtime.**

---

## 2. Current state — what the code does today (evidence)

### 2.1 How each surface is served

- **Desktop:** Electron loads `resources/frontend/dist/index.html` via `file://` with `?backendUrl=http://127.0.0.1:8000` (`desktop/main.js:1323-1346`). Backend binds `127.0.0.1:8000` (`app/__main__.py`, `config.py:21-22`). `"/"` returns JSON only (`main.py:1426`).
- **LAN listener:** `lan_socket.py:53,95-110` — second `uvicorn.Server` on the **same** `app.main:app`, `host="0.0.0.0"`, port `8088→8092`, `lifespan="off"` (the main server owns startup; re-enabling lifespan double-fires every startup handler — 2026-07-25 note). Started at boot when `app_settings.lan_sharing_enabled` (`main.py:951-957`); toggled by `POST /api/lan-sharing/{enable,disable}` (`routers/lan_sharing.py:115,135`); status + URL lists at `GET /api/lan-sharing/status` (`:75-112`, `lite_urls`/`full_urls` per non-loopback IPv4, no `client_urls`). Firewall self-heal `windows_firewall.py:97-113`: program-scoped inbound TCP rule, **`profile=any`**.
- **Static mounts** (`main.py:390-459`, bundled by `trustnode-service.spec:136-165`): `/trustnode/lite/app` ← `static/lite_view`; `/trustnode/client/app` ← `frontend/dist_client_view`; `/trustnode/full/app` ← `frontend/dist`; landings at `/trustnode/{lite,full,client}/`; login at `/trustnode/login/`. Whole block is `try/except: pass` — a missing build silently yields 404s; **no gate asserts the bundles exist in the EXE**.
- **API base when served over HTTP:** `api.js:25-93` — same origin; `isHostedWebClientRuntime()` (`:36-46`, duplicated at `App.jsx:212-224` and `:3457-3466`) is **true** for a LAN IP → the app switches to "hosted" behaviour (~40 `if (isHostedWebClient) return;` guards disable edge-only pollers/writers; `localStorage` gets `trustnode_backend_mode=cloud`).
- **WebSocket:** `/ws/stream?token=<jwt>` (`main.py:1431-1449`), served by the same app — works over LAN.
- **Cloud:** VPS runs the same FastAPI under systemd with `TRUSTNODE_PREFER_CLOUD_READS=true` + `TRUSTNODE_CLOUD_DB_*` secrets (`.github/workflows/trustnode-edge-cicd.yml:229-249`); nginx terminates TLS (`deploy/nginx/trustnode-edge.conf`), serves the React portal at `/`, the vanilla Lite at `/lite/` with `config.json` aliased from `/etc/trustnode/lite-config.json`.

### 2.2 Authentication & sessions

- Login cascade `routers/auth.py:141-213`: per-IP rate limit (10/60 s, in-memory) → master-admin break-glass (`TRUSTNODE_MASTER_ADMIN_PASSWORD`, **defaults to `admin`**, `:90`) → AuthStore → control-plane users → any-tenant → Supabase password grant. No lockout, no password policy (only `len ≥ 8` on reset), `verify_password` accepts plaintext-stored passwords (`auth.py:46`).
- JWT: hand-rolled HS256, **12 h**, claims `sub, role, permissions, modules, tenant_id` (`auth.py:73-89`); secret from env → AuthStore → app_store (`:49-70`). Roles are free-text: `admin, super, engineer, operator, viewer` (+ view-set `view, client, client_operator, kiosk`).
- Middleware `main.py:1332-1423`: non-`/api/` paths **bypass auth entirely** (static bundles); `PUBLIC_PATHS`; `/api/lite-view/resolve/` and `/api/lite-local/` do their own token checks; **`/api/lan-sharing/*` and `/api/connections/*` are waived only for loopback** (`:1379-1385`); Bearer required otherwise; tenant taken from the JWT with a 403 on mismatch (`:1400-1406`).
- LAN variant gate `lite_local.py:224-268`: `check-access {variant}` → `access_full|access_lite|access_client` re-read from `app_store` (not the token); `admin|super` always pass. The React entry re-checks on load (`main.jsx:20-50`) but **falls back to rendering on any network error**.
- View links: `cp_edge_view_links`, per-edge and per-user, rotate/revoke (`control_plane.py:692-905`); `/api/lite-local/validate` mints a 12 h HMAC session JWT **and** a normal auth JWT with `role=viewer` for the link's user (`lite_local.py:291-326`).
- Concurrent viewers: `view_sessions.check_view_login_allowed` enforces `max_view_users` at login (`auth.py:262-280`), 5-min liveness window.

### 2.3 Licensing

- Portal → edge: activation code / edge-link bootstrap → `local-finalize` persists `tenant_id, edge_id, customer_id, license_id, cloud_url, supabase_url, infrastructure_endpoints…` (`control_plane.py:2236-2487`); refresh via `edge-link/license-check` (`:2587`), ED25519-signed (`license_signature.py`, 30-day grace), mirrored to the Windows registry.
- Evaluation `license_inspect.py`: `modules` explicit list (bare strings or `{key, enabled}`; **`enabled` defaults to True when absent**), grandfathered list wins, `package_key` defaults to `"edge"`, 30 s cache. **Enforced today:** `max_view_users` (`view_sessions.py:74`), `max_tags` / `max_gateways_per_edge` (`plc.py:1294-1295`), batch module (`modules/batch_management/license.py:93`, returns **404**), Intelligence module (`trustnode_intelligence/backend/license.py`). **Not enforced:** every other module — `has_module()` has no callers.
- Live catalog `control_plane_store.py:20-59` (30 keys incl. the dead `local_web_app`, `cloud_lite_access`, `cloud_client_view`, and `lan_access`); served to the portal at `GET /api/control-plane/modules`. The dev edge's real licence (`lic-f20f10ab`, tenant `tenant-cust-e5916328`) carries all of them — and **`end_utc = 2026-07-31`** (expired; renew in the portal before licensing tests).
- Frontend: `MODULE_KEY_BY_PAGE` (`App.jsx:2013-2032`), `hasLicenseModule` returns **true for client view** (`:10612-10626`), `studio.*` gating is fail-open (`:10814-10825`), `DEV_LICENSE_BYPASS` writes modules into `grandfathered_modules` (`:11197`). Staleness heuristic `control_plane.py:2693-2705` re-pulls whenever `len(local_modules) < len(catalog)` — adding keys makes every edge "stale".

### 2.4 Lite data paths (keep working)

- **LAN Lite** (`/api/lite-local/*`, `lite_local.py:345-477`): `bootstrap / live / historian` read the **customer Postgres when `app_settings.database_mode == "customer_sql"`**, else the edge SQLite — and deliberately ignore `prefer_cloud_reads`. Shim (`static/lite_view/local_supabase_shim.js`) maps tables → endpoints; **`generated_reports` and `app_logs` return `[]`** (no reports on LAN Lite), no realtime, client-side filters only.
- **Cloud Lite**: supabase-js direct to Supabase (`live_latest`, `historian_readings`, `generated_reports`, `report_templates`, `lite_report_requests`), RLS per tenant (`db/migrations/20260518_*`), Realtime channels. Reports: Lite inserts `lite_report_requests` → edge `LiteReportRequestPoller` (10 s) claims, renders locally, uploads to bucket `lite-reports` (service key), mirrors `generated_reports` (`lite_report_poller.py`, `reports_cloud_uploader.py`).
- **Full React app reads** (`/api/app-store/*`): `_resolve_prefer_cloud_reads()` = explicit `?prefer_cloud=` else **"Host header is not localhost"** (`routers/app_store.py:73-80`; copies in `power.py:42,93,168,210`, `plc.py:1484-1488`); `prefer_cloud=True` **suppresses** the canonical customer-DB route (`app_store.py:8020, 8273`).

### 2.5 Things that break when the full app is opened from another PC today

| # | Symptom | Cause |
|---|---|---|
| 1 | Charts/tables show cloud data or nothing | `_resolve_prefer_cloud_reads` host heuristic (§2.4) |
| 2 | LAN Sharing + Connections pages show "Stopped", buttons do nothing | `App.jsx:2876/2888` relative `fetch("/api/lan-sharing/…")` without Bearer; middleware waives only loopback |
| 3 | Copy buttons silent | `navigator.clipboard` undefined on non-secure origins (`App.jsx:2928` + 5 sites) |
| 4 | Header logo 404 | `App.jsx:5502-5512` builds `origin + /assets/...` (assets live under the mount prefix) |
| 5 | Folder picker / workspace detection missing | Electron preload bridges (`window.trustnodeDialogs`, `window.trustnodeWorkspace`) absent, no "desktop only" affordance |
| 6 | App behaves as the cloud portal | `isHostedWebClientRuntime()` true for a LAN IP; `localStorage` mode pollution |
| 7 | Anyone can download the admin bundle | static paths bypass the middleware (`main.py:1351-1352`) |

---

## 3. Target architecture

### 3.1 One vocabulary, portal-owned (decision)

Keep **`MODULE_CATALOG` (code) as the authority** — it is what the portal editor renders and what every licence already contains. Do **not** migrate to the `view.*/studio.*` names now (35 keys, none implemented, and the catalog-size staleness heuristic would mark every edge stale). Instead:

- **Reuse** the dead keys: `local_web_app` → *TrustNode View (LAN)*; `cloud_lite_access` → *Cloud View (Lite)*; `cloud_client_view` → *Cloud View (React client view)*; `lan_access` → *LAN listener may bind*.
- **Add one key:** `remote_admin_lan` — *"Full runtime reachable from a non-loopback address with an admin/engineer login"*. Without it the full surface is loopback-only even when LAN sharing is ON. This is the single genuinely missing permission in requirement 2.
- **Optionally add** `view_share_links` — *"no-login view-link tokens allowed"* (a different security posture from "must log in"; keep it separately sellable).
- Provide an **alias map** in `license_inspect._normalize_module_key` so portal-issued `view.lan` ⇄ `local_web_app`, `view.web` ⇄ `cloud_lite_access`, `studio.*` ⇄ the existing studio keys resolve to the same flags; update `LICENSE_PACKAGES.md` to state the live names (it mandates same-commit updates and was never followed).
- Replace the staleness heuristic with a `license.updated_utc`/`version` comparison **before** any new key ships.
- `package_key` stays informational: `edge`, `view_lan`, `view_cloud`, `operations`, `enterprise` drive only the portal dropdown and the banner.

### 3.2 Access matrix (what is allowed where)

| Surface · origin | Anonymous | `viewer`/`operator` (login) | view-link token | `engineer` | `admin`/`super` |
|---|---|---|---|---|---|
| Desktop (loopback) | login page | read + operate (per role) | — | configure (no users/licence) | everything |
| `/trustnode/full/app/` from LAN | **login page only** (bundle not served) | **403** unless `access_full` **and** role ≥ engineer **and** `remote_admin_lan` | never | configure, if `remote_admin_lan` | everything, if `remote_admin_lan` |
| `/trustnode/client/app/` from LAN | login | read-only, if `local_web_app` + `access_client` | read-only, if `view_share_links` | same as viewer | same |
| `/trustnode/lite/app/` from LAN | login | read-only, if `local_web_app` + `access_lite` | read-only, if `view_share_links` | same | same |
| Cloud View (VPS) | Supabase Auth | read-only, if `cloud_lite_access`/`cloud_client_view` | — | — | portal admin |
| Mutating `/api/*` from LAN | 401 | **403** (read-only) | **403** | allowed for configuration prefixes when `remote_admin_lan` | allowed when `remote_admin_lan` |
| `/api/lan-sharing/*`, `/api/connections/*` | 401 | 403 | 403 | 403 | allowed (loopback or LAN) |

Rule of thumb: **network origin never grants rights; it can only remove them.** Loopback keeps today's behaviour exactly.

### 3.3 Central enforcement (server side) — the missing layer

One module `backend/app/access_policy.py` used by the auth middleware and as router dependencies:

1. **Role policy by method + prefix** (applied in `auth_middleware` after the Bearer check):
   - `GET/HEAD` → any authenticated role (read).
   - `POST/PUT/PATCH/DELETE` under configuration prefixes (`/api/plc/`, `/api/app-store/` writes, `/api/database/`, `/api/connections/`, `/api/lan-sharing/`, `/api/notifications/`, `/api/reports/templates|schedules`, `/api/control-plane/`, `/api/retention/`, `/api/directories/`, `/api/ui-source/`) → role ∈ {`engineer`, `admin`, `super`}; user/licence/LAN/connection management → `admin|super` only.
   - Operational actions a `operator` legitimately performs (acknowledge alarm, start/stop batch, trigger a report run, batch scan) are listed explicitly as exceptions — **the list is reviewed with the product owner before enforcement**.
   - Ship behind `TRUSTNODE_RBAC_MODE=log|enforce`; run `log` for one release (every would-be denial is written to the customer log with user, role, method, path) so the desktop flows are proven unaffected before `enforce` becomes default.
2. **Licence gates as dependencies** (copy the batch pattern: **404**, not 403, so unlicensed surfaces look non-existent): `require_module("local_web_app")` on `lite_local` router + the three landing/app mounts for lite/client; `require_module("lan_access")` + admin role on `/api/lan-sharing/enable`; `require_module("remote_admin_lan")` on the full-app mount and on **any** mutating request whose `request.client.host` is not loopback; `require_module("view_share_links")` on `/api/lite-view/resolve/` and the raw `?token=` branch of `_extract_session`.
3. **Static bundle gate:** wrap the `/trustnode/*/app/` mounts in a tiny ASGI guard that requires a valid session (cookie `tn_session` set by the login page, or the Bearer for XHR) before serving `index.html`/assets; landings and `/trustnode/login/` stay public. Removes the fail-open client redirect.
4. **View-only licences cannot write data:** when the licence has no `gateway_runtime_control`, `license_gate.is_data_writes_allowed()` returns `(False, "view_only_license")` (hook already exists, `services/license_gate.py:107`).
5. **Fix the host heuristic:** `_resolve_prefer_cloud_reads()` must use an explicit deployment flag — `TRUSTNODE_PREFER_CLOUD_READS` env (VPS) or `app_settings.endpoint_mode == "cloud"` — **never** the `Host` header; apply the same change to the copies in `power.py` and `plc.py`. Frontend: `isHostedWebClientRuntime()` (and its two copies) must distinguish *"served by this edge over LAN"* (same-origin `/trustnode/full/app/`) from *"hosted cloud portal"*; expose one `getRuntimeSurface()` = `desktop | lan_full | lan_client | lan_lite | cloud` and replace the ~40 ad-hoc guards with it over time (Phase 4).

### 3.4 Transport security & network posture

- **TLS on the LAN listener**: generate a per-install self-signed certificate + key at first enable (stored in the data dir, CN = hostname, SANs = all LAN IPs, 10-year validity), serve `https://<ip>:8443` (uvicorn `ssl_certfile/ssl_keyfile`; `LAN_PORT_CANDIDATES` → configurable `lan_port`, default 8443 for TLS, 8088 legacy HTTP kept behind `lan_http_enabled=false`). Offer **"Download certificate"** + a one-page trust instruction (Windows/Android/iOS) on the Remote Access page; allow uploading an enterprise cert/key for sites with their own CA. TLS also restores secure-context APIs (clipboard, service worker).
- Firewall rule `profile=private,domain` (not `any`); `bind_host` configurable (single NIC) — `lan_socket.py:97`.
- Session hardening for a LAN-exposed login: per-account lockout (5 failures → 15 min, persisted, admin-unlockable) in addition to the per-IP limiter; mandatory password change for the default master-admin; password policy (12+ chars) for admin/engineer; shorter JWT for LAN origins (4 h) with silent refresh; `Secure`/`HttpOnly` session cookie when TLS is on.
- Audit: every LAN login, denial, licence-gate hit and configuration mutation from a non-loopback origin → `cp_security_audit_log` (table exists) with user, role, IP, path.

### 3.5 Operator experience

- Rename **LAN Sharing → "Remote Access"** (Connections group) and make it the one place for: ON/OFF, TLS status + certificate download/upload, bind/port, the three URLs per IP (**Full / Client View / View**) with **QR codes**, per-user access flags shortcut, active remote sessions (user, surface, IP, since) with revoke, and the licence state of each surface ("TrustNode View: licensed · 2/3 viewers in use"). Tray submenu mirrors the URLs and the ON/OFF.
- First-enable wizard: explains that the admin login will be reachable on the LAN, forces the master-admin password change if still default, shows the certificate step.
- Browser-served full app: hide desktop-only controls with a *"available on the edge desktop"* note instead of silent failure; fix the logo path; route the LAN-Sharing/Connections calls through `getControlApiBase()` + `fetchWithTimeout` (Bearer attached).

### 3.6 TrustNode View & Cloud View — consolidation direction

Today there are **two read-only codebases** (vanilla Lite + its LAN fork; React client view). Recommendation: make the **React client view the single read-only UI for both LAN and cloud** (same code as the full app, mobile nav, reports and batch views already present, no hand-porting), keep the vanilla Lite **frozen** as the cloud's current experience until the React client view is verified on the VPS with `TRUSTNODE_PREFER_CLOUD_READS=true`, then retire the fork. Until then: restore the cloud Lite bundle to git (§4 Phase 0) so it can be rebuilt, and keep `/api/lite-local/*` untouched. Data routing stays as it is: LAN View reads the locally configured database (`database_mode`); Cloud View reads the cloud database (VPS env / Supabase).

---

## 4. Rollout plan (gated, collection never at risk)

Every phase ends with: `python scripts/validate_release.py` PASS on the running build **plus** the new surface checks of that phase. No phase changes the collection engine. RBAC goes live only after a full release in log-only mode.

### Phase 0 — Stop the bleeding, establish the baseline (days, no behaviour change for existing users)
1. **Restore `web_cloud_readonly/lite/**` to git** from `0.0.0.1000/…`; confirm the CI step `Add TrustNode Lite static bundle` succeeds and `scripts/push_lite_to_vps.py` finds `LITE_DIR`.
2. **Release-gate additions** (`scripts/validate_full_12h.py` + a new `scripts/validate_surfaces.py`): assert the EXE bundle contains `frontend/dist`, `frontend/dist_client_view`, `static/lite_view`, `static/login`; assert `/trustnode/{full,client,lite}/app/` and `/trustnode/login/` return 200 on loopback; with LAN sharing ON, assert `GET /api/lan-sharing/status.running` and a 200 on `http://<lan-ip>:<port>/trustnode/login/`; run the `check-access` matrix (403 without flag, 200 with, 200 admin); `/api/lite-local/{bootstrap,live,historian}` return rows with the expected `source`; `lifespan="off"` invariant (single `startup_event fired` after toggling).
3. **Fix the two landmines without changing semantics for loopback/VPS:** (a) `_resolve_prefer_cloud_reads` → explicit flag (env/`endpoint_mode`), all three copies; (b) LAN-Sharing/Connections frontend calls → authenticated via `getControlApiBase()`; plus logo path and clipboard fallback. Verify the desktop is byte-identical in behaviour (gate) and the VPS still prefers cloud (env is set there).
4. Renew the dev edge licence in the portal (`lic-f20f10ab` expired 2026-07-31).

### Phase 1 — Server-side enforcement in log-only mode (1–2 weeks)
1. `access_policy.py`: role policy by method/prefix + operator exception list; `TRUSTNODE_RBAC_MODE=log` default; denials → customer log + audit table.
2. Licence-gate dependencies (`require_module`) on: `lite_local` router, lite/client mounts (`local_web_app`), `lan_sharing.enable` (`lan_access` + admin), `/api/lite-view/resolve/` (`view_share_links`), full-app mount + non-loopback mutations (`remote_admin_lan`) — **also in log mode first** (`TRUSTNODE_LICENSE_GATES=log|enforce`).
3. Static-bundle session guard for `/trustnode/*/app/` (cookie set by the login page; Bearer accepted for XHR).
4. Portal: add `remote_admin_lan` (and `view_share_links`) to `MODULE_CATALOG` **after** replacing the staleness heuristic; portal editor shows them under "Cloud / Web"; reissue the dev licence with both ON.
5. Gate: one full release cycle with RBAC/gates in log mode; review the denial log from real desktop usage; adjust the exception list; then flip both to `enforce` in Phase 2's build.

### Phase 2 — Transport security + Remote Access page (2–3 weeks)
1. TLS listener (self-signed per install, cert download, enterprise cert upload), configurable port/bind, firewall profile fix, legacy HTTP off by default (toggle for migration).
2. Account lockout, master-admin default-password enforcement, password policy, LAN session TTL, audit of remote actions, active-session list + revoke (`view_sessions` already tracks liveness).
3. Remote Access page + tray parity + QR codes + first-enable wizard; "desktop only" affordances in the browser-served full app.
4. Flip `TRUSTNODE_RBAC_MODE` and licence gates to `enforce` (with the Phase-1 evidence). Gate: desktop unchanged; LAN full app usable end-to-end by an admin on another PC over HTTPS; viewer cannot mutate (API-level test); unlicensed surface → 404.

### Phase 3 — Licensing vocabulary & portal contract (1–2 weeks, mostly portal)
1. Alias map (`view.lan`⇄`local_web_app`, `view.web`⇄`cloud_lite_access`, `studio.*`⇄studio keys); `LICENSE_PACKAGES.md` rewritten to the live names with the packages `edge`, `operations`, `view_lan`, `view_cloud`, `enterprise`; `PORTAL_PACKAGE_EDITOR_SPEC.md` dropdown preselects them.
2. `cp_licenses` gains `package_key` + `limits_json` columns (today only in `metadata_json`), so the portal can report by tier.
3. Close the fail-open holes: `enabled` must be explicit (absent = OFF once `package_key` is present); slim Lite tab gating fail-closed; `DEV_LICENSE_BYPASS` never writes `grandfathered_modules`; client view no longer bypasses `hasLicenseModule` (cloud backend enforces anyway, but the UI must agree).
4. `max_view_users` counted per surface origin; `max_studio_admins` enforced on user create (documented, not wired).

### Phase 4 — View consolidation & cloud verification (2–4 weeks)
1. React client view verified on the VPS (cloud portal host) as *Cloud View*; then as the LAN *TrustNode View* default; vanilla Lite kept behind a feature flag, then retired.
2. `getRuntimeSurface()` replaces the `isHostedWebClient` triplet; LAN full app stops flipping to "hosted" semantics.
3. Reports on LAN View (the shim returns `[]` today): serve `generated_reports` + file download via `/api/lite-local/reports` backed by the local `reports_store` (the full app already downloads over LAN with the query token).
4. Docs: edge-side LAN deployment guide (cert trust, ports, VLAN/firewall profiles, hostname/mDNS), support runbook for "cannot reach the edge from another PC".

### Phase 5 — Optional hardening (later)
Reverse-proxy mode (customer nginx/IIS in front, `X-Forwarded-*` trust), SSO (LDAP/OIDC) for LAN admin login, per-IP allow-lists, signed URLs for share links with expiry.

---

## 5. Regression matrix (must stay green in every phase)

| Area | Check | Where |
|---|---|---|
| Desktop runtime | boot health ≤ 15 s, gateways resume ≤ 10 s, 10-min gate PASS | `validate_release.py` (exists) |
| Desktop UI | login, gateway start/stop, configuration save, dashboards, reports, batch, AI — unchanged with RBAC in `log` then `enforce` | manual + denial-log review |
| LAN full | admin on another PC: login → configure a tag → see live chart → download a report (HTTPS) | `validate_surfaces.py` + manual |
| LAN full (negative) | viewer login → every mutating call 403; unlicensed edge → `/trustnode/full/app/` 404; bundle not served without session | `validate_surfaces.py` |
| LAN View (client + lite) | login + share-link paths; `source` flips with `database_mode`; dashboards render; viewer limit enforced | items 1-8 of the Lite regression list (§2.4 report) |
| Cloud View | `curl /lite/` 200; `config.json` alias; two tenants isolated (RLS); Realtime; report round-trip `pending→running→done`, signed URL | `scripts/smoke_lifecycle_lite.py` steps 10-12 + CI smoke |
| VPS API | `TRUSTNODE_PREFER_CLOUD_READS=true` still routes to Supabase after the heuristic change; `?tenant=` mismatch → 403; SSE + `/ws/cloud-live` | CI deploy smoke |
| LAN listener invariant | toggling ON never re-fires startup handlers; port fallback; firewall rule present | gate log census |
| Portal contract | `GET /api/control-plane/modules` lists the new keys; licence-check does not loop on catalog growth; signature verified | portal smoke |

---

## 6. Risks and controls

| Risk | Control |
|---|---|
| Enforcing RBAC breaks a desktop workflow that silently relied on an unguarded route | log-only mode for a full release; operator exception list reviewed with the owner; denial log in the customer log |
| Changing `_resolve_prefer_cloud_reads` breaks the VPS | VPS already sets `TRUSTNODE_PREFER_CLOUD_READS=true`; CI smoke asserts Supabase rows; change keeps explicit flags, drops only the Host guess |
| Adding catalog keys makes every edge "stale" and hammer the portal | replace the count heuristic first (Phase 1.4) |
| Self-signed TLS confuses users / breaks mobile | certificate download + trust guide, QR, keep HTTP toggle for migration, enterprise-cert upload |
| Static-bundle session guard locks out the desktop | desktop stays on `file://` + loopback API (no mounts involved); guard applies only to `/trustnode/*/app/` |
| Two read-only codebases drift further | freeze the vanilla Lite, consolidate on the React client view (Phase 4), restore the cloud bundle to git now |
| LAN-exposed login brute force | per-account lockout + per-IP limiter + audit; default master password forced to change |
| Licence fail-open paths (`enabled` absent, grandfathered, client-view bypass) | Phase 3 closes them; until then surfaces are gated by role + `access_*` flags which are enforced server-side from Phase 1 |
| `lifespan="off"` second server skips new startup work | any new startup task must be idempotent and registered in the main server only (documented invariant; gate check) |
| A build without the frontend bundles ships silently | Phase 0 gate asserts bundle presence in the EXE and 200 on the mounts |

---

## 7. Decisions needed from the owner

1. **Tier naming in the portal**: `edge`, `view_lan`, `view_cloud` (+ `operations`, `enterprise`) as `package_key` values — OK?
2. **Which roles may configure from the LAN**: admin only, or admin + engineer (proposed)? Which operator actions are allowed remotely (alarm ack, batch start/stop, report run)?
3. **TLS default**: HTTPS-only on the LAN after Phase 2 (HTTP kept as an explicit legacy toggle)?
4. **Share links**: keep no-login view links as a separately licensed option (`view_share_links`), or require login always for TrustNode View?
5. **Read-only UI of record**: consolidate on the React client view for both LAN and cloud (recommended), retiring the vanilla Lite after verification?
6. **Remote management of locked-down sites** (`view.lan_only` in the old spec): is portal-initiated remote admin (cloud relay) in scope, or is LAN-with-admin-login sufficient for now? (This plan assumes the latter.)

---

## Appendix A — File map (where each concern lives today)

| Concern | Files |
|---|---|
| LAN listener / URLs / firewall | `backend/app/services/lan_socket.py`, `backend/app/routers/lan_sharing.py`, `backend/app/services/windows_firewall.py`, `desktop/main.js:1663-1848` |
| Static surfaces + login + landings | `backend/app/main.py:390-459`, `backend/static/{login,lite,lite_view,lite_slim}`, `frontend/src/main.jsx:20-50`, `backend/trustnode-service.spec:136-165` |
| LAN variant gate, view links, lite-local API | `backend/app/routers/lite_local.py`, `backend/app/routers/control_plane.py:692-905`, `backend/app/main.py:464-470,1363-1385` |
| Auth, JWT, sessions, rate limit | `backend/app/auth.py`, `backend/app/routers/auth.py`, `backend/app/services/auth_store.py`, `backend/app/services/view_sessions.py`, `backend/app/main.py:1332-1423` |
| Licensing | `backend/app/services/license_inspect.py`, `license_signature.py`, `license_gate.py`, `control_plane_store.py:20-59,247-304`, `routers/control_plane.py:1332-1362,2236-2487,2587-3298`, `modules/batch_management/license.py` (reference gate) |
| Read routing | `backend/app/routers/app_store.py:73-80`, `routers/power.py`, `routers/plc.py:1484-1488`, `services/app_store.py:556-569,1775-1811,2033-2158,8020,8273` |
| Frontend surface detection / gating | `frontend/src/api.js:4-109`, `frontend/src/App.jsx:192-224,1957-2032,2868-2938,3457-3466,10612-10827,11138-11358` |
| Cloud view + VPS | `web_cloud_readonly/` (+ fork `0.0.0.1000/web_cloud_readonly/lite/`), `deploy/nginx/trustnode-edge.conf`, `.github/workflows/trustnode-edge-cicd.yml:84-95,229-249,377-378`, `scripts/push_lite_to_vps.py`, `scripts/build-web-cloud-readonly.ps1`, `db/migrations/20260518_*` |
| Lite reports | `backend/app/services/lite_report_poller.py`, `reports_cloud_uploader.py`, `reports_store.py:646-702` |
| Release gate | `scripts/validate_release.py`, `scripts/validate_full_12h.py`, `scripts/boot_log_check.py` (+ proposed `scripts/validate_surfaces.py`) |
