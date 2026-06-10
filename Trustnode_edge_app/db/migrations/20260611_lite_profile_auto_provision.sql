-- ============================================================================
-- TrustNode Lite — auto-provision lite_profiles from auth.users
-- ============================================================================
-- Date:    2026-06-11
-- Target:  Supabase project
-- Purpose: Eliminate "user can log in but sees nothing" silent failures.
--
-- Before this trigger, lite_profiles was only populated by a fire-and-forget
-- background thread in lite_user_mirror.py. If that thread failed (Supabase
-- pooler busy, network blip, edge restart mid-write), the user got a
-- Supabase Auth account but no lite_profiles row. RLS then returned NULL
-- for lite_current_tenant() and every Lite read was denied — the operator
-- saw a blank dashboard and "STALE" on the header.
--
-- This trigger ensures that EVERY auth.users row produces a lite_profiles
-- row, derived from the JWT metadata the edge writes on user creation:
--   raw_user_meta_data->>'username'
--   raw_user_meta_data->>'tenant_id'
--   raw_user_meta_data->>'role'
-- ============================================================================

CREATE OR REPLACE FUNCTION public.lite_handle_new_auth_user()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_catalog
AS $$
DECLARE
  v_tenant text;
  v_username text;
  v_role text;
BEGIN
  v_tenant := COALESCE(NEW.raw_user_meta_data->>'tenant_id', 'default');
  v_username := NEW.raw_user_meta_data->>'username';
  v_role := COALESCE(NEW.raw_user_meta_data->>'role', 'viewer');

  -- Idempotent upsert: keeps existing tenant/role assignment when the
  -- trigger fires on metadata updates (Supabase Auth fires UPDATE on
  -- email confirmation flows, password resets, etc).
  INSERT INTO public.lite_profiles (
    user_id, tenant_id, username, email, role, created_utc, updated_utc
  ) VALUES (
    NEW.id, v_tenant, v_username, NEW.email, v_role, now(), now()
  )
  ON CONFLICT (user_id) DO UPDATE SET
    tenant_id   = EXCLUDED.tenant_id,
    username    = COALESCE(EXCLUDED.username, public.lite_profiles.username),
    email       = COALESCE(EXCLUDED.email, public.lite_profiles.email),
    role        = EXCLUDED.role,
    updated_utc = now();
  RETURN NEW;
EXCEPTION WHEN OTHERS THEN
  -- Never fail the auth.users write because of a profile-mirror hiccup.
  RAISE WARNING 'lite_handle_new_auth_user: % (user=%, tenant=%)', SQLERRM, NEW.id, v_tenant;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS lite_profiles_from_auth_users_insert ON auth.users;
CREATE TRIGGER lite_profiles_from_auth_users_insert
AFTER INSERT ON auth.users
FOR EACH ROW
EXECUTE FUNCTION public.lite_handle_new_auth_user();

DROP TRIGGER IF EXISTS lite_profiles_from_auth_users_update ON auth.users;
CREATE TRIGGER lite_profiles_from_auth_users_update
AFTER UPDATE OF raw_user_meta_data, email ON auth.users
FOR EACH ROW
EXECUTE FUNCTION public.lite_handle_new_auth_user();

COMMENT ON FUNCTION public.lite_handle_new_auth_user() IS
  'Mirrors auth.users JWT metadata into public.lite_profiles so RLS lite_current_tenant() always resolves. Replaces the fire-and-forget lite_user_mirror upsert as the source of truth.';

-- One-time backfill: every existing auth.users without a lite_profiles row
-- gets one derived from raw_user_meta_data. Idempotent.
INSERT INTO public.lite_profiles (
  user_id, tenant_id, username, email, role, created_utc, updated_utc
)
SELECT
  u.id,
  COALESCE(u.raw_user_meta_data->>'tenant_id', 'default'),
  u.raw_user_meta_data->>'username',
  u.email,
  COALESCE(u.raw_user_meta_data->>'role', 'viewer'),
  now(),
  now()
FROM auth.users u
LEFT JOIN public.lite_profiles p ON p.user_id = u.id
WHERE p.user_id IS NULL
ON CONFLICT (user_id) DO NOTHING;
