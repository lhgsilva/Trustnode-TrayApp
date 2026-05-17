-- ============================================================================
-- Lite — allow authenticated SELECT on lite-reports bucket
-- ============================================================================
-- Date:    2026-05-18
-- Purpose: Without an explicit allow policy on storage.objects, authenticated
--          Lite users can't sign URLs for the PDFs they're allowed to see
--          (RLS denies by default). Scope to the lite-reports bucket and
--          require the object path to start with the user's tenant id, so a
--          tenant can never reach another tenant's PDF.
--
-- IDEMPOTENT. Safe to re-run.
-- ============================================================================

DROP POLICY IF EXISTS lite_reports_authenticated_select ON storage.objects;
CREATE POLICY lite_reports_authenticated_select
  ON storage.objects
  FOR SELECT
  TO authenticated
  USING (
    bucket_id = 'lite-reports'
    AND (storage.foldername(name))[1] = public.lite_current_tenant()
  );
