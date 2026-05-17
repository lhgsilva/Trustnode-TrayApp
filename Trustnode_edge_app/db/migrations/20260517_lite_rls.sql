-- ============================================================================
-- TrustNode Lite — Row-Level Security migration
-- ============================================================================
-- Date:    2026-05-17
-- Target:  Supabase project (postgres database, `public` schema)
-- Purpose: Lock down all data tables so a browser logged in via Supabase Auth
--          can only read rows belonging to their tenant. The desktop edge and
--          the cloud portal continue to read/write via the service role (which
--          bypasses RLS) so existing behaviour is unchanged.
--
-- This file is IDEMPOTENT. Run it multiple times safely. Apply via the
-- Supabase SQL editor or any psql session with database-owner privileges.
--
-- Design notes:
--   * We do NOT touch any existing `cp_current_tenant()`-based policies.
--     Those are used by the legacy VPS proxy path and must keep working.
--   * Every new policy is named with the `_lite_` prefix so you can grep
--     /drop them cleanly later without affecting the existing security model.
--   * The bridge table `public.lite_profiles` is the only piece of new
--     schema. It maps a Supabase Auth user (auth.users.id) to a tenant.
--     One row per viewer. Created server-side by the portal backend.
--   * The `authenticated` role is the only one we grant browser reads to.
--     The `anon` role gets nothing — anonymous browsers see zero rows.
-- ============================================================================


-- 1. Bridge table -----------------------------------------------------------
-- One row per Supabase Auth user. The portal/edge writes this row whenever a
-- viewer is provisioned. RLS on this table only lets the user read their own
-- row — they must never be able to see another user's tenant assignment.

