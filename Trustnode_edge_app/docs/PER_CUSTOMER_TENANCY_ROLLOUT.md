# Per-Customer Tenancy Rollout

**Date written:** 2026-05-18
**What it does:** moves TrustNode from "everyone on tenant_id='default'" to
"one tenant per customer", so Supabase RLS isolates each customer's Lite
users from every other customer's data. The master admin keeps cross-
tenant access via the existing `lite_is_global_admin()` policy.

**Risk level:** medium-high. The migration rewrites `tenant_id` on every
historian/log/alarm/dashboard row. Take a Supabase backup BEFORE applying.

---

## 0. Prerequisites — read before doing anything

- A current Supabase backup. Either a Supabase Cloud point-in-time backup
  or a `pg_dump` you can restore from your laptop. **Don't skip this.**
  If the backfill UPDATE breaks anything, this is your only safety net.
- The new desktop EXE built from commit (latest `main` after this commit
  lands). Customers' edges keep running on the old EXE until you upgrade
  them; that's fine — the old EXE will warn loudly when it boots against
  the new portal (see Phase C below).
- Maintenance window for the portal: ~15 min, enough to apply the SQL,
  redeploy the portal backend, and run smoke tests. Lite users will see
  stale data during this window but won't be served wrong data.
- The customer_id values you've assigned to each customer in the portal.
  Tenant slugs will be `tenant-<customer_id>` — confirm they don't
  contain characters that break URLs or SQL (alphanumeric + `-_` is
  safest).

## 1. Apply the SQL migration

```bash
# From your laptop, against the Supabase project's connection string:
psql "$SUPABASE_DB_URL" -f Trustnode_edge_app/db/migrations/20260518_per_customer_tenant.sql
```

Watch the output for `NOTICE: POST-MIGRATION LEFTOVER` lines. Any non-zero
count there means a customer-scoped row is still tagged `default` — that
row will be invisible to that customer's Lite users until you fix it.
Common causes:

