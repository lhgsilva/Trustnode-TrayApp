-- ============================================================================
-- TrustNode Lite — report template mirror
-- ============================================================================
-- Date:    2026-05-18
-- Purpose: Let the Lite app list the report templates the operator built
--          in the desktop. The actual report rendering still happens on
--          the edge (PDF generation, scheduling, etc.); Lite shows the
--          catalogue and will eventually expose a "request this report"
--          button that calls back to the edge.
--
-- IDEMPOTENT. Safe to re-run.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.report_templates (
  id              text NOT NULL,
  tenant_id       text NOT NULL DEFAULT 'default',
  name            text NOT NULL,
  description     text,
  definition_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_utc     timestamptz NOT NULL DEFAULT now(),
  updated_utc     timestamptz NOT NULL DEFAULT now(),
  created_by      text,
  PRIMARY KEY (tenant_id, id)
);

CREATE INDEX IF NOT EXISTS ix_report_templates_tenant
  ON public.report_templates(tenant_id);

COMMENT ON TABLE public.report_templates IS
  'Mirror of the edge''s report_templates table. Listed by the Lite app.';

ALTER TABLE public.report_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.report_templates FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS report_templates_lite_select ON public.report_templates;
CREATE POLICY report_templates_lite_select
  ON public.report_templates
  FOR SELECT
  TO authenticated
  USING (tenant_id = public.lite_current_tenant());

GRANT SELECT ON public.report_templates TO authenticated;
REVOKE ALL ON public.report_templates FROM anon;

ALTER TABLE public.report_templates REPLICA IDENTITY FULL;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
     WHERE pubname='supabase_realtime'
       AND schemaname='public' AND tablename='report_templates'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.report_templates;
  END IF;
END
$$;