CREATE TABLE IF NOT EXISTS public.lite_profiles (
  user_id        uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  tenant_id      text NOT NULL,
  customer_id    text,
  username       text,
  email          text,
  role           text NOT NULL DEFAULT 'viewer',
  -- Optional fine-grained edge filter. NULL = all edges in the tenant.
  -- Use TEXT[] so the portal can write a list without touching schema again.
  edge_ids       text[],
  created_utc    timestamptz NOT NULL DEFAULT now(),
  updated_utc    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_lite_profiles_tenant
  ON public.lite_profiles(tenant_id);

COMMENT ON TABLE public.lite_profiles IS
  'Maps a Supabase Auth user to a TrustNode tenant. Source of truth for Lite-app RLS.';


-- 2. Helper function --------------------------------------------------------
-- A SECURITY DEFINER function that returns the current user's tenant. Defined
-- as STABLE so Postgres caches it within a statement; cheap enough to call
-- from every RLS policy.

CREATE OR REPLACE FUNCTION public.lite_current_tenant()
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_catalog
AS $$
  SELECT tenant_id FROM public.lite_profiles WHERE user_id = auth.uid()
$$;

-- Lock the helper down: only authenticated users (real or via JWT) may call it.
REVOKE ALL ON FUNCTION public.lite_current_tenant() FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION public.lite_current_tenant() TO authenticated;

COMMENT ON FUNCTION public.lite_current_tenant() IS
  'Returns the tenant_id of the currently authenticated Lite user (NULL for anon).';


-- 3. Enable RLS where it is currently OFF -----------------------------------
-- These tables already exist with tenant_id columns but no RLS. Turn it on.
-- The desktop / portal backend reads/writes via the service-role key which
-- ALWAYS bypasses RLS, so this is a pure tightening for browser access.

ALTER TABLE public.historian_readings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.historian_readings FORCE ROW LEVEL SECURITY;

ALTER TABLE public.plc_readings       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.plc_readings       FORCE ROW LEVEL SECURITY;

ALTER TABLE public.app_logs           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.app_logs           FORCE ROW LEVEL SECURITY;

ALTER TABLE public.lite_profiles      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.lite_profiles      FORCE ROW LEVEL SECURITY;

-- live_latest already has RLS on, no-op for safety.
ALTER TABLE public.live_latest        ENABLE ROW LEVEL SECURITY;


-- 4. Policies for the new authenticated browser path ------------------------
-- Pattern for every data table: SELECT allowed when the row's tenant_id
-- equals the caller's lite_current_tenant(). DROP first so re-running the
-- migration replaces the policy cleanly.

DROP POLICY IF EXISTS historian_readings_lite_select ON public.historian_readings;
CREATE POLICY historian_readings_lite_select
  ON public.historian_readings
  FOR SELECT
  TO authenticated
  USING (tenant_id IS NOT NULL AND tenant_id = public.lite_current_tenant());

DROP POLICY IF EXISTS plc_readings_lite_select ON public.plc_readings;
CREATE POLICY plc_readings_lite_select
  ON public.plc_readings
  FOR SELECT
  TO authenticated
  USING (tenant_id IS NOT NULL AND tenant_id = public.lite_current_tenant());

DROP POLICY IF EXISTS live_latest_lite_select ON public.live_latest;
CREATE POLICY live_latest_lite_select
  ON public.live_latest
  FOR SELECT
  TO authenticated
  USING (tenant_id IS NOT NULL AND tenant_id = public.lite_current_tenant());

DROP POLICY IF EXISTS app_logs_lite_select ON public.app_logs;
CREATE POLICY app_logs_lite_select
  ON public.app_logs
  FOR SELECT
  TO authenticated
  USING (tenant_id IS NOT NULL AND tenant_id = public.lite_current_tenant());


-- Control-plane tables also need a browser-readable lane. The existing
-- `cp_*_tenant_read` policies depend on the legacy `cp_current_tenant()`
-- GUC and never match a browser session, so we add parallel `_lite_select`
-- policies. They coexist — PostgreSQL OR's policies of the same command.

DROP POLICY IF EXISTS cp_edges_lite_select ON public.cp_edges;
CREATE POLICY cp_edges_lite_select
  ON public.cp_edges
  FOR SELECT
  TO authenticated
  USING (tenant_id = public.lite_current_tenant());

DROP POLICY IF EXISTS cp_customers_lite_select ON public.cp_customers;
CREATE POLICY cp_customers_lite_select
  ON public.cp_customers
  FOR SELECT
  TO authenticated
  USING (tenant_id = public.lite_current_tenant());

DROP POLICY IF EXISTS cp_users_lite_select ON public.cp_users;
CREATE POLICY cp_users_lite_select
  ON public.cp_users
  FOR SELECT
  TO authenticated
  USING (tenant_id = public.lite_current_tenant());

DROP POLICY IF EXISTS cp_user_tenant_memberships_lite_select ON public.cp_user_tenant_memberships;
CREATE POLICY cp_user_tenant_memberships_lite_select
  ON public.cp_user_tenant_memberships
  FOR SELECT
  TO authenticated
  USING (tenant_id = public.lite_current_tenant());


-- lite_profiles: a user can read ONLY their own row. Service role bypasses
-- RLS for writes (the portal backend is the only writer).

DROP POLICY IF EXISTS lite_profiles_self_select ON public.lite_profiles;
CREATE POLICY lite_profiles_self_select
  ON public.lite_profiles
  FOR SELECT
  TO authenticated
  USING (user_id = auth.uid());


-- 5. Grants -----------------------------------------------------------------
-- Postgres needs the role to have SELECT privilege in addition to satisfying
-- the RLS policy. Without these, the policy is never even consulted.

GRANT SELECT ON public.historian_readings          TO authenticated;
GRANT SELECT ON public.plc_readings                TO authenticated;
GRANT SELECT ON public.live_latest                 TO authenticated;
GRANT SELECT ON public.app_logs                    TO authenticated;
GRANT SELECT ON public.cp_edges                    TO authenticated;
GRANT SELECT ON public.cp_customers                TO authenticated;
GRANT SELECT ON public.cp_users                    TO authenticated;
GRANT SELECT ON public.cp_user_tenant_memberships  TO authenticated;
GRANT SELECT ON public.lite_profiles               TO authenticated;

-- Explicitly deny the anonymous role. Belt and braces — by default anon has
-- no grants on our tables, but this makes the intent visible.
REVOKE ALL ON public.historian_readings          FROM anon;
REVOKE ALL ON public.plc_readings                FROM anon;
REVOKE ALL ON public.live_latest                 FROM anon;
REVOKE ALL ON public.app_logs                    FROM anon;
REVOKE ALL ON public.cp_edges                    FROM anon;
REVOKE ALL ON public.cp_customers                FROM anon;
REVOKE ALL ON public.cp_users                    FROM anon;
REVOKE ALL ON public.cp_user_tenant_memberships  FROM anon;
REVOKE ALL ON public.lite_profiles               FROM anon;


-- 6. Self-check (informational) ---------------------------------------------
-- After applying, you can verify with:
--   SELECT relname, relrowsecurity, relforcerowsecurity
--     FROM pg_class
--    WHERE relname IN ('historian_readings','live_latest','app_logs',
--                      'plc_readings','lite_profiles','cp_users',
--                      'cp_user_tenant_memberships','cp_edges','cp_customers')
--    ORDER BY relname;
--
--   SELECT tablename, policyname FROM pg_policies
--    WHERE policyname LIKE '%lite%' OR tablename = 'lite_profiles'
--    ORDER BY tablename, policyname;
--
-- And to test from a browser session you can run, as the authenticated user:
--   SELECT auth.uid(), public.lite_current_tenant();
-- which should return your UUID and your tenant id.
