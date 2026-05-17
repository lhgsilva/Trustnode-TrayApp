-- ============================================================================
-- TrustNode Lite — alarms_setup + generated_reports + lite_report_requests
-- ============================================================================
-- Date:    2026-05-18
-- Purpose: Three additions for the Lite app:
--   1. Mirror of the edge's `alarms_setup` config doc so Lite can render
--      the configured alarm rules and evaluate them against live_latest.
--   2. Mirror of the edge's `generated_reports` table + a `lite-reports`
--      Storage bucket so Lite can list, preview, and download PDFs.
--   3. A `lite_report_requests` queue the Lite app writes into when a
--      viewer asks to generate a report; the edge backend polls this
--      table, renders the report, uploads the PDF to Storage, and
--      updates the request status.
--
-- IDEMPOTENT. Safe to re-run.
-- ============================================================================


-- ---- 1. alarms_setup mirror ----------------------------------------------
CREATE TABLE IF NOT EXISTS public.alarms_setup (
  tenant_id    text NOT NULL,
  scope_key    text NOT NULL DEFAULT '',
  payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  version      integer NOT NULL DEFAULT 1,
  updated_utc  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, scope_key)
);
CREATE INDEX IF NOT EXISTS ix_alarms_setup_tenant ON public.alarms_setup(tenant_id);

ALTER TABLE public.alarms_setup ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.alarms_setup FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS alarms_setup_lite_select ON public.alarms_setup;
CREATE POLICY alarms_setup_lite_select
  ON public.alarms_setup
  FOR SELECT TO authenticated
  USING (tenant_id = public.lite_current_tenant());

GRANT SELECT ON public.alarms_setup TO authenticated;
REVOKE ALL ON public.alarms_setup FROM anon;

ALTER TABLE public.alarms_setup REPLICA IDENTITY FULL;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_publication_tables WHERE pubname='supabase_realtime' AND tablename='alarms_setup') THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.alarms_setup;
  END IF;
END $$;


-- ---- 1b. triggers_limits mirror (alarm rules) ----------------------------
-- The Lite Alarms view reads `payload_json.trigger_rules[]` here and
-- evaluates each rule against the live values. Same shape as alarms_setup.
CREATE TABLE IF NOT EXISTS public.triggers_limits (
  tenant_id    text NOT NULL,
  scope_key    text NOT NULL DEFAULT '',
  payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  version      integer NOT NULL DEFAULT 1,
  updated_utc  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, scope_key)
);
CREATE INDEX IF NOT EXISTS ix_triggers_limits_tenant ON public.triggers_limits(tenant_id);

ALTER TABLE public.triggers_limits ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.triggers_limits FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS triggers_limits_lite_select ON public.triggers_limits;
CREATE POLICY triggers_limits_lite_select
  ON public.triggers_limits
  FOR SELECT TO authenticated
  USING (tenant_id = public.lite_current_tenant());

GRANT SELECT ON public.triggers_limits TO authenticated;
REVOKE ALL ON public.triggers_limits FROM anon;

ALTER TABLE public.triggers_limits REPLICA IDENTITY FULL;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_publication_tables WHERE pubname='supabase_realtime' AND tablename='triggers_limits') THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.triggers_limits;
  END IF;
END $$;


-- ---- 2. generated_reports mirror -----------------------------------------
-- Mirrors the edge's `generated_reports` rows. `storage_path` is the
-- object key inside the `lite-reports` Storage bucket — set by the edge
-- after the upload succeeds. The Lite app uses Supabase Storage's
-- createSignedUrl() to get a short-lived preview/download link.

