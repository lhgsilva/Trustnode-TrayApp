-- ============================================================================
-- TrustNode Lite — Realtime push enrolment
-- ============================================================================
-- Date:    2026-05-17
-- Target:  Supabase project (postgres database, `public` schema)
-- Purpose: Enrol the live data tables in Supabase's `supabase_realtime`
--          publication so the Lite app can subscribe via WebSocket and
--          receive new rows the moment they land — instead of polling
--          PostgREST every 2 s.
--
--          Realtime respects RLS: a subscribed client only receives change
--          notifications for rows their `lite_profiles` row would allow them
--          to SELECT. No data leaks.
--
-- This file is IDEMPOTENT. Re-running it is safe.
-- ============================================================================


-- 1. REPLICA IDENTITY ---------------------------------------------------------
-- Supabase Realtime needs the row's old/new state to compute deltas. The
-- default REPLICA IDENTITY is the primary key only, which is fine for
-- live_latest (its PK is gateway+tag+tenant) — but FULL gives subscribers the
-- complete new row, which is what the Lite UI actually consumes.

ALTER TABLE public.live_latest         REPLICA IDENTITY FULL;
ALTER TABLE public.historian_readings  REPLICA IDENTITY FULL;
ALTER TABLE public.app_logs            REPLICA IDENTITY FULL;


-- 2. Enrol in supabase_realtime publication ----------------------------------
-- Idempotent: skip if the table is already in the publication.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
     WHERE pubname='supabase_realtime'
       AND schemaname='public' AND tablename='live_latest'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.live_latest;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
     WHERE pubname='supabase_realtime'
       AND schemaname='public' AND tablename='app_logs'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.app_logs;
  END IF;

  -- historian_readings is intentionally NOT subscribed: every PLC scan inserts
  -- a new row, so a WS stream for 100 tags at 10 Hz = 1000 events/sec per
  -- browser. The Lite UI only needs live_latest for tile freshness and the
  -- chart-drawer's range query is a one-shot REST call. If you later add a
  -- "live chart" feature, enrol historian_readings here too — RLS already
  -- protects it.
END
$$;


-- 3. Self-check (informational) ---------------------------------------------
-- After applying:
--   SELECT schemaname, tablename
--     FROM pg_publication_tables
--    WHERE pubname='supabase_realtime'
--    ORDER BY 1, 2;
--
-- Then in the Supabase dashboard:
--   Project Settings -> API -> Realtime should show live_latest and app_logs.
