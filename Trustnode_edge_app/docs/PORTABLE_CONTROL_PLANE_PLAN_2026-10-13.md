# Portable control plane, customer logins and migration — plan

**2026-10-13.** Owner's stated design, what already exists, what is genuinely
missing, and the order to close it in.

> Every claim below was checked against the running VPS, the live Supabase
> project and the code. Where something is a gap, the evidence is named.

---

## 1. The design, as stated

1. Every customer with a proper login uses **Lite / web view / mobile view**.
2. The **developer** has a **master login** that sees every customer and
   everything in the system.
3. **All logins are configured in the portal.** The portal's database is
   **Supabase in the cloud**.
4. The database that manages **customers, licences and users** is **separate
   from the customer database that holds their data**.
5. A customer may create **local users with permissions** on their own edge,
   but a **new licensed seat** must be set up by the developer in the portal.
6. The whole system is **migratable**:
   - the portal **website** can move to another host;
   - the portal **database** can move, and is pointed at **from the portal**;
   - the **customer database** (users config + telemetry) can move provider;
   - the **web client / Lite** can move provider.

---

## 2. What already exists — verified

| Capability | Where | Status |
|---|---|---|
| Control plane in Supabase | `cp_tenants`, `cp_customers`, `cp_users`, `cp_licenses`, `cp_edges`, `cp_license_modules`, `cp_edge_activation_codes`, `cp_infrastructure_config`, `cp_security_audit_log` | **Live** |
| Developer "sees everything" | `_is_global_admin()` = `role=admin` AND `tenant_id=default`; `list_customers(all_tenants=True)` | **Live** |
| Per-customer tenancy | `tenant-cust-*`, `_scoped_tenant()` rejects cross-tenant reads | **Live** |
| Portal-managed endpoints | `cp_infrastructure_config`, global + per-tenant, merged by `resolve_infrastructure_endpoints()`; UI at Portal → Infrastructure | **Live** |
| Endpoints travel to the edge | Embedded in the **activation code**, so a re-hosted deployment needs no rebuild | **Live** |
| Customer DB is configurable | `/api/database/test-connection`, `/provision`, `/activate-sink`, `/recovery/*` | **Live** |
| Edge local users + permissions | `auth_store` (`trustnode_auth.db`), licence seats | **Live** |
| Lite points at a configurable backend | `/lite/config.json` → `supabase_url` + `supabase_anon_key` | **Live** |
| Control-plane backend swappable | `TRUSTNODE_CONTROL_PLANE_BACKEND=local\|cloud`, chosen in `state.py` | **Live (env only)** |

`INFRA_KEYS` today: `cloud_api_url`, `supabase_url`, `ai_endpoint_url`,
`web_client_url`.

**Conclusion: points 1, 2, 5 and most of 6 are already built.** The failures
in production are data and configuration, not missing features.

---

## 3. Gaps — what is actually missing

### A. The master row does not exist in the cloud control plane *(data)*

Cloud `cp_users` holds **4** rows — `admin-newpc`, `admin-lucas`, `admin-mari`,
`admin` — all in customer tenants. There is **no `master`**, and **no row at
all in tenant `default`**.

Since `_is_global_admin()` requires `tenant_id=default`, the developer view
cannot be reached by anyone. `master` exists only in the VPS's *local*
`cp_users`, which is ignored because the backend is `cloud`.

> The master account **does** exist in Supabase **Auth**
> (`master@trustnode.local`, confirmed, last sign-in 2026-10-07). Auth and
> authorisation are different stores; only the authorisation row is missing.

**Fix:** one row — `tenant_id=default`, `username=master`, `role=admin`,
`status=active`. Password stays in Supabase Auth; the local hash must be a
hash of random bytes, **never blank** — `_verify_password` ends in
`hmac.compare_digest(password, raw)`, so an empty hash authenticates an empty
password.

### B. Tenant resolution does nothing *(config)*

No `cp_tenants` row has a `primary_domain`, so `trustnode.lsapps.app` resolves
to `default`. Combined with (A), `list_users(tenant_id="default")` is empty and
the Supabase-Auth login fallback can never fire.

**Fix:** set `primary_domain` per tenant for customer-specific hostnames, and
keep `default` as the developer/portal tenant.

### C. The portal database cannot be moved *from the portal* *(code)*

`TRUSTNODE_CONTROL_PLANE_BACKEND` and the connection details are **environment
variables read at import time** in `state.py`. Moving the portal database means
editing systemd and restarting — not a portal action, which is what the design
calls for.

**Fix:** store the control-plane connection in `cp_infrastructure_config`
(`control_plane_db_url`), add a portal screen to set and **test** it, and make
`_build_control_plane_store()` prefer it, falling back to env. Requires a
bootstrap source, since the setting lives in the database being configured —
see Phase 3.

