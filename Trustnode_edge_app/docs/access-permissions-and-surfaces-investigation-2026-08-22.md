# Users, permissions, licensing and the three view surfaces — investigation and remediation plan

Status: **INVESTIGATION COMPLETE — PROPOSAL. No behaviour changed by this document**
(one security fix was applied immediately and is marked ✅ below).
Date: 2026-08-22 · Evidence gathered against the running build and the live database.

Companion documents: `licensing-seats-and-remote-access-plan-2026-08-22.md` (seats, implemented),
`edge-runtime-lan-access-and-view-licensing-plan-2026-08-21.md` (surfaces, implemented).

---

## 0. The short version

Twelve symptoms were reported. They are **not twelve bugs** — they are five root causes,
plus two security defects found on the way.

| # | Root cause | Explains |
|---|---|---|
| **R1** | **The "client view" is not read-only.** It is the same `App.jsx`, built with one flag that only shrinks the menu. It runs every mutating effect the full app runs — as a `viewer` those all 403, forever, on a retry loop. | empty dashboard, missing gateway/collection info, "Failed to fetch" on reports, "dashboards not available to other users" |
| **R2** | **Two competing permission vocabularies.** `alarms` vs `client_module_alarms` (and the same for reporting/interface). The nav reads the `client_module_*` key; role defaults always set it explicitly to `false`, so the `?? perms.alarms` fallback is unreachable. | alarms ticked but not in the menu; reporting/interface identically; several checkboxes that do nothing at all |
| **R3** | **Permissions and licence modules are two unrelated lists.** 34 licence modules, 27 permission labels, 24 rendered checkboxes, ~6 of which are dead. 17 modules have no per-user control at all. | "the list of permissions is very limited"; no Intelligence toggle; Tags unreachable; log page visible to everyone |
| **R4** | **The share-URL builder reads only the HTTP URL list.** This install is HTTPS-only, so that list is empty and the button copies a **bare token**, not a URL. When HTTP is on it picks `ips[0]` — here a VPN adapter. The hostname form the operator actually used is computed server-side and read by nothing. | "a Local View button appears but the URL is not correct"; "the correct URL should be the computer name or the IP" |
| **R5** | **Historian export round-trips every row through the browser.** The client loads ≤20 000 rows, POSTs them back as JSON, and the server builds the whole workbook in memory. | "export historian does not work for big amounts of data" |
| **S1** ✅ | **`/api/health` leaked the OpenAI API key to unauthenticated callers** on loopback *and* the LAN listeners. | — (found during this investigation) |
| **S2** | **An `engineer` can promote themselves to `admin`** by writing the `users_access` document through an endpoint that is not admin-gated. | — (found during this investigation) |

**The core of the app is not implicated.** Collection, historian, alarms, reports and the
gateway engine behave correctly; every failure above is in the access/permission/UI layer.

---

## 1. Evidence (verified, not inferred)

**Live audit log** (`app_logs`, category `access`) for the viewer the operator created:

```
17:26:12  login.remote ok:  user=Lucas role=viewer  POST /api/auth/login  ip=10.5.0.2
17:26:47  rbac denied:      user=Lucas role=viewer  POST /api/reports/scheduler/email-settings
17:26:55  rbac denied:      user=Lucas role=viewer  PUT  /api/app-store/domain
17:27:22  rbac denied:      user=Lucas role=viewer  POST /api/database/recovery/repair
17:27:24  rbac denied:      user=Lucas role=viewer  PUT  /api/app-store/bootstrap
            … the same three repeat every ~60 s until 17:40 …
```

The denials are rate-limited to one row per minute per identical action
(`access_policy.py:163-172`), so the real request volume is higher.

**Reports API is healthy.** As admin, over loopback *and* the LAN HTTPS listener:
`/api/reports/templates` 200 (≈30 ms), `/api/reports/generated` 200, `/api/reports/schedules` 200.
The banner text was a bare `Failed to fetch` — a transport-level `TypeError`, never an HTTP
status — on requests the server would have answered 200.

**Licence state on this box** (corrected — an earlier reading of this was wrong):
`license_status()` reports `lan_access`, `local_web_app`, `remote_admin_lan`,
`view_share_links` all **true**. `remote_admin_lan` is not in the module list; it is granted by
the legacy derivation, which only applies when the payload carries **no** `package_key`.
The `"edge"` shown in `/api/health` is `get_package_key()`'s display default, not the raw
value. **So this is a legacy/pre-tier licence and LAN access is genuinely licensed.**

