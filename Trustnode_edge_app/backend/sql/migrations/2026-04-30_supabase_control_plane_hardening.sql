-- 2026-04-30_supabase_control_plane_hardening.sql
-- Purpose:
-- 1) Keep current app compatibility (public tables stay in place)
-- 2) Harden tenant isolation and role access for Supabase
-- 3) Seed module catalog and customer domain mapping primitives

BEGIN;

-- Functional schemas for governance/documentation and future object placement.
CREATE SCHEMA IF NOT EXISTS control_plane;
CREATE SCHEMA IF NOT EXISTS telemetry;
CREATE SCHEMA IF NOT EXISTS iam;
CREATE SCHEMA IF NOT EXISTS audit;

-- Resolve tenant from JWT claim in a single reusable SQL function.
CREATE OR REPLACE FUNCTION public.cp_current_tenant()
RETURNS TEXT
LANGUAGE sql
STABLE
AS $$
  SELECT NULLIF(current_setting('request.jwt.claim.tenant_id', true), '');
$$;

-- Optional domain table for customer URL routing.
CREATE TABLE IF NOT EXISTS public.cp_customer_domains (
  id BIGSERIAL PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  customer_id TEXT NOT NULL,
  domain TEXT NOT NULL,
  is_primary BOOLEAN NOT NULL DEFAULT FALSE,
  status TEXT NOT NULL DEFAULT 'active',
  created_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (domain),
  UNIQUE (tenant_id, customer_id, domain)
);

CREATE INDEX IF NOT EXISTS ix_cp_customer_domains_tenant_customer
  ON public.cp_customer_domains(tenant_id, customer_id);

-- Module catalog, used by licenses and customer entitlement UX.
CREATE TABLE IF NOT EXISTS public.cp_module_catalog (
  module_key TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  default_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  status TEXT NOT NULL DEFAULT 'active',
  created_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_utc TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO public.cp_module_catalog(module_key, label, default_enabled)
VALUES
  ('dashboard', 'Dashboard', TRUE),
  ('power_overview', 'Power Management Overview', TRUE),
  ('historian', 'Historian', TRUE),
  ('reporting', 'Reporting', TRUE),
  ('alarms', 'Alarms', TRUE),
  ('interface', 'Interface', TRUE),
  ('tags', 'Tags', FALSE),
  ('gateway_configuration', 'Gateway Configuration', FALSE),
  ('gateway_runtime_control', 'Gateway Runtime Control', FALSE),
  ('database', 'Database', FALSE),
  ('users_and_access_control', 'Users and Access Control', FALSE)
ON CONFLICT (module_key) DO UPDATE
SET
  label = EXCLUDED.label,
  default_enabled = EXCLUDED.default_enabled,
  status = 'active',
  updated_utc = now();

-- Edge-to-license explicit association.
CREATE TABLE IF NOT EXISTS public.cp_edge_licenses (
  id BIGSERIAL PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  edge_id TEXT NOT NULL,
  license_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, edge_id, license_id)
);

CREATE INDEX IF NOT EXISTS ix_cp_edge_licenses_tenant_edge
  ON public.cp_edge_licenses(tenant_id, edge_id);

-- Ensure RLS is enabled on all tenant-sensitive control-plane tables.
ALTER TABLE public.cp_tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cp_customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cp_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cp_licenses ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cp_license_modules ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cp_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cp_user_tenant_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cp_edge_activation_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cp_password_reset_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cp_security_audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cp_customer_domains ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cp_module_catalog ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cp_edge_licenses ENABLE ROW LEVEL SECURITY;

-- Drop legacy policies if they exist; recreate deterministic policy set.
DO $$
DECLARE
  pol RECORD;
BEGIN
  FOR pol IN
    SELECT schemaname, tablename, policyname
    FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename IN (
        'cp_tenants','cp_customers','cp_edges','cp_licenses','cp_license_modules',
        'cp_users','cp_user_tenant_memberships','cp_edge_activation_codes',
        'cp_password_reset_events','cp_security_audit_log','cp_customer_domains',
        'cp_module_catalog','cp_edge_licenses'
      )
  LOOP
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I.%I', pol.policyname, pol.schemaname, pol.tablename);
  END LOOP;
END $$;

-- Tenant read access.
CREATE POLICY cp_tenants_tenant_read
  ON public.cp_tenants
  FOR SELECT
  USING (
    tenant_id = public.cp_current_tenant()
    OR public.cp_current_tenant() = 'default'
  );

CREATE POLICY cp_customers_tenant_read
  ON public.cp_customers
  FOR SELECT
  USING (
    tenant_id = public.cp_current_tenant()
    OR public.cp_current_tenant() = 'default'
  );

CREATE POLICY cp_edges_tenant_read
  ON public.cp_edges
  FOR SELECT
  USING (
    tenant_id = public.cp_current_tenant()
    OR public.cp_current_tenant() = 'default'
  );

CREATE POLICY cp_licenses_tenant_read
  ON public.cp_licenses
  FOR SELECT
  USING (
    tenant_id = public.cp_current_tenant()
    OR public.cp_current_tenant() = 'default'
  );

