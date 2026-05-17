-- ============================================================================
-- TrustNode Lite — dashboard widget configuration mirror
-- ============================================================================
-- Date:    2026-05-17
-- Target:  Supabase project (postgres database, `public` schema)
-- Purpose: Give the Lite app read access to the widget layout the operator
--          designed in the desktop edge app, scoped to their tenant via RLS.
--
-- The edge keeps the authoritative copy in its local SQLite
-- (config_documents.dashboard_configurations + the scoped variant per user).
-- We add ONE row per (tenant_id, scope_key) here, mirroring the same JSON
-- payload. The Lite app SELECTs the row for its tenant and renders.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.dashboard_configurations (
  tenant_id    text NOT NULL,
  scope_key    text NOT NULL DEFAULT '',
  payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  version      integer NOT NULL DEFAULT 1,
  updated_utc  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, scope_key)
);

CREATE INDEX IF NOT EXISTS ix_dashboard_configurations_tenant
  ON public.dashboard_configurations(tenant_id);

COMMENT ON TABLE public.dashboard_configurations IS
  'Mirror of the edge''s config_documents.dashboard_configurations; one row per tenant + optional scope_key.';

ALTER TABLE public.dashboard_configurations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.dashboard_configurations FORCE ROW LEVEL SECURITY;

-- Lite viewers can read rows matching their tenant.
DROP POLICY IF EXISTS dashboard_configurations_lite_select ON public.dashboard_configurations;
CREATE POLICY dashboard_configurations_lite_select
  ON public.dashboard_configurations
  FOR SELECT
  TO authenticated
  USING (tenant_id = public.lite_current_tenant());

GRANT SELECT ON public.dashboard_configurations TO authenticated;
REVOKE ALL ON public.dashboard_configurations FROM anon;

-- Enrol in supabase_realtime so the Lite app can subscribe and re-render
-- whenever the operator saves a new widget layout in the desktop.
ALTER TABLE public.dashboard_configurations REPLICA IDENTITY FULL;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
     WHERE pubname='supabase_realtime'
       AND schemaname='public' AND tablename='dashboard_configurations'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.dashboard_configurations;
  END IF;
END
$$;
