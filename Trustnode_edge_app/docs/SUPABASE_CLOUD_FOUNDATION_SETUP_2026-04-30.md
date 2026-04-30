# Supabase Cloud Foundation Setup (2026-04-30)

This prepares Trustnode cloud DB for:
- Multi-customer control-plane data
- Tenant-safe RLS baseline
- License/module catalog primitives
- URL domain mapping primitives

## Applied Migration Order

1. `Trustnode_edge_app/backend/sql/migrations/2026-04-11_telemetry_v1_core.sql`
2. `Trustnode_edge_app/backend/sql/migrations/2026-04-22_control_plane_core.sql`
3. `Trustnode_edge_app/backend/sql/migrations/2026-04-30_supabase_control_plane_hardening.sql`

## Prerequisites

- `psql` available in PATH
- Supabase PostgreSQL connection URL (service/admin DB URL)

## One-command Apply

```powershell
powershell -ExecutionPolicy Bypass -File .\Trustnode_edge_app\scripts\apply-supabase-cloud-foundation.ps1 `
  -DatabaseUrl "postgresql://postgres:<PASSWORD>@<HOST>:5432/postgres?sslmode=require"
```

## Verify Core Objects

```sql
-- Core tables:
select to_regclass('public.cp_tenants');
select to_regclass('public.cp_customers');
select to_regclass('public.cp_edges');
select to_regclass('public.cp_licenses');
select to_regclass('public.cp_module_catalog');
select to_regclass('public.cp_customer_domains');
select to_regclass('public.cp_edge_licenses');

-- Telemetry tables:
select to_regclass('public.telemetry_samples_raw');
select to_regclass('public.latest_machine_state');
select to_regclass('public.ingest_audit_log');
```

## Verify RLS + Policies

```sql
select schemaname, tablename, rowsecurity
from pg_tables
where schemaname='public'
  and tablename in (
    'cp_tenants','cp_customers','cp_edges','cp_licenses','cp_license_modules',
    'cp_users','cp_user_tenant_memberships','cp_edge_activation_codes',
    'cp_password_reset_events','cp_security_audit_log',
    'cp_customer_domains','cp_module_catalog','cp_edge_licenses',
    'telemetry_samples_raw','latest_machine_state','ingest_audit_log'
  )
order by tablename;

select schemaname, tablename, policyname, permissive, cmd
from pg_policies
where schemaname='public'
  and tablename like 'cp_%'
order by tablename, policyname;
```

## Notes

- Browser roles (`anon`, `authenticated`) are revoked from direct control-plane table access.
- Control-plane writes should happen through backend API/service role.
- Existing app compatibility is preserved (tables remain in `public`).
