-- ============================================================================
-- TrustNode historian — composite index for Lite tail queries
-- ============================================================================
-- Date:    2026-06-11
-- Target:  Supabase project (already applied)
--
-- Lite charts and the edge dashboard both issue queries of the shape:
--
--   SELECT ts_utc, value, ...
--   FROM historian_readings
--   WHERE tag_name = ? AND gateway_id = ?
--   ORDER BY ts_utc DESC
--   LIMIT ?
--
-- Before this index the planner used idx_hist_ts (ts_utc DESC only) and
-- had to scan many rows just to filter for the matching tag/gateway pair
-- — for SimREAL[3] / gw-1781124704421, Postgres scanned ~6500 rows just
-- to return 500. As history grows that gets worse.
--
-- A (tag_name, gateway_id, ts_utc DESC) composite index lets the planner
-- jump straight to the right (tag, gateway) partition and walk it
-- backwards in time, returning exactly LIMIT rows with no filter step.
--
-- Verified plan after creation:
--   Index Scan using ix_hist_tag_gw_ts on historian_readings
--     Index Cond: ((tag_name = ?) AND (gateway_id = ?))
--   Execution Time: 4.4 ms for LIMIT 500
-- ============================================================================

CREATE INDEX IF NOT EXISTS ix_hist_tag_gw_ts
    ON public.historian_readings (tag_name, gateway_id, ts_utc DESC);

-- Keep planner statistics fresh after creating a new index on a
-- heavily-read table.
ANALYZE public.historian_readings;
