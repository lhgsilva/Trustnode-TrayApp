-- 2026-06-17 — Per-user Lite view-link tokens.
--
-- Adds a nullable user_id column to cp_edge_view_links so admins can
-- mint one read-only Lite token per user from the Users page (NULL =
-- legacy edge-wide token, non-NULL = per-user token). Backwards
-- compatible: existing rows keep working as edge-wide links.

ALTER TABLE IF EXISTS public.cp_edge_view_links
  ADD COLUMN IF NOT EXISTS user_id text;

CREATE INDEX IF NOT EXISTS ix_cp_view_links_edge_user
  ON public.cp_edge_view_links(tenant_id, edge_id, user_id)
  WHERE user_id IS NOT NULL;
