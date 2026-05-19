-- ============================================================================
-- TrustNode — align Supabase cp_* schema with edge-side ControlPlaneStore
-- ============================================================================
-- Date:    2026-05-19
-- Target:  Supabase project (postgres, public schema)
--
-- Purpose: Make the cloud cp_* tables column-compatible with the SQLite
--          version in backend/app/services/control_plane_store.py so the
--          new ControlPlaneStoreCloud can run the existing SQL with just
--          the SQLite-flavour adapter (no per-method rewrites).
--
-- Idempotent. Safe to re-run.
--
-- Notes:
--   - We add ONLY the columns the SQLite store relies on but Postgres
--     was missing. We do NOT drop the cp_edge_licenses table (which
--     exists in Postgres but not in SQLite) because something may
--     already depend on it; the new store just doesn't write to it.
--   - The `id` columns on Postgres-only tables (cp_users, cp_license_modules,
--     etc.) are kept — they're bigserials and harmless. The SQLite store
--     uses INSERT ... ON CONFLICT(...) targeting natural keys, which
--     still works whether or not an `id` autoincrement column exists.
-- ============================================================================

BEGIN;

-- 1. cp_edge_activation_codes: add activation_code, edge_id, license_id
ALTER TABLE public.cp_edge_activation_codes
  ADD COLUMN IF NOT EXISTS activation_code text,
  ADD COLUMN IF NOT EXISTS edge_id text,
  ADD COLUMN IF NOT EXISTS license_id text;

-- Optional FKs — added soft so they don't break inserts when the
-- referenced row hasn't been written yet (the SQLite path doesn't enforce
-- them either).

-- 2. cp_users: add customer_id (Lite users scoped per customer) and
--    must_change_password (admin-issued temp password flag).
ALTER TABLE public.cp_users
  ADD COLUMN IF NOT EXISTS customer_id text,
  ADD COLUMN IF NOT EXISTS must_change_password integer NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS ix_cp_users_tenant_customer
  ON public.cp_users(tenant_id, customer_id);

-- 3. cp_customers: ancestor_tenant_id already added by an earlier
--    migration. The SQLite store doesn't reference it, so nothing more
--    to do here.

-- 4. cp_tenants: SQLite uses TEXT created_utc / updated_utc; Postgres
--    uses timestamptz. The SQLite store passes ISO-8601 strings, which
--    Postgres accepts via implicit cast, so no schema change needed.

COMMIT;

-- Sanity:
--   SELECT column_name FROM information_schema.columns
--    WHERE table_schema='public' AND table_name='cp_edge_activation_codes'
--    ORDER BY ordinal_position;
--   -- should include activation_code, edge_id, license_id
