-- ============================================================================
-- TrustNode Lite — historian_readings realtime push
-- ============================================================================
-- Date:    2026-05-18
-- Purpose: Enable Supabase Realtime on historian_readings so chart widgets
--          in the Lite app can append each new sample in place instead of
--          re-fetching the whole window on a timer.
--
-- The Lite frontend filters its subscription server-side by tag_name +
-- gateway_id, so only the (gateway, tag) pairs actually rendered on
-- screen generate network traffic. RLS still applies — viewers only
-- receive change events for rows their lite_profiles row would let them
-- SELECT.
-- ============================================================================

ALTER TABLE public.historian_readings REPLICA IDENTITY FULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
     WHERE pubname='supabase_realtime'
       AND schemaname='public' AND tablename='historian_readings'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.historian_readings;
  END IF;
END
$$;
