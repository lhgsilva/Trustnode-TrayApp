-- ============================================================================
-- TrustNode — Per-customer tenancy hardening (Model 1 multi-tenancy)
-- ============================================================================
-- Date:    2026-05-18
-- Target:  Supabase project (postgres database, `public` schema)
--
-- Purpose: Lock in per-customer tenancy as the supported topology, and
--          add an audit column so future re-tagging is reversible.
--
--   Current reality (verified 2026-05-18 against the live project):
--     cp_customers already have one tenant per customer
--     (`customer_a`, `customer_b`, `customer_c`) and cp_edges /
--     cp_licenses already mirror that. The cross-customer leak we
--     feared was theoretical — what was actually broken was the LOCAL
--     edge writing 'default' into shared-scope rows because activation
--     never persisted customer_id into app_settings (fixed in code
--     commit afa3017) and because the portal customer-creation path
--     didn't enforce a tenant per customer (fixed in code commit
--     0ae195a).
--
--   What this migration does:
--     1. Audit column: cp_customers.ancestor_tenant_id captures the
--        current tenant_id so that any future re-naming is reversible.
--     2. Constraint: refuse a cp_customers insert/update that would
--        leave tenant_id NULL or 'default' if customer_id is set, to
--        keep the per-customer tenancy invariant from drifting.
--
--   What this migration deliberately DOES NOT do:
--     - Rewrite tenant_id on cp_customers / cp_edges / cp_licenses.
--       These rows are already on per-customer tenants; rewriting them
--       would invalidate Supabase Auth JWTs that already encode those
--       tenant strings.
--     - Rewrite historian_readings / plc_readings / live_latest /
--       app_logs. These tables don't carry edge_id and the cloud has
--       no gateway_id→customer mapping. Pre-migration rows stay on
--       tenant_id='default' (master-visible only via lite_is_global_admin).
--       NEW rows from edges running EXE >= 0ae195a will land under the
--       correct per-customer tenant automatically.
--     - Rewrite lite_profiles. The existing rows use tenant slugs
--       ('acmelocal01' etc) that don't match any cp_customers row;
--       these are legacy/orphan accounts. Cleanup is operator work.
--     - Touch report_templates / generated_reports. They have no
--       customer_id; treat them as master-only for now.
--
-- Idempotent. Safe to re-run.
-- ============================================================================

BEGIN;

-- 1. Audit column on cp_customers
ALTER TABLE public.cp_customers
  ADD COLUMN IF NOT EXISTS ancestor_tenant_id text;

UPDATE public.cp_customers
   SET ancestor_tenant_id = tenant_id
 WHERE ancestor_tenant_id IS NULL;

COMMENT ON COLUMN public.cp_customers.ancestor_tenant_id IS
  'Captures the tenant_id at the time of the 2026-05-18 per-customer tenancy hardening migration. Used as a rollback anchor if you ever need to undo a tenant rename.';

-- 2. Invariant: a customer with a customer_id may not be on tenant_id='default'.
--    Use a CHECK constraint that allows NULL customer_id (master-owned rows)
--    but forbids 'default' once customer_id is set.
--    Existing rows are already compliant (none are on 'default'); this just
--    locks the door so a future bad insert can't reopen the cross-customer
--    leak.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
     WHERE table_schema = 'public'
       AND table_name   = 'cp_customers'
       AND constraint_name = 'cp_customers_tenant_per_customer_chk'
  ) THEN
    ALTER TABLE public.cp_customers
      ADD CONSTRAINT cp_customers_tenant_per_customer_chk
      CHECK (tenant_id IS NOT NULL AND tenant_id <> 'default');
  END IF;
END $$;

-- 3. Snapshot block: surface anything that needs operator attention so
--    we never silently leave a footgun in place.
DO $$
DECLARE
  ophan_lite int;
  master_dashboards int;
BEGIN
  SELECT count(*) INTO ophan_lite
    FROM public.lite_profiles
   WHERE tenant_id NOT IN (SELECT tenant_id FROM public.cp_customers);
  IF ophan_lite > 0 THEN
    RAISE NOTICE 'lite_profiles has % row(s) whose tenant_id does not match any cp_customers. Cleanup recommended via portal.', ophan_lite;
  END IF;

  SELECT count(*) INTO master_dashboards
    FROM public.dashboard_configurations
   WHERE tenant_id = 'default';
  IF master_dashboards > 0 THEN
    RAISE NOTICE 'dashboard_configurations has % row(s) on tenant_id=default (master-only). New customer dashboards from upgraded edges will land under the right tenant automatically.', master_dashboards;
  END IF;
END $$;

COMMIT;

-- ============================================================================
-- Operator playbook
-- ============================================================================
--   To verify after applying:
--     SELECT customer_id, tenant_id, ancestor_tenant_id FROM public.cp_customers;
--   To roll back the audit column (only if you really need to):
--     ALTER TABLE public.cp_customers DROP COLUMN ancestor_tenant_id;
--   To remove the constraint (if a master-only row ever needs to live
--   without a customer_id — currently impossible since customer_id is
--   PRIMARY KEY):
--     ALTER TABLE public.cp_customers DROP CONSTRAINT cp_customers_tenant_per_customer_chk;
-- ============================================================================
