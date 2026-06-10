-- ============================================================================
-- TrustNode Lite + Portal — gateway_configurations and devices mirror
-- ============================================================================
-- Date:    2026-06-10
-- Target:  Supabase project (postgres database, `public` schema)
-- Purpose: Extend the edge → cloud config mirror to include the
--          gateway list and device list. Lite needs gateway_configurations
--          so widgets can resolve friendly names and the historian view
--          can filter by gateway; the portal needs both to show what each
--          edge actually has deployed without bouncing through the local
--          API.
--
-- Schema mirrors the existing dashboard_configurations table shape so the
-- backend `_mirror_config_doc_to_cloud` helper can reuse its INSERT/UPSERT
-- template unchanged (tenant_id, scope_key, payload_json, version, updated_utc).
-- ============================================================================

-- gateway_configurations ----------------------------------------------------
CREATE TABLE IF NOT EXISTS public.gateway_configurations (
  tenant_id    text NOT NULL,
  scope_key    text NOT NULL DEFAULT '',
  payload_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  version      integer NOT NULL DEFAULT 1,
  updated_utc  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, scope_key)
);

CREATE INDEX IF NOT EXISTS ix_gateway_configurations_tenant
  ON public.gateway_configurations(tenant_id);

COMMENT ON TABLE public.gateway_configurations IS
  'Mirror of the edge''s config_documents.gateway_configurations; one row per tenant + scope_key.';

ALTER TABLE public.gateway_configurations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.gateway_configurations FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS gateway_configurations_lite_select ON public.gateway_configurations;
CREATE POLICY gateway_configurations_lite_select
  ON public.gateway_configurations
  FOR SELECT
  TO authenticated
  USING (
    public.lite_is_global_admin()
    OR tenant_id = public.lite_current_tenant()
  );

GRANT SELECT ON public.gateway_configurations TO authenticated;
REVOKE ALL ON public.gateway_configurations FROM anon;

-- Realtime enrolment so widget labels refresh when the operator renames
-- a gateway at the edge.
ALTER TABLE public.gateway_configurations REPLICA IDENTITY FULL;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
     WHERE pubname='supabase_realtime'
       AND schemaname='public' AND tablename='gateway_configurations'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.gateway_configurations;
  END IF;
END
$$;

-- devices --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.devices (
  tenant_id    text NOT NULL,
  scope_key    text NOT NULL DEFAULT '',
  payload_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  version      integer NOT NULL DEFAULT 1,
  updated_utc  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, scope_key)
);

CREATE INDEX IF NOT EXISTS ix_devices_tenant
  ON public.devices(tenant_id);

COMMENT ON TABLE public.devices IS
  'Mirror of the edge''s config_documents.devices; one row per tenant + scope_key.';

ALTER TABLE public.devices ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.devices FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS devices_lite_select ON public.devices;
CREATE POLICY devices_lite_select
  ON public.devices
  FOR SELECT
  TO authenticated
  USING (
    public.lite_is_global_admin()
    OR tenant_id = public.lite_current_tenant()
  );

GRANT SELECT ON public.devices TO authenticated;
REVOKE ALL ON public.devices FROM anon;

ALTER TABLE public.devices REPLICA IDENTITY FULL;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
     WHERE pubname='supabase_realtime'
       AND schemaname='public' AND tablename='devices'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.devices;
  END IF;
END
$$;
