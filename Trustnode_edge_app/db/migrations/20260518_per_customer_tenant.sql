-- ============================================================================
-- TrustNode — One tenant per customer (Model 1 multi-tenancy)
-- ============================================================================
-- Date:    2026-05-18
-- Target:  Supabase project (postgres database, `public` schema)
--
-- Purpose: Move from "everyone on tenant_id='default'" to "one tenant per
--          customer" so Postgres RLS can isolate customers from each other
--          using the existing tenant-based policies.
--
--   Before: Customer A and Customer B both share tenant_id='default'.
--           Lite RLS `tenant_id = lite_current_tenant()` evaluates true for
--           any 'default' row, so A's Lite users see B's data.
--   After:  Customer A is on tenant 'tenant-<A_customer_id>', Customer B on
--           'tenant-<B_customer_id>'. lite_profiles.tenant_id mirrors that.
--           RLS isolates structurally; no policy change needed.
--
-- Idempotent. Safe to re-run. Designed to be applied during a maintenance
-- window with the portal + all edges offline (or read-only) so no new rows
-- come in with the old tenant tag while we're rewriting them.
--
-- Order of operations:
--   1. cp_tenants: create one row per existing customer
--   2. cp_customers: tag with assigned_tenant_id, keep old tenant_id as
--      ancestor_tenant_id for audit, repoint tenant_id to the new value
--   3. Cascade the new tenant to every table that carries tenant_id and
--      is scoped to a customer (cp_edges, cp_licenses, cp_users,
--      cp_edge_activation_codes, alarms, dashboards, historian, …).
--   4. lite_profiles: re-tag each Lite user to their customer's new tenant.
--   5. Verify: surface any row that still carries tenant_id='default' AND
--      belongs to a customer-scoped resource (other than master rows).
--
-- DOES NOT TOUCH:
--   - The master admin's own tenant (we don't create a 'default' customer;
--     'default' stays as the master admin's home tenant).
--   - Existing RLS policies — the per-tenant filters already work; this
--     migration just moves rows to the right tenants.
--   - `lite_is_global_admin()` — master admin keeps cross-tenant access.
-- ============================================================================

BEGIN;

-- 0. Safety: refuse to run if a customer has no customer_id (would break
--    the tenant slug). cp_customers.customer_id is PRIMARY KEY so this
--    should never fire, but we want a loud error before any UPDATE.
DO $$
DECLARE bad_count integer;
BEGIN
  SELECT count(*) INTO bad_count FROM public.cp_customers WHERE coalesce(customer_id,'')='';
  IF bad_count > 0 THEN
    RAISE EXCEPTION 'cp_customers has % rows with empty customer_id — aborting before tenant rewrite', bad_count;
  END IF;
END $$;

-- 1. cp_tenants: ensure a row exists for every customer.
--    Tenant id format: 'tenant-<customer_id>' (decided 2026-05-18).
INSERT INTO public.cp_tenants (tenant_id, name, status, timezone, metadata_json)
SELECT
  'tenant-' || customer_id            AS tenant_id,
  coalesce(company_name, customer_id) AS name,
  'active'                            AS status,
  'UTC'                               AS timezone,
  jsonb_build_object('source','per_customer_migration','customer_id', customer_id) AS metadata_json
FROM public.cp_customers
ON CONFLICT (tenant_id) DO NOTHING;

-- 2. cp_customers: add audit column, then repoint tenant_id.
ALTER TABLE public.cp_customers
  ADD COLUMN IF NOT EXISTS ancestor_tenant_id text;

UPDATE public.cp_customers
   SET ancestor_tenant_id = coalesce(ancestor_tenant_id, tenant_id)
 WHERE ancestor_tenant_id IS NULL;

-- The cp_customers.tenant_id FK references cp_tenants(tenant_id). Both new
-- tenants were inserted above, so this UPDATE never violates the FK.
UPDATE public.cp_customers
   SET tenant_id   = 'tenant-' || customer_id,
       updated_utc = now()
 WHERE tenant_id <> 'tenant-' || customer_id;

-- 3. Cascade to every customer-scoped CP table that joins via customer_id.
UPDATE public.cp_edges e
   SET tenant_id = 'tenant-' || e.customer_id,
       updated_utc = now()
  FROM public.cp_customers c
 WHERE e.customer_id = c.customer_id
   AND e.customer_id IS NOT NULL
   AND e.tenant_id <> 'tenant-' || e.customer_id;

UPDATE public.cp_licenses l
   SET tenant_id = 'tenant-' || l.customer_id,
       updated_utc = now()
  FROM public.cp_customers c
 WHERE l.customer_id = c.customer_id
   AND l.customer_id IS NOT NULL
   AND l.tenant_id <> 'tenant-' || l.customer_id;