CREATE TABLE IF NOT EXISTS public.generated_reports (
  id              text NOT NULL,
  tenant_id       text NOT NULL DEFAULT 'default',
  template_id     text,
  template_name   text,
  schedule_id     text,
  schedule_name   text,
  triggered_by    text NOT NULL DEFAULT 'manual',
  file_name       text NOT NULL,
  file_bytes      bigint NOT NULL DEFAULT 0,
  file_sha256     text,
  storage_path    text,                    -- key inside the lite-reports bucket
  email_status    text,
  email_message   text,
  email_recipients_json jsonb,
  meta_json       jsonb,
  created_utc     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS ix_generated_reports_tenant_created
  ON public.generated_reports(tenant_id, created_utc DESC);

ALTER TABLE public.generated_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.generated_reports FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS generated_reports_lite_select ON public.generated_reports;
CREATE POLICY generated_reports_lite_select
  ON public.generated_reports
  FOR SELECT TO authenticated
  USING (tenant_id = public.lite_current_tenant());

GRANT SELECT ON public.generated_reports TO authenticated;
REVOKE ALL ON public.generated_reports FROM anon;

ALTER TABLE public.generated_reports REPLICA IDENTITY FULL;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_publication_tables WHERE pubname='supabase_realtime' AND tablename='generated_reports') THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.generated_reports;
  END IF;
END $$;


-- ---- 3. lite_report_requests queue ---------------------------------------
-- Lite inserts a request when the viewer clicks "Generate". The edge
-- backend polls this table, runs the template, uploads the PDF, links
-- the result to a generated_reports row, and flips status to 'done'.

CREATE TABLE IF NOT EXISTS public.lite_report_requests (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       text NOT NULL,
  requested_by    uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  requester_email text,
  template_id     text NOT NULL,
  template_name   text,
  status          text NOT NULL DEFAULT 'pending',  -- pending | running | done | failed
  error_message   text,
  generated_id    text,                              -- FK-ish to generated_reports.id
  requested_utc   timestamptz NOT NULL DEFAULT now(),
  started_utc     timestamptz,
  finished_utc    timestamptz,
  meta_json       jsonb
);
CREATE INDEX IF NOT EXISTS ix_lite_report_requests_tenant
  ON public.lite_report_requests(tenant_id, requested_utc DESC);
CREATE INDEX IF NOT EXISTS ix_lite_report_requests_status
  ON public.lite_report_requests(status, requested_utc) WHERE status = 'pending';

ALTER TABLE public.lite_report_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.lite_report_requests FORCE ROW LEVEL SECURITY;

-- A signed-in viewer can read their own request rows.
DROP POLICY IF EXISTS lite_report_requests_self_select ON public.lite_report_requests;
CREATE POLICY lite_report_requests_self_select
  ON public.lite_report_requests
  FOR SELECT TO authenticated
  USING (tenant_id = public.lite_current_tenant());

-- A signed-in viewer can INSERT a new request scoped to their tenant.
DROP POLICY IF EXISTS lite_report_requests_self_insert ON public.lite_report_requests;
CREATE POLICY lite_report_requests_self_insert
  ON public.lite_report_requests
  FOR INSERT TO authenticated
  WITH CHECK (
    tenant_id = public.lite_current_tenant()
    AND requested_by = auth.uid()
  );

GRANT SELECT, INSERT ON public.lite_report_requests TO authenticated;
REVOKE ALL ON public.lite_report_requests FROM anon;

ALTER TABLE public.lite_report_requests REPLICA IDENTITY FULL;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_publication_tables WHERE pubname='supabase_realtime' AND tablename='lite_report_requests') THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.lite_report_requests;
  END IF;
END $$;


-- ---- 4. Storage bucket for PDFs ------------------------------------------
-- Private bucket. Edge uploads with the service-role key; Lite gets a
-- signed URL via supabase.storage.from('lite-reports').createSignedUrl().
INSERT INTO storage.buckets (id, name, public)
  VALUES ('lite-reports', 'lite-reports', false)
  ON CONFLICT (id) DO NOTHING;

-- RLS on storage.objects: signed-URL downloads bypass RLS, but for any
-- direct authenticated reads we still scope by tenant via the object's
-- path prefix `<tenant_id>/...`. Keep it simple: deny all to anon and
-- authenticated; only the service role (edge backend) writes.
DROP POLICY IF EXISTS lite_reports_no_anon ON storage.objects;
CREATE POLICY lite_reports_no_anon ON storage.objects FOR ALL TO anon USING (false);
