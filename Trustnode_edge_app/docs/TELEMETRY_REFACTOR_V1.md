# Telemetry Refactor v1 (IT/OT Safe, Provenance-First)

## Updated Architecture Summary
- **OT Edge collector** reads PLC tags on configured interval and performs **durable local commit first**.
- Local commit writes to:
  - `telemetry_samples_raw` (immutable truth)
  - `sync_outbox_v1` (authoritative cloud-sync queue)
  - `latest_machine_state` (live snapshot)
  - `ingest_audit_log_local` (append-only local audit)
- Edge performs **outbound-only HTTPS** to VPS ingest API: `POST /api/v1/ingest/batch`.
- VPS ingest validates scope + schema + payload hash and writes to cloud DB tables (`telemetry_samples_raw`, `latest_machine_state`, `ingest_audit_log`).
- Dashboards should consume `latest_machine_state` for live mode and `telemetry_samples_raw` for historical mode.

## Revised Folder Structure
- `backend/app/models_telemetry_v1.py`
- `backend/app/auth_device.py`
- `backend/app/services/telemetry_service.py` (edge durable write + outbox + retries)
- `backend/app/services/ingest_store.py` (VPS-side ingest persistence)
- `backend/app/routers/telemetry_v1.py` (`/api/v1/*` endpoints)
- `backend/sql/migrations/2026-04-11_telemetry_v1_core.sql`
- `backend/tests/test_telemetry_v1.py`

## SQL Migrations
- Added core migration file:
  - `backend/sql/migrations/2026-04-11_telemetry_v1_core.sql`
- Tables covered:
  - `telemetry_samples_raw`
  - `latest_machine_state`
  - `ingest_audit_log`
  - `gateway_registry`
  - `gateway_credentials_metadata`
  - `collection_config_versions`
  - `tenant_users`
  - `security_audit_log`
- Includes baseline RLS policies for tenant-scoped read access on user-facing tables.

## Auth Flow
- **Human auth** remains existing `/api/auth/*` token flow (separate from device ingest path).
- **Device auth** uses dedicated device tokens (`typ=device`) signed with `TRUSTNODE_DEVICE_AUTH_SECRET`.
- New admin endpoint to mint scoped device token:
  - `POST /api/v1/devices/token` (requires admin user token)
- Ingest endpoint enforces token tenant/gateway scope match.

## Ingestion Flow
- `POST /api/v1/ingest/batch`
  - Accepts gzip (`Content-Encoding: gzip`) JSON.
  - Strict model validation.
  - Recomputes payload hash (`payload_hash_sha256`) and rejects tampered records.
  - Writes `received_at_vps_ts_utc` / `ingested_at_cloud_ts_utc` on server.
  - Idempotent by `edge_record_id`.
  - `latest_machine_state` only advances on newer `(sample_ts_utc, edge_monotonic_seq)`.
  - Returns `acknowledged_ids`, `duplicate_ids`, `rejected`.

## Outbox Sync Flow
- Edge writes every committed cycle into `sync_outbox_v1`.
- Background sync thread:
  - FIFO by `(sample_ts_utc, edge_monotonic_seq)`
  - batches and gzip upload
  - exponential backoff + jitter on failure
  - marks rows `acked` only on explicit ack IDs
  - partial reject handling by row status (`acked`, `rejected`, `retry`)

## Live-Update Flow
- Edge live snapshot source: `latest_machine_state` (local)
- Cloud live snapshot source: `latest_machine_state` (cloud)
- Historical queries use immutable `telemetry_samples_raw`.

## Audit Log Design
- Append-only audit rows with:
  - `ts_utc`, `actor_type`, `actor_id`, `tenant_id`, `action`, `outcome`, `correlation_id`, `details_json`
- Events currently emitted in new path:
  - local collection commit success/failure
  - batch send failure
  - batch acknowledge/partial
  - device auth failures and scope mismatches
  - token issuance

## Health-State Model
- Existing `api/health` unchanged.
- Added:
  - `GET /api/v1/healthz`
  - `GET /api/v1/readyz`
  - `GET /api/v1/edge/diagnostics` (outbox depth + oldest unsynced sample)

## Tests Added
- `backend/tests/test_telemetry_v1.py`
  - payload hash determinism
  - duplicate resend idempotency
  - latest state monotonic update behavior
  - device token scope roundtrip

## Assumptions
- Existing legacy app-store and PLC writer paths are still present for compatibility.
- New `v1` path is introduced in parallel to reduce rollout risk.
- Supabase/Postgres RLS production policies should be applied on target cloud DB.

## Remaining Risks
- Legacy and v1 paths currently coexist; until full cutover, operators can still rely on older endpoints.
- MFA enforcement for admin users requires integration with your chosen IdP/Auth provider; DB flag exists but runtime enforcement is not fully integrated yet.
- Device credential rotation workflow metadata table exists; operational rotation APIs are still basic and should be expanded.
- Metrics are currently persisted minimally in local DB; full Prometheus/OTel export not yet added in this patch.