**Secret exposure** (S1): an unauthenticated `GET /api/health` returned
`license_summary.module_configs.trustnode_intelligence.auth_token` — a 164-character live
`sk-proj-…` key.

---

## 2. The five root causes in detail

### R1 — the client view is not read-only

`/trustnode/client/app/` serves `frontend/dist_client_view`, which is **the same `App.jsx`**
built with `VITE_TRUSTNODE_CLIENT_VIEW=true` (`api.js:18,151-153`; mount `main.py:436-439`).
That flag does exactly two things: it hides a list of pages
(`CLIENT_VIEW_BLOCKED_PAGES`, `App.jsx:2041-2052`) and restyles the shell. **Nothing makes it
read-only.** Confirmed mutating effects that run on mount regardless of role:

| Effect | Location | Result for a viewer |
|---|---|---|
| e-mail settings autosave | `App.jsx:5955`, gated only on `!isHostedWebClient` (`:5961`) | 403 — path not in `OPERATOR_ALLOW` |
| database recovery repair | `App.jsx:6555-6588` | 403, and `dbRecoveryLastSignatureRef` is never updated (`:6603`) → **retries forever** |
| bootstrap / domain autosave | the config save loops | 403 — `/api/app-store/domain` is not admin-gated but resolves to `CONFIG_ROLES` |

Consequences the operator saw: the dashboard/gateway state never hydrates cleanly, and the
403 retry storm competes with the reports poll for Chrome's six connections per origin —
the reports call has a 12 s timeout (`api.js:181`) and, unlike `getGatewayInstanceStatuses`
(`api.js:473-490`), **no retry**: one transport hiccup becomes a permanent red banner
(`ScheduledReportsManager.jsx:187-201`).

### R2 — two permission vocabularies for the same feature

```js
// what the admin ticks                     // what the nav reads
PERMISSION_GROUPS "Notifications" → alarms  perms.client_module_alarms ?? perms.alarms   (App.jsx:10714)
PERMISSION_GROUPS "Reporting"     → reporting  perms.client_module_reporting ?? perms.reporting (:10715)
PERMISSION_GROUPS "Administration"→ interface  perms.client_module_interface ?? perms.interface (:10716)
```

`normalizePermissions` (`App.jsx:2299-2304`) merges `{...buildRolePermissions(role), ...raw}`,
and `buildRolePermissions` **always writes an explicit `false`** for `client_module_alarms`
(`:2275`), `client_module_reporting` (`:2276`) and `client_module_interface` (`:2277`). An
explicit `false` is not nullish, so `?? perms.alarms` **can never fire**.

Worse, the admin's own summary column uses the same broken expression
(`visibleClientModuleLabels`, `App.jsx:10877-10892`), so the Users table agrees with the
broken nav rather than with the checkbox that was ticked — which is why the mismatch is
invisible from the admin side. Meanwhile `deriveModuleKeysFromPermissions` (`:2085`) uses
**OR** on the same pair, so `user.modules` says "alarms" while the nav says no.

**Dead checkboxes** (rendered, never read): `scheduled_reports` and `email_and_notifications`
(nav reads `users_and_access_control` instead, `:10760-10761`), `control_plane` (nav is
`isPortalOnly`, `:10780`), `database` and `backup_and_retention` (nav is hard `isAdmin`,
`:10843-10845`), `data_log` (masked by `historian` always being set).

### R3 — permissions and licence modules are unrelated lists

- `MODULE_CATALOG`: **34** modules (`control_plane_store.py:20-66`).
- `PERMISSION_LABELS`: **27** keys (`App.jsx:1951-1979`); **24** rendered (`:2007-2023`).
- **17 modules have no per-user control**, including `trustnode_intelligence` (no gate at
  all, front or back — anyone logged in gets the AI assistant), `batch_management`,
  `historian_export`, `report_templates`, `custom_dashboards`, `oee_downtime`, `opcua`,
  `mqtt`, `plc_drivers`, `meter_drivers`, `cloud_lite_access`, `cloud_client_view`.
- **Server-side licence enforcement is 4 call sites** (`require_module` in `main.py:472`,
  `lan_sharing.py:175`, `lite_local.py:53,292`). Everything else is UI-only licensing.