UPDATE public.cp_edge_activation_codes a
   SET tenant_id = 'tenant-' || a.customer_id
  FROM public.cp_customers c
 WHERE a.customer_id = c.customer_id
   AND a.customer_id IS NOT NULL
   AND a.tenant_id <> 'tenant-' || a.customer_id;

-- cp_users carries a tenant_id directly. Each portal user belongs to one
-- customer's tenant. The mapping lives in cp_user_tenant_memberships OR in
-- the user's existing tenant_id (legacy). We migrate by membership when
-- available, else leave the user alone (likely a master admin or a stale
-- account).
UPDATE public.cp_users u
   SET tenant_id = 'tenant-' || c.customer_id,
       updated_utc = now()
  FROM public.cp_customers c
 WHERE u.tenant_id = 'default'
   AND EXISTS (
     SELECT 1
       FROM public.cp_user_tenant_memberships m
      WHERE m.username = u.username
        AND m.tenant_id = c.tenant_id
   );

-- 4. Lite-readable data tables. These were all tenant_id='default' before
--    the migration. We rewrite each row's tenant_id based on the customer
--    that owns the edge_id (or gateway_id → edge mapping).
--
--    NB: tables that carry gateway_id but no direct customer_id use a
--    join through cp_edges. If an edge is unowned (customer_id IS NULL),
--    we leave the row on 'default' so it stays visible only to master.

-- Pre-compute a helper: edge_id → tenant lookup. Done as a CTE per
-- UPDATE so it stays inline-able with the planner.

UPDATE public.dashboard_configurations d
   SET tenant_id = 'tenant-' || split_part(d.scope_key, '|', 2)
 WHERE d.tenant_id = 'default'
   AND split_part(d.scope_key, '|', 2) <> ''
   AND split_part(d.scope_key, '|', 2) <> '-'
   AND EXISTS (
     SELECT 1 FROM public.cp_customers c
      WHERE c.customer_id = split_part(d.scope_key, '|', 2)
   );

UPDATE public.alarms_setup a
   SET tenant_id = 'tenant-' || split_part(a.scope_key, '|', 2)
 WHERE a.tenant_id = 'default'
   AND split_part(a.scope_key, '|', 2) <> ''
   AND split_part(a.scope_key, '|', 2) <> '-'
   AND EXISTS (
     SELECT 1 FROM public.cp_customers c
      WHERE c.customer_id = split_part(a.scope_key, '|', 2)
   );

UPDATE public.triggers_limits t
   SET tenant_id = 'tenant-' || split_part(t.scope_key, '|', 2)
 WHERE t.tenant_id = 'default'
   AND split_part(t.scope_key, '|', 2) <> ''
   AND split_part(t.scope_key, '|', 2) <> '-'
   AND EXISTS (
     SELECT 1 FROM public.cp_customers c
      WHERE c.customer_id = split_part(t.scope_key, '|', 2)
   );

-- report_templates / generated_reports may not carry scope_key but DO
-- carry tenant_id. If they have a customer_id column, use it; otherwise
-- they stay on 'default' (master-only).
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema='public' AND table_name='report_templates' AND column_name='customer_id'
  ) THEN
    EXECUTE $upd$
      UPDATE public.report_templates r
         SET tenant_id = 'tenant-' || r.customer_id
       WHERE r.tenant_id = 'default'
         AND r.customer_id IS NOT NULL
         AND EXISTS (SELECT 1 FROM public.cp_customers c WHERE c.customer_id = r.customer_id)
    $upd$;
  END IF;
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema='public' AND table_name='generated_reports' AND column_name='customer_id'
  ) THEN
    EXECUTE $upd$
      UPDATE public.generated_reports r
         SET tenant_id = 'tenant-' || r.customer_id
       WHERE r.tenant_id = 'default'
         AND r.customer_id IS NOT NULL
         AND EXISTS (SELECT 1 FROM public.cp_customers c WHERE c.customer_id = r.customer_id)
    $upd$;
  END IF;
END $$;

-- Historian / live / logs tables: scoped by gateway_id, with edge_id
-- recoverable via cp_edges. Use a CTE for the lookup.
WITH edge_tenant AS (
  SELECT e.edge_id, 'tenant-' || e.customer_id AS new_tenant
    FROM public.cp_edges e
   WHERE e.customer_id IS NOT NULL
), gateway_to_tenant AS (
  -- One gateway maps to one edge; if your model has gateways without
  -- direct edge_id storage in historian rows, we fall back to leaving
  -- those rows on default. The desktop edge writes 'edge_id' as a
  -- metadata field on historian_readings — if missing, no rewrite.
  SELECT edge_id, new_tenant FROM edge_tenant
)
UPDATE public.historian_readings h
   SET tenant_id = g.new_tenant
  FROM gateway_to_tenant g
 WHERE h.tenant_id = 'default'
   AND coalesce(h.gateway_id,'') <> ''
   AND EXISTS (
     -- match via the cp_edges row that owns the gateway. We do not yet
     -- have a gateway_id -> edge_id table in cloud; fall back to the
     -- edge_id column on historian_readings if present.
     SELECT 1
       FROM information_schema.columns ic
      WHERE ic.table_schema='public' AND ic.table_name='historian_readings'
        AND ic.column_name='edge_id'
   )
   AND h.edge_id = g.edge_id;