- A customer with NULL `customer_id` (master-owned resource — leave it on
  `default`, it's correct).
- A historian row with no `edge_id` column (older edges might not have
  written it — those rows stay on `default` and are master-only).
- A `dashboard_configurations.scope_key` that doesn't decode to a known
  `customer_id` (orphan from a deleted customer — leave it, master can
  clean up via the portal).

If you see leftovers you DO want migrated, write a follow-up `UPDATE`
statement using `cp_edges` joined by `edge_id` and run it manually.

## 2. Deploy the backend control-plane changes

The portal and the edge share the same Python codebase. After this rollout
the portal will:

- `POST /api/control-plane/customers` → auto-creates `tenant-<customer_id>`
  in `cp_tenants` and assigns the customer to it.
- `POST /api/control-plane/edges` / `licenses` / `activation-code/issue` /
  `users` → forces the per-customer tenant when `customer_id` is supplied.

Deploy steps on your VPS:

```bash
cd /opt/trustnode/Trustnode_edge_app
git pull origin main
sudo systemctl restart trustnode-control-plane
sudo systemctl restart trustnode-edge-api  # if separate
```

Verify with:

```bash
curl -s -X POST https://trustnode.lsapps.app/api/control-plane/customers \
  -H "Authorization: Bearer $MASTER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"customer_id":"smoke-test-A","company_name":"Smoke Test A","status":"active"}' \
  | jq .
# Expected: {"ok":true,"tenant_id":"tenant-smoke-test-A","row":{...}}
```

If `tenant_id` in the response is still `default`, the new code didn't
deploy — restart the service.

## 3. Smoke test: cross-customer isolation

Before upgrading any real customer's EXE, prove isolation works:

a. Create two test customers via the portal: `smoke-test-A`, `smoke-test-B`.
b. Create a Lite user for each (different email each).
c. Open `https://trustnode.lsapps.app/lite/` in two private browsers, log
   in as each user.
d. Check the customer picker in Lite: each user should see only their own
   customer. The master admin login should see both.

If a tenant user can pick the other tenant's customer, the RLS policy on
`cp_customers` is wrong — stop here and check `cp_customers` policies in
Supabase.

## 4. Upgrade one real customer's edge

Pick the customer most tolerant of downtime. On their machine:

1. Stop TrustNode (tray app → exit).
2. Install the new EXE.
3. Open the app, go to Settings → Edge.
4. Click **Unlink Local Edge** (this clears the stale `default` tenant).
5. Go back to the portal, generate a NEW activation code for that edge
   (the old one is consumed). The new code will carry the per-customer
   tenant.
6. Paste the new activation code in the edge app, click Activate.
7. Verify Settings → Edge now shows `Linked Tenant: tenant-<customer_id>`,
   `Linked Customer ID: <customer_id>`.
8. Wait ~30s for new historian rows + dashboard mirror to land in cloud.
9. Open Lite as that customer's user — they should see the new edge.

If any of those checks fail, capture `/api/database/inspector` output and
the Data History → cloud_sync log lines before reverting.

## 5. Roll out to remaining customers

Same procedure as step 4 for each customer. There's no batch shortcut —
each edge has to be unlinked and re-activated because the activation code
encodes the tenant.

## 6. After all customers are on the new EXE

- Verify the `NOTICE` block in the migration shows ZERO leftovers when re-
  run (the migration is idempotent — re-running it confirms steady state).
- Optionally tighten Lite RLS: drop the `lite_is_global_admin()` bypass on
  tables you don't want the master to see (e.g. customer historian) for
  defense in depth. Recommended only after you've verified per-tenant
  isolation works end-to-end for at least a week.

## Rollback

If something is catastrophically wrong AFTER the migration is applied but
BEFORE any customer has activated against the new portal:

```sql
-- Run inside Supabase SQL editor as service_role.
BEGIN;
UPDATE public.cp_customers SET tenant_id = coalesce(ancestor_tenant_id, 'default');
UPDATE public.cp_edges     SET tenant_id = 'default' WHERE customer_id IS NOT NULL;
UPDATE public.cp_licenses  SET tenant_id = 'default' WHERE customer_id IS NOT NULL;
UPDATE public.cp_edge_activation_codes SET tenant_id = 'default' WHERE customer_id IS NOT NULL;
UPDATE public.cp_users     SET tenant_id = 'default' WHERE tenant_id LIKE 'tenant-%';
-- Lite data tables — only if you have time to re-tag them all:
UPDATE public.dashboard_configurations SET tenant_id = 'default' WHERE tenant_id LIKE 'tenant-%';
UPDATE public.alarms_setup             SET tenant_id = 'default' WHERE tenant_id LIKE 'tenant-%';
UPDATE public.triggers_limits          SET tenant_id = 'default' WHERE tenant_id LIKE 'tenant-%';
UPDATE public.historian_readings       SET tenant_id = 'default' WHERE tenant_id LIKE 'tenant-%';
UPDATE public.plc_readings             SET tenant_id = 'default' WHERE tenant_id LIKE 'tenant-%';
UPDATE public.live_latest              SET tenant_id = 'default' WHERE tenant_id LIKE 'tenant-%';
UPDATE public.app_logs                 SET tenant_id = 'default' WHERE tenant_id LIKE 'tenant-%';
UPDATE public.lite_profiles            SET tenant_id = 'default' WHERE tenant_id LIKE 'tenant-%';
COMMIT;
```

After rollback, revert the portal backend to the previous commit and
restart the services. **Don't** push out the new EXE during rollback —
it will refuse to activate against a `default`-only portal.

---

## What this does NOT cover

- The portal frontend UI. The "create customer" form in your portal may
  need to drop any "tenant_id" input field (it's now derived). I don't
  have the portal frontend source in this repo to update.
- A `customer_id` column on `lite_profiles`. The migration handles
  `lite_profiles` only if the column already exists. If your Lite signup
  flow doesn't write `customer_id` into `lite_profiles`, you need to add
  it manually in the portal-side user-create code, then re-run the
  migration to backfill.
- Master-owned resources (rows where `customer_id` IS NULL stay on
  `default`). The master admin still sees them via `lite_is_global_admin()`.
