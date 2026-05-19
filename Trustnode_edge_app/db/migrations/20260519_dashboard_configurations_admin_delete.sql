-- ============================================================================
-- TrustNode Portal — admin delete policy for dashboard_configurations
-- ============================================================================
-- Date:    2026-05-19
-- Target:  Supabase project (postgres database, `public` schema)
-- Purpose: Let portal admins delete stale dashboard profile rows. Without
--          this policy the portal's new "Dashboard Profiles" management
--          page can list rows (lite_select grants that) but cannot remove
--          them — every DELETE fails RLS.
--
-- Policy scope: same as other admin write policies. A row is deletable when
--   - the caller is a tenant admin matching the row's tenant_id, OR
--   - the caller is the global master admin (lite_is_global_admin()).
-- ============================================================================

DROP POLICY IF EXISTS dashboard_configurations_admin_delete ON public.dashboard_configurations;
CREATE POLICY dashboard_configurations_admin_delete
  ON public.dashboard_configurations
  FOR DELETE
  TO authenticated
  USING (
    public.lite_is_global_admin()
    OR tenant_id = public.lite_current_tenant()
  );

GRANT DELETE ON public.dashboard_configurations TO authenticated;