UPDATE public.plc_readings p
   SET tenant_id = 'tenant-' || e.customer_id
  FROM public.cp_edges e
 WHERE p.tenant_id = 'default'
   AND e.customer_id IS NOT NULL
   AND EXISTS (
     SELECT 1 FROM information_schema.columns ic
      WHERE ic.table_schema='public' AND ic.table_name='plc_readings'
        AND ic.column_name='edge_id'
   )
   AND p.edge_id = e.edge_id;

UPDATE public.live_latest l
   SET tenant_id = 'tenant-' || e.customer_id
  FROM public.cp_edges e
 WHERE l.tenant_id = 'default'
   AND e.customer_id IS NOT NULL
   AND EXISTS (
     SELECT 1 FROM information_schema.columns ic
      WHERE ic.table_schema='public' AND ic.table_name='live_latest'
        AND ic.column_name='edge_id'
   )
   AND l.edge_id = e.edge_id;

-- app_logs sometimes carry edge_id, sometimes not. Conditional rewrite.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema='public' AND table_name='app_logs' AND column_name='edge_id'
  ) THEN
    EXECUTE $upd$
      UPDATE public.app_logs a
         SET tenant_id = 'tenant-' || e.customer_id
        FROM public.cp_edges e
       WHERE a.tenant_id = 'default'
         AND e.customer_id IS NOT NULL
         AND a.edge_id = e.edge_id
    $upd$;
  END IF;
END $$;

-- 5. lite_profiles: every Lite user must end up on their customer's
--    tenant. If lite_profiles has a customer_id column already, we use
--    it directly. If not, we leave existing rows untouched (master
--    admin only) and Portal-side code is responsible for setting the
--    correct tenant on new sign-ups.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema='public' AND table_name='lite_profiles' AND column_name='customer_id'
  ) THEN
    EXECUTE $upd$
      UPDATE public.lite_profiles lp
         SET tenant_id = 'tenant-' || lp.customer_id
       WHERE lp.tenant_id = 'default'
         AND lp.customer_id IS NOT NULL
         AND EXISTS (SELECT 1 FROM public.cp_customers c WHERE c.customer_id = lp.customer_id)
    $upd$;
  END IF;
END $$;

-- 6. Verification block: surface anything still on tenant_id='default'
--    that belongs to a customer-scoped table. Raise NOTICE only (so the
--    transaction still commits) — operator reviews the output.
DO $$
DECLARE
  leftover record;
BEGIN
  FOR leftover IN
    SELECT 'cp_edges' AS tbl, count(*)::text AS n FROM public.cp_edges
      WHERE tenant_id = 'default' AND customer_id IS NOT NULL
    UNION ALL
    SELECT 'cp_licenses',         count(*)::text FROM public.cp_licenses
      WHERE tenant_id = 'default' AND customer_id IS NOT NULL
    UNION ALL
    SELECT 'cp_edge_activation_codes', count(*)::text FROM public.cp_edge_activation_codes
      WHERE tenant_id = 'default' AND customer_id IS NOT NULL
    UNION ALL
    SELECT 'dashboard_configurations', count(*)::text FROM public.dashboard_configurations
      WHERE tenant_id = 'default'
        AND split_part(scope_key,'|',2) NOT IN ('','-')
    UNION ALL
    SELECT 'alarms_setup', count(*)::text FROM public.alarms_setup
      WHERE tenant_id = 'default'
        AND split_part(scope_key,'|',2) NOT IN ('','-')
    UNION ALL
    SELECT 'triggers_limits', count(*)::text FROM public.triggers_limits
      WHERE tenant_id = 'default'
        AND split_part(scope_key,'|',2) NOT IN ('','-')
  LOOP
    IF leftover.n::int > 0 THEN
      RAISE NOTICE 'POST-MIGRATION LEFTOVER: % rows in % still on tenant_id=default', leftover.n, leftover.tbl;
    END IF;
  END LOOP;
END $$;

COMMIT;

-- ============================================================================
-- ROLLBACK (manual, in case of fire):
--   The migration is reversible because cp_customers.ancestor_tenant_id
--   captures the pre-migration tenant_id. To revert:
--     UPDATE public.cp_customers SET tenant_id = ancestor_tenant_id;
--     UPDATE public.cp_edges e SET tenant_id = c.ancestor_tenant_id
--       FROM public.cp_customers c WHERE e.customer_id = c.customer_id;
--   …and so on for every table touched above. Cross-table rollback is
--   error-prone, so prefer rolling forward with a fix migration instead.
-- ============================================================================
