-- TrustNode Control Plane Core (tenants/customers/edges/licenses/users)
-- Date: 2026-04-22

BEGIN;

CREATE TABLE IF NOT EXISTS cp_tenants (
  tenant_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  primary_domain TEXT,
  timezone TEXT NOT NULL DEFAULT 'UTC',
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_utc TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cp_customers (
  customer_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES cp_tenants(tenant_id),
  company_name TEXT NOT NULL,
  contact_email TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_utc TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cp_edges (
  edge_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES cp_tenants(tenant_id),
  customer_id TEXT REFERENCES cp_customers(customer_id),
  edge_name TEXT NOT NULL,
  site TEXT,
  area TEXT,
  equipment TEXT,
  status TEXT NOT NULL DEFAULT 'inactive',
  activation_code_hash TEXT,
  activated_utc TIMESTAMPTZ,
  last_heartbeat_utc TIMESTAMPTZ,
  heartbeat_payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_utc TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cp_licenses (
  license_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES cp_tenants(tenant_id),
  customer_id TEXT REFERENCES cp_customers(customer_id),
  plan_code TEXT NOT NULL DEFAULT 'standard',
  status TEXT NOT NULL DEFAULT 'active',
  start_utc TIMESTAMPTZ,
  end_utc TIMESTAMPTZ,
  max_edges INTEGER NOT NULL DEFAULT 3,
  max_users INTEGER NOT NULL DEFAULT 10,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_utc TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cp_license_modules (
  id BIGSERIAL PRIMARY KEY,
  license_id TEXT NOT NULL REFERENCES cp_licenses(license_id),
  module_key TEXT NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  updated_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(license_id, module_key)
);

CREATE TABLE IF NOT EXISTS cp_users (
  id BIGSERIAL PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES cp_tenants(tenant_id),
  username TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'viewer',
  status TEXT NOT NULL DEFAULT 'active',
  email TEXT,
  mfa_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  modules_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  permissions_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_login_utc TIMESTAMPTZ,
  UNIQUE(tenant_id, username)
);

CREATE TABLE IF NOT EXISTS cp_user_tenant_memberships (
  id BIGSERIAL PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  username TEXT NOT NULL,
  edge_id TEXT,
  module_key TEXT,
  granted BOOLEAN NOT NULL DEFAULT TRUE,
  created_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(tenant_id, username, edge_id, module_key)
);

CREATE TABLE IF NOT EXISTS cp_edge_activation_codes (
  code_hash TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES cp_tenants(tenant_id),
  customer_id TEXT REFERENCES cp_customers(customer_id),
  edge_name TEXT,
  expires_utc TIMESTAMPTZ NOT NULL,
  used_utc TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'issued',
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_utc TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cp_password_reset_events (
  id BIGSERIAL PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  username TEXT NOT NULL,
  token_hash TEXT NOT NULL,
  expires_utc TIMESTAMPTZ NOT NULL,
  used_utc TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'issued',
  created_utc TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cp_security_audit_log (
  id BIGSERIAL PRIMARY KEY,
  ts_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor_type TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  action TEXT NOT NULL,
  outcome TEXT NOT NULL,
  correlation_id TEXT NOT NULL,
  details_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Infrastructure endpoints (developer-admin managed, 2026-07-15). Single source
-- of truth for where the deployment's services live (control-plane API, Supabase,
-- AI, ...). tenant_id '__global__' is the deployment-wide default; a per-tenant
-- row overrides it. endpoints_json is kept as TEXT (a JSON string) to match the
-- ControlPlaneStore accessor which json.loads/dumps it — do NOT change to JSONB
-- without updating the store. Idempotent so re-running the migration is safe and
-- a fresh Supabase gets it automatically (no hand-created tables ever).
CREATE TABLE IF NOT EXISTS cp_infrastructure_config (
  tenant_id TEXT PRIMARY KEY,
  endpoints_json TEXT NOT NULL DEFAULT '{}',
  updated_by TEXT,
  updated_utc TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS ix_cp_customers_tenant ON cp_customers(tenant_id);
CREATE INDEX IF NOT EXISTS ix_cp_edges_tenant ON cp_edges(tenant_id);
CREATE INDEX IF NOT EXISTS ix_cp_users_tenant ON cp_users(tenant_id);
CREATE INDEX IF NOT EXISTS ix_cp_licenses_tenant ON cp_licenses(tenant_id);
CREATE INDEX IF NOT EXISTS ix_cp_audit_tenant_ts ON cp_security_audit_log(tenant_id, ts_utc DESC);

COMMIT;
