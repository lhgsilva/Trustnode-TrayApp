-- 2026-04-11_telemetry_v1_core.sql
-- Immutable raw telemetry + latest snapshot + ingest/security audit + gateway registry

CREATE TABLE IF NOT EXISTS telemetry_samples_raw (
  edge_record_id UUID PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  customer_id TEXT NOT NULL,
  plant_id TEXT NOT NULL,
  machine_id TEXT NOT NULL,
  gateway_id TEXT NOT NULL,
  collector_instance_id TEXT NOT NULL,
  gateway_config_version TEXT NOT NULL,
  plc_driver_type TEXT NOT NULL,
  plc_endpoint_id TEXT NOT NULL,
  sample_ts_utc TIMESTAMPTZ NOT NULL,
  edge_monotonic_seq BIGINT NOT NULL,
  interval_ms INTEGER NOT NULL,
  tags_json JSONB NOT NULL,
  quality_code SMALLINT,
  collection_status TEXT NOT NULL,
  collected_at_edge_ts_utc TIMESTAMPTZ NOT NULL,
  received_at_vps_ts_utc TIMESTAMPTZ NOT NULL,
  ingested_at_cloud_ts_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
  payload_hash_sha256 TEXT NOT NULL,
  time_status TEXT NOT NULL DEFAULT 'ok',
  CONSTRAINT uq_telemetry_gateway_seq UNIQUE (gateway_id, edge_monotonic_seq),
  CONSTRAINT ck_telemetry_interval_positive CHECK (interval_ms > 0)
);

CREATE INDEX IF NOT EXISTS ix_telemetry_tenant_plant_machine_ts
  ON telemetry_samples_raw(tenant_id, plant_id, machine_id, sample_ts_utc DESC);
CREATE INDEX IF NOT EXISTS ix_telemetry_gateway_ts
  ON telemetry_samples_raw(gateway_id, sample_ts_utc DESC);
CREATE INDEX IF NOT EXISTS ix_telemetry_gateway_seq
  ON telemetry_samples_raw(gateway_id, edge_monotonic_seq DESC);

CREATE TABLE IF NOT EXISTS latest_machine_state (
  machine_key TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  customer_id TEXT NOT NULL,
  plant_id TEXT NOT NULL,
  machine_id TEXT NOT NULL,
  gateway_id TEXT NOT NULL,
  sample_ts_utc TIMESTAMPTZ NOT NULL,
  edge_monotonic_seq BIGINT NOT NULL,
  tags_json JSONB NOT NULL,
  quality_code SMALLINT,
  gateway_config_version TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ingest_audit_log (
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

CREATE TABLE IF NOT EXISTS gateway_registry (
  gateway_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  customer_id TEXT,
  plant_id TEXT,
  machine_id TEXT,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  updated_utc TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS gateway_credentials_metadata (
  id BIGSERIAL PRIMARY KEY,
  gateway_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  credential_fingerprint TEXT NOT NULL,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  rotated_at_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_gateway_cred UNIQUE(gateway_id, credential_fingerprint)
);

CREATE TABLE IF NOT EXISTS collection_config_versions (
  id BIGSERIAL PRIMARY KEY,
  gateway_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  gateway_config_version TEXT NOT NULL,
  created_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_gateway_cfg UNIQUE(gateway_id, tenant_id, gateway_config_version)
);

CREATE TABLE IF NOT EXISTS tenant_users (
  id BIGSERIAL PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  username TEXT NOT NULL,
  role TEXT NOT NULL,
  mfa_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  created_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_tenant_user UNIQUE(tenant_id, username)
);

CREATE TABLE IF NOT EXISTS security_audit_log (
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

-- RLS baseline (tenant isolation for user-facing access).
ALTER TABLE telemetry_samples_raw ENABLE ROW LEVEL SECURITY;
ALTER TABLE latest_machine_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingest_audit_log ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = 'telemetry_samples_raw' AND policyname = 'tenant_read_telemetry'
  ) THEN
    CREATE POLICY tenant_read_telemetry ON telemetry_samples_raw
      FOR SELECT USING (tenant_id = current_setting('request.jwt.claim.tenant_id', true));
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = 'latest_machine_state' AND policyname = 'tenant_read_latest'
  ) THEN
    CREATE POLICY tenant_read_latest ON latest_machine_state
      FOR SELECT USING (tenant_id = current_setting('request.jwt.claim.tenant_id', true));
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = 'ingest_audit_log' AND policyname = 'tenant_read_ingest_audit'
  ) THEN
    CREATE POLICY tenant_read_ingest_audit ON ingest_audit_log
      FOR SELECT USING (tenant_id = current_setting('request.jwt.claim.tenant_id', true));
  END IF;
END $$;