### D. Control plane and customer data share one Supabase project *(config)*

`cp_*` and `plc_readings` are both in project `tsfreqjcrgbxdwvmxeuk`. The design
says the management database is **not** part of the customer data database.
They are logically separated and physically co-located.

**Fix:** move `cp_*` to its own project. No code change — (C) makes it a
portal action rather than a redeploy.

### E. Lite's config is a static file *(code)*

`/var/www/trustnode/lite/config.json` is written by a script and served by
nginx. `web_client_url` and `supabase_url` already exist in the portal but do
**not** drive it, so migrating the web client is a manual file push.

**Fix:** serve `/lite/config.json` from the backend, derived from
`resolve_infrastructure_endpoints()`, keyed by host → tenant. Keep the static
file as the offline fallback.

### F. No data migration between customer databases *(code)*

`provision` + `activate-sink` point the edge at a new database; **historical
telemetry does not move**. Only ad-hoc backfill scripts exist.

**Fix:** a ranged, resumable copy with verification — see Phase 4.

### G. The VPS is stale *(ops)*

86 commits behind (`db450eb`, 2026-07-15); the service has not restarted since
2026-08-27. `TRUSTNODE_SUPABASE_ANON_KEY` was missing — installed 2026-10-13 as
`20-anon-key.conf`, **pending a restart**. `/client/` returns 403 because nginx
tries to index the directory.

---

## 4. Plan

### Phase 0 — restore service *(no code; do first)*

1. Create the `master` row (A).
2. Restart the backend so the anon key loads (G).
3. Fix `/client/` — add `index client_test.html;` to the `location /client/`
   block, matching the fallback that block already names (G).
4. Verify: master login returns a JWT with `tenant_id=default`; the portal
   Customers page lists every tenant; `/client/` returns 200.

**Reversible:** delete the row, delete `20-anon-key.conf`, restore the nginx
backup.

### Phase 1 — make the deployment honest *(low risk)*

5. Set `primary_domain` per tenant (B).
6. Deploy the 86 commits and rebuild the served bundle. Verified beforehand:
   **no new SQL migrations, no new Python dependencies** — but 16 frontend
   source files changed, so the bundle **must** be rebuilt or the UI will call
   endpoints it was not built against.
7. **Landmine:** current `main` refuses the break-glass master from the network
   while its password is the default. Set `TRUSTNODE_MASTER_ADMIN_USER` and
   `TRUSTNODE_MASTER_ADMIN_PASSWORD` **in the same change**, or master breaks
   again with a 403 instead of a 401.

### Phase 2 — Lite follows the portal *(code, additive)*

8. Backend serves `/lite/config.json` from the resolved endpoints; nginx
   proxies that path instead of serving the file. Static file stays as the
   fallback.
9. Add `lite_url` to `INFRA_KEYS` so the Lite origin is portal-managed too.

### Phase 3 — portable portal database *(code, the real work)*

10. `control_plane_db_url` in `cp_infrastructure_config`, plus a portal screen
    with **Test connection** before save.
11. Bootstrap order: env → `cp_infrastructure_config` → local file. The pointer
    to the database cannot live only inside it; the env var remains the
    break-glass.
12. An **export/import** of the whole `cp_*` set so moving provider is: export,
    import, test, switch, verify.

### Phase 4 — customer data migration *(code)*

13. `POST /api/database/migrate` — ranged, resumable, idempotent copy between
    two configured databases, with row-count and checksum verification.
14. Dual-write window, then cut over the active sink, then stop the old one.
    Never delete the source as part of the migration.

### Phase 5 — separate the control plane *(config, after Phase 3)*

15. New Supabase project for `cp_*`; export/import; point the portal at it from
    the portal; keep the old project read-only for one cycle.

---

## 5. Order, and why

Phase 0 is the only thing standing between you and a working portal, and it is
one row, one restart and one nginx line. Everything after it is improvement,
not repair.

Phases 3 and 5 are the ones that deliver "the portal database can be moved from
the portal". They are last because they are the only ones that can lock you out
of the portal if they go wrong — which is exactly why the env var stays as the
break-glass path.

---

## 6. Standing risks

- **Never leave a `cp_users.password_hash` blank.** An empty hash authenticates
  an empty password.
- **Auth ≠ authorisation.** Supabase Auth holds the password; `cp_users` holds
  the role and tenant. A login needs both; they fail independently.
- **The anon key is public** (it is served in `config.json`); the **service
  key** is not. The service key and cloud DB password were exposed in a
  terminal session on 2026-10-13 and should be rotated.
- The bundle and the backend must move together; a mixed pair is how the Lite
  page ends up calling an endpoint that returns 404.
