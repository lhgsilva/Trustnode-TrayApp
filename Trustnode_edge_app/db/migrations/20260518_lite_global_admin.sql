-- ============================================================================
-- TrustNode Lite — Master/global admin cross-tenant access
-- ============================================================================
-- Date:    2026-05-18
-- Target:  Supabase project (postgres database, `public` schema)
-- Purpose: Lets the master admin (role=admin AND tenant_id=default in their
--          Supabase JWT user_metadata) see every tenant's lite_profiles and
--          dashboards from the Lite app. Customer-scoped users keep their
--          existing self/tenant-only access — this only ADDS policies.
--
-- Idempotent. Safe to re-run.
--
-- Design notes:
--   * Mirrors the pattern already used by the cp_* tables, where a
--     `cp_current_tenant() = 'default'` clause widens read access for the
--     central admin. We can't reuse cp_current_tenant() because that
--     reads a different JWT claim path; instead define
--     lite_is_global_admin() reading the same user_metadata the Lite
--     JWT carries.
--   * No-op for anon role: only `authenticated` JWTs carry user_metadata.
--   * Adds SELECT-only policies — global admin uses the portal for writes.
-- ============================================================================


-- 1. Helper -----------------------------------------------------------------
-- Reads role + tenant_id out of the JWT's user_metadata claim.
-- The Supabase Auth user object stores these under raw_user_meta_data
-- (server-side) and surfaces them at request time under
-- `request.jwt.claim.user_metadata` after the postgrest auth layer maps
-- it. Newer Supabase puts them at `auth.jwt() -> 'user_metadata'`.
-- Both paths return JSONB; we just check the values.

CREATE OR REPLACE FUNCTION public.lite_is_global_admin()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
AS $$
  -- auth.jwt() returns the full decoded JWT as a JSONB object. We dig
  -- into user_metadata and compare. Any missing piece short-circuits to
  -- false, which is the safe default.
  SELECT
    coalesce((auth.jwt() -> 'user_metadata' ->> 'role'), '') = 'admin'
    AND
    coalesce((auth.jwt() -> 'user_metadata' ->> 'tenant_id'), '') = 'default';
$$;

COMMENT ON FUNCTION public.lite_is_global_admin() IS
  'Returns true when the calling JWT has user_metadata.role=admin AND user_metadata.tenant_id=default. Used by Lite-app RLS to grant cross-tenant SELECT to the master admin.';


-- 2. lite_profiles: add a global-admin SELECT policy -----------------------
-- Existing policy keeps everyone restricted to their own row; this one
-- widens to every row when the caller is the master admin.

DROP POLICY IF EXISTS lite_profiles_global_admin_select ON public.lite_profiles;
CREATE POLICY lite_profiles_global_admin_select
  ON public.lite_profiles
  FOR SELECT
  TO authenticated
  USING (public.lite_is_global_admin());


-- 3. dashboard_configurations: cross-tenant SELECT for master admin -------
DROP POLICY IF EXISTS dashboard_configurations_global_admin_select ON public.dashboard_configurations;
CREATE POLICY dashboard_configurations_global_admin_select
  ON public.dashboard_configurations
  FOR SELECT
  TO authenticated
  USING (public.lite_is_global_admin());


-- 4. Telemetry / data tables: cross-tenant SELECT for master admin --------
-- These are what makes the "master picks any customer + sees their live
-- data" use case work. Without these, the master could enumerate tenant
-- IDs but not actually read a tenant's data.

DROP POLICY IF EXISTS plc_readings_global_admin_select ON public.plc_readings;
CREATE POLICY plc_readings_global_admin_select
  ON public.plc_readings
  FOR SELECT
  TO authenticated
  USING (public.lite_is_global_admin());

DROP POLICY IF EXISTS historian_readings_global_admin_select ON public.historian_readings;
CREATE POLICY historian_readings_global_admin_select
  ON public.historian_readings
  FOR SELECT
  TO authenticated
  USING (public.lite_is_global_admin());

DROP POLICY IF EXISTS live_latest_global_admin_select ON public.live_latest;
CREATE POLICY live_latest_global_admin_select
  ON public.live_latest
  FOR SELECT
  TO authenticated
  USING (public.lite_is_global_admin());


-- 5. Lookups for the customer/edge picker ---------------------------------
-- Lite needs to render a "pick customer / pick edge" UI for the master
-- admin. The cp_customers / cp_edges tables already have lite_select
-- policies scoped to lite_current_tenant() — we add a parallel one for
-- the global admin.

DROP POLICY IF EXISTS cp_customers_global_admin_select ON public.cp_customers;
CREATE POLICY cp_customers_global_admin_select
  ON public.cp_customers
  FOR SELECT
  TO authenticated
  USING (public.lite_is_global_admin());

DROP POLICY IF EXISTS cp_edges_global_admin_select ON public.cp_edges;
CREATE POLICY cp_edges_global_admin_select
  ON public.cp_edges
  FOR SELECT
  TO authenticated
  USING (public.lite_is_global_admin());


-- 6. Alarms / reporting tables --------------------------------------------
DROP POLICY IF EXISTS alarms_setup_global_admin_select ON public.alarms_setup;
CREATE POLICY alarms_setup_global_admin_select
  ON public.alarms_setup
  FOR SELECT
  TO authenticated
  USING (public.lite_is_global_admin());

DROP POLICY IF EXISTS triggers_limits_global_admin_select ON public.triggers_limits;
CREATE POLICY triggers_limits_global_admin_select
  ON public.triggers_limits
  FOR SELECT
  TO authenticated
  USING (public.lite_is_global_admin());

DROP POLICY IF EXISTS report_templates_global_admin_select ON public.report_templates;
CREATE POLICY report_templates_global_admin_select
  ON public.report_templates
  FOR SELECT
  TO authenticated
  USING (public.lite_is_global_admin());

DROP POLICY IF EXISTS generated_reports_global_admin_select ON public.generated_reports;
CREATE POLICY generated_reports_global_admin_select
  ON public.generated_reports
  FOR SELECT
  TO authenticated
  USING (public.lite_is_global_admin());

-- ============================================================================
-- End of migration. After applying, verify with:
--   SELECT public.lite_is_global_admin();   -- as the master JWT
-- which should return TRUE for master and FALSE for tenant users.
-- ============================================================================
