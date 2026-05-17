-- ============================================================================
-- Local edge SQLite — historian index dedupe + enable 7-day raw retention
-- ============================================================================
-- Date:    2026-05-18
-- Purpose: The historian table carried 8 indexes; 4 were redundant duplicates
--          or subsumed by composite indexes. Each duplicate adds ~250-350 B
--          per row on disk and a write penalty on every insert. Dropping them
--          cuts per-row cost roughly in half with no query plan change (the
--          covering indexes that remain handle every existing query path).
--
--          Also flips retention_policy.enabled from 0 to 1 with a 7-day raw
--          window — without this the historian table grows unbounded
--          (~245 GB/month at 50 tags @ 1 Hz). Aggregates (minute/hour/day)
--          still keep older data at much lower cost via the same daemon.
--
-- This script is hand-applied via sqlite3 directly (it is not auto-run by
-- AppStore boot because we don't want startup time spent on a migration).
-- ============================================================================

-- ---- Indexes ---------------------------------------------------------------
-- Subsumed by idx_hist_tenant_ts (tenant_id, ts_utc DESC)
DROP INDEX IF EXISTS idx_hist_ts;

-- Subsumed by idx_hist_tenant_tag_ts (tenant_id, tag_name, ts_utc DESC)
DROP INDEX IF EXISTS idx_hist_tag;

-- Subsumed by idx_hist_tenant_gwid_tag_ts (tenant_id, gateway_id, tag_name, ts_utc DESC)
DROP INDEX IF EXISTS idx_hist_gateway;

-- Exact duplicate of idx_hist_tenant_gwid_tag_ts — same columns, same order.
DROP INDEX IF EXISTS idx_hist_tenant_gw_tag_ts;


-- ---- Retention -------------------------------------------------------------
-- Defaults preserved (7d raw / 30d minute / 180d hour / 730d day) but the
-- daemon is now armed.
UPDATE retention_policy
   SET enabled     = 1,
       updated_utc = strftime('%Y-%m-%d %H:%M:%f', 'now')
 WHERE id = 1;