- **Per-user permissions are cosmetic at the API layer.** `access_policy` reads only
  `access_full` / `access_client` / `access_lite`. Any authenticated session can `GET` any
  data endpoint, because `_required_roles` returns `None` for reads (`:274-275`).
- Three inconsistent page lists exist: `CLIENT_MODULE_PAGE_SET` (6 pages),
  `CLIENT_VIEW_BLOCKED_PAGES` (10 pages) and the `isReadonlyCloudMode` whitelist
  (`App.jsx:10822-10831`, which *does* include Tags and Logs).
- `role === "client"` takes a hard whitelist (`:10818-10821`) — **Tags is unreachable**;
  `role === "viewer"` takes a different path entirely. Two code paths, two answers.
- **There is no read-only concept**: `canEditPage` *is* the nav gate (`:10874`), so "can see"
  and "can edit" are the same boolean.
- **Logs**: the fallback viewer role sets `historian: true` (`:2274`) and the Logs page maps
  to `historian` (`:10737,10773-10775`) → **every viewer sees Logs by default**, and the logs
  API has no role check at all.

### R4 — the share-URL builder

`getLiteLocalShareTargets()` (`api.js:2055-2068`) reads **only** `body.lite_urls` and
`body.lan_port`. On this install `lan_http_enabled=false`, so the backend returns
`lite_urls: []`, `full_urls: []`, `view_urls: []`, `lan_port: 0` — the HTTPS equivalents live
under `https.urls` / `https.hostname_urls` (`lan_sharing.py:154-155`), which nothing reads.

Therefore `copyWithVariant()` (`App.jsx:2617-2642`) falls through to copying **`lk.token`** —
a bare token string. Even with HTTP enabled it takes `urls[0]` (`:2623-2625`), i.e. `ips[0]`,
which here is `10.5.0.2` (a VPN adapter) rather than `192.168.1.41` or the hostname. The
`hostname_urls` form — `https://DESKTOP-OP6ED6R:8443/trustnode/client/`, the one that actually
worked — is computed server-side and never used. `api.js:2059` also falls back to `body.port`,
which is **8000**, the loopback-only port.

### R5 — historian export

`POST /api/historian/export-xlsx` takes `rows: list[dict]` **in the request body**
(`historian_export.py:50-54`): the browser loads the data, uploads it, and the server echoes it
back as a workbook built entirely in memory (`:83-105`) and returned as a single chunk
(`StreamingResponse(iter([data]))`, `:347`). The client Load button caps at **20 000 rows**
with no pagination (`App.jsx:25031-25040`), so anything beyond that is silently missing from
the export. A `viewer` is additionally refused: the export is in `OPERATOR_ALLOW`, which
excludes plain viewers by design (`access_policy.py:52,91-92,281`).

---

## 3. Every reported item: cause → fix → risk → test