CREATE POLICY cp_license_modules_tenant_read
  ON public.cp_license_modules
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1
      FROM public.cp_licenses l
      WHERE l.license_id = cp_license_modules.license_id
        AND (
          l.tenant_id = public.cp_current_tenant()
          OR public.cp_current_tenant() = 'default'
        )
    )
  );

CREATE POLICY cp_users_tenant_read
  ON public.cp_users
  FOR SELECT
  USING (
    tenant_id = public.cp_current_tenant()
    OR public.cp_current_tenant() = 'default'
  );

CREATE POLICY cp_user_tenant_memberships_tenant_read
  ON public.cp_user_tenant_memberships
  FOR SELECT
  USING (
    tenant_id = public.cp_current_tenant()
    OR public.cp_current_tenant() = 'default'
  );

CREATE POLICY cp_edge_activation_codes_tenant_read
  ON public.cp_edge_activation_codes
  FOR SELECT
  USING (
    tenant_id = public.cp_current_tenant()
    OR public.cp_current_tenant() = 'default'
  );

CREATE POLICY cp_password_reset_events_tenant_read
  ON public.cp_password_reset_events
  FOR SELECT
  USING (
    tenant_id = public.cp_current_tenant()
    OR public.cp_current_tenant() = 'default'
  );

CREATE POLICY cp_security_audit_log_tenant_read
  ON public.cp_security_audit_log
  FOR SELECT
  USING (
    tenant_id = public.cp_current_tenant()
    OR public.cp_current_tenant() = 'default'
  );

CREATE POLICY cp_customer_domains_tenant_read
  ON public.cp_customer_domains
  FOR SELECT
  USING (
    tenant_id = public.cp_current_tenant()
    OR public.cp_current_tenant() = 'default'
  );

CREATE POLICY cp_edge_licenses_tenant_read
  ON public.cp_edge_licenses
  FOR SELECT
  USING (
    tenant_id = public.cp_current_tenant()
    OR public.cp_current_tenant() = 'default'
  );

-- Module catalog can be read by all authenticated JWT users.
CREATE POLICY cp_module_catalog_read
  ON public.cp_module_catalog
  FOR SELECT
  USING (public.cp_current_tenant() IS NOT NULL);

-- Writes are restricted to backend/service-role path by tenant check.
-- (service role bypasses RLS in Supabase; these policies protect accidental direct JWT writes).
CREATE POLICY cp_tenants_tenant_write
  ON public.cp_tenants
  FOR ALL
  USING (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default')
  WITH CHECK (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default');

CREATE POLICY cp_customers_tenant_write
  ON public.cp_customers
  FOR ALL
  USING (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default')
  WITH CHECK (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default');

CREATE POLICY cp_edges_tenant_write
  ON public.cp_edges
  FOR ALL
  USING (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default')
  WITH CHECK (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default');

CREATE POLICY cp_licenses_tenant_write
  ON public.cp_licenses
  FOR ALL
  USING (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default')
  WITH CHECK (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default');

CREATE POLICY cp_users_tenant_write
  ON public.cp_users
  FOR ALL
  USING (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default')
  WITH CHECK (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default');

CREATE POLICY cp_user_tenant_memberships_tenant_write
  ON public.cp_user_tenant_memberships
  FOR ALL
  USING (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default')
  WITH CHECK (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default');

CREATE POLICY cp_edge_activation_codes_tenant_write
  ON public.cp_edge_activation_codes
  FOR ALL
  USING (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default')
  WITH CHECK (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default');

CREATE POLICY cp_password_reset_events_tenant_write
  ON public.cp_password_reset_events
  FOR ALL
  USING (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default')
  WITH CHECK (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default');

CREATE POLICY cp_security_audit_log_tenant_write
  ON public.cp_security_audit_log
  FOR ALL
  USING (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default')
  WITH CHECK (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default');

CREATE POLICY cp_customer_domains_tenant_write
  ON public.cp_customer_domains
  FOR ALL
  USING (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default')
  WITH CHECK (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default');

CREATE POLICY cp_edge_licenses_tenant_write
  ON public.cp_edge_licenses
  FOR ALL
  USING (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default')
  WITH CHECK (tenant_id = public.cp_current_tenant() OR public.cp_current_tenant() = 'default');

-- Browser roles should not touch control-plane tables directly.
REVOKE ALL ON public.cp_tenants FROM anon, authenticated;
REVOKE ALL ON public.cp_customers FROM anon, authenticated;
REVOKE ALL ON public.cp_edges FROM anon, authenticated;
REVOKE ALL ON public.cp_licenses FROM anon, authenticated;
REVOKE ALL ON public.cp_license_modules FROM anon, authenticated;
REVOKE ALL ON public.cp_users FROM anon, authenticated;
REVOKE ALL ON public.cp_user_tenant_memberships FROM anon, authenticated;
REVOKE ALL ON public.cp_edge_activation_codes FROM anon, authenticated;
REVOKE ALL ON public.cp_password_reset_events FROM anon, authenticated;
REVOKE ALL ON public.cp_security_audit_log FROM anon, authenticated;
REVOKE ALL ON public.cp_customer_domains FROM anon, authenticated;
REVOKE ALL ON public.cp_edge_licenses FROM anon, authenticated;
REVOKE ALL ON public.cp_module_catalog FROM anon, authenticated;

COMMIT;