| # | Reported | Cause | Fix | Risk | Test |
|---|---|---|---|---|---|
| 1 | Users page badly laid out; checkbox and label should share a row; too much vertical space | UI only | Rebuild the page: seat ledger as a compact strip; users table; a permission editor grouped by **licence module group** with `label + checkbox` on one row, three columns, collapsed groups | None to logic — presentation only | UI smoke + a screenshot review |
| 2 | Local View button gives an unusable URL | **R4** | `getLiteLocalShareTargets` reads `https.hostname_urls` → `https.urls` → `hostname_urls` → `*_urls`, in that order; never copy a bare token — if no URL exists, say Remote Access is off | Low | New gate check: with HTTPS-only, the copied text starts `https://` and contains `/trustnode/` |
| 3 | Permission list far too limited, not licence-driven | **R3** | Derive the permission editor from `MODULE_CATALOG` groups; one row per licensable feature; hide (or show disabled) what the licence lacks | Medium — this is the big one; keep old keys working | Matrix test: for each module, a user with/without it sees/doesn't see the page |
| 4 | LAN view should be the same app read-only, no report/gateway errors | **R1** | Introduce a real read-only mode: a single `isReadOnlySurface` predicate that (a) suppresses every autosave/repair effect, (b) renders pages without edit affordances. Serve the FULL app read-only on `/trustnode/client/app/` | **High** — touches shared effects; must not alter desktop behaviour | Viewer session over LAN produces **zero** `rbac denied` rows in `app_logs` for a 10-minute window |
| 5 | Share URL must be hostname or IP that works | **R4** | Same fix as #2; prefer hostname, then a **routable** IP (exclude VPN/virtual adapters by default), and show all options | Low | Gate check as #2 |
| 6 | No enable/disable for TrustNode Intelligence per user | **R3** | Add `trustnode_intelligence` as a per-user permission; gate the menu on it AND add a server-side check in the intelligence router | Low | Viewer without it: menu hidden AND API 403 |
| 7 | Alarms ticked but missing from the menu | **R2** | Collapse to ONE key per feature; keep the legacy `client_module_*` keys as read-aliases for existing users; remove the explicit `false` defaults that make `??` unreachable | Medium — must migrate existing user docs | Tick "Alarms" → the item appears, in all three surfaces |
| 8 | Tags page not available as in the main app | **R3** | Tags becomes a normal licence-gated, permission-gated page available read-only on every surface | Low | Viewer with `tags` sees the page read-only; without it, no menu item and 403 on write |
| 9 | Reports not working in any version | **R1** (transport storm + no retry) | Fix R1; add retry/backoff to `ScheduledReportsManager.refresh()`; show a quiet inline state instead of a red banner on a transient error | Low | Reports load in full app, client view and lite; no banner during a 10-min viewer session |
| 10 | Admin dashboards not usable by other users | Mostly the wipe I caused; dashboards are **already edge-shared** (`app_store.py:100-120`) | Verify after restore; fix the three stale comments that claim the opposite (`:97-99,355-357,463-466`); ensure a viewer can *select* but not *edit* (read-only mode, #4) | Low | Two users, one dashboard: both see it; only admin can modify |
| 11 | Log page should be admin-only | **R3** | Move `data_log`/`logs` to an admin-only permission **and** add `/api/app-store/logs` to the server-side admin rules | Low | Viewer: no menu item, API 403 |
| 12 | Historian export fails for large data | **R5** | Server-side export: `POST /api/historian/export` takes a *range + filter*, streams the workbook/CSV in chunks, no rows in the request body; UI polls for completion. Decide whether a viewer may export (see §8) | Medium | Export 500 k rows without the browser holding them; memory bounded |

---

## 4. The target model (one source of truth)

Today: **licence → (mostly nothing) → UI**, and **permission → UI only**.
Target: **licence → capability → permission → surface**, enforced on the server.

```
portal package  ──▶  licence payload (modules, seats, limits)
                          │
                          ▼
        license_inspect  (has_module, seats, limits)
                          │
      ┌───────────────────┼────────────────────────┐
      ▼                   ▼                        ▼
  CAPABILITY          PER-USER PERMISSION       SEAT (who may open which surface)
  "does this site     "may this person use      "is this person licensed for
   have the feature"   the feature"              Studio / View LAN / Cloud View"
      │                   │                        │
      └──────────┬────────┴────────────────────────┘
                 ▼
      access_policy.evaluate()   ← server-side, for every /api call
      surface_access()           ← server-side, for every bundle load
                 │
                 ▼
      UI renders what the server would allow (menus are a mirror, never the gate)
```

Concretely:

1. **One registry**, generated from `MODULE_CATALOG`, mapping
   `module → pages → permission key → required role to WRITE`. Frontend and backend both
   read it (ship it to the UI via an endpoint so it cannot drift).
2. **One permission key per feature.** `client_module_*` become read-aliases during migration.
3. **Read vs write split**: every page gets `canView(page)` and `canEdit(page)`; the nav uses
   `canView`, the controls use `canEdit`. Today they are the same boolean.
4. **The server is the gate**: `_required_roles` gains per-path *read* rules for
   admin-only data (logs, users, database, control plane), and `require_module` is applied to
   every licensable router — not four.
5. **Surfaces become a rendering choice, not a different app**: full / client / lite all serve
   the same React app; the surface decides read-only + menu scope, and the server enforces it.

---

## 5. Users and Access Control — the redesign (item 1)

- **Row 1: seat ledger** — one compact strip, four products, `assigned / licensed`, over-assignment in red.
- **Row 2: users table** — user, e-mail (login), role, seats, surfaces, last login, actions.
  The current "Client Modules" free-text column is replaced by seat chips.
- **Row 3: the editor** — a drawer/modal, not an inline form:
  - identity (username, e-mail, role, status, password),
  - **seats** (Studio / View LAN + interface / Cloud View),
  - **feature permissions**, grouped exactly like `MODULE_CATALOG` (Visualization, Data,
    Operations, Reporting, Gateways, Admin, Connections, Applications, AI), each row
    `[✓] Label ……… (licence badge when the site lacks it)`, three columns, groups collapsible,
    with **Select all / none** per group.
- Density: label and control on **one line**; 32 px rows; no full-width single-column stacks.
- Everything disabled (not hidden) when the viewer is not an admin, so the page is
  self-explanatory rather than mysteriously empty.

---

## 6. Security items

| ID | Issue | Status |
|---|---|---|
| **S1** | `/api/health` (public, also on the LAN listeners) returned the plaintext OpenAI key in `license_summary.module_configs` | ✅ **fixed** — credential-looking fields are redacted to `"__set__"`; the in-process AI module reads its token from its own path (`trustnode_intelligence/backend/license.py:79`), so it keeps working. **The operator must rotate the exposed key.** |
| **S2** | `engineer` may write `users_access` (and the whole bootstrap) via `PUT /api/app-store/domain` / `/bootstrap`, which the handler mirrors straight into AuthStore → **self-promotion to admin** | Fix: treat `users_access` (and any domain carrying roles/permissions) as admin-only inside the domain-save handler, regardless of prefix; add a regression test |
| **S3** | On loopback the RBAC mode is `lan` = **log-only**, so a non-admin on the desktop is audited but never denied | Decide: keep (desktop is a trusted console) or switch to `enforce` with an explicit tray override. Recommend enforce, because the desktop now serves multiple named users |
| **S4** | Per-user permissions are not read by the API layer at all; any authenticated session can `GET` any data endpoint | Fix as part of §4.4 — read rules for admin-only data; feature reads gated by permission where it matters (logs, users, database, control plane) |

---

## 7. What must not break (non-regression rules)

1. **Collection, historian, alarms, reports, batch and the gateway engine are working** — no
   change to those paths. The gate's collection/delivery/distribution checks must stay green.
2. **The desktop admin experience must not regress.** Loopback behaviour changes only if S3 is
   accepted, and then with an explicit decision.
3. **Existing user documents keep working.** Old permission keys are read-aliases; nobody
   loses access on upgrade, and a migration writes the canonical keys once.
4. **Pre-seat licences keep behaving exactly as today** (already true, guarded by
   `seats_are_explicit()`).
5. **The blank-write guard stays**: no client may clear a saved collection.
6. **Every change lands with the release gate green** (`validate_release.py`) **and** the UI
   smoke test, run against a throwaway data dir — never the live install.

---

## 8. Phasing

Each phase is independently shippable and gated.

| Phase | Content | Why first |
|---|---|---|
| **A — security** | S2, S4 (read rules for logs/users/database/control-plane), S3 decision, plus item 11 | Smallest, highest risk if left |
| **B — read-only surfaces** | R1: `isReadOnlySurface`, suppress mutating effects, serve the full app read-only over LAN; item 9 retry/backoff; items 4, 10 | Removes the 403 storm that produces most visible symptoms |
| **C — permission model** | R2 + R3: one registry, one key per feature, read/write split, migration of old keys; items 6, 7, 8 | The correctness core |
| **D — Users page redesign** | Item 1, built on C's registry | Needs C's vocabulary |
| **E — URLs** | R4: items 2, 5 | Small, independent |
| **F — historian export** | R5: server-side streaming export; item 12 | Independent, larger engineering |
| **G — portal** | Package editor: module groups + seats; validation that a package cannot enable a page whose module is off | Portal repo |

---

## 9. Decisions needed before Phase B/C

1. **May a `viewer` export data?** Today `OPERATOR_ALLOW` says no (xlsx and report csv/txt),
   while client-side CSV works because it never reaches the server. Consistent options:
   allow viewers to export what they can see (recommended), or block both paths.
2. **Loopback RBAC (S3)**: enforce on the desktop, or keep log-only?
   *Recommendation: enforce* — the desktop is now a multi-user surface.
3. **`role=client` vs `role=viewer`**: two code paths exist today. *Recommendation: retire
   `client` as a role* and express everything as role + seat + permissions.
4. **Lite**: keep it for Cloud View only (as agreed 2026-08-21), and serve **View LAN** solely
   as the read-only full app? That removes the third page-list and simplifies C.
5. **Read-only depth**: hide edit controls only, or also hide pages an operator cannot act on?
   *Recommendation: show the page read-only* — an operator seeing Tags/Gateways read-only is
   the stated goal.
