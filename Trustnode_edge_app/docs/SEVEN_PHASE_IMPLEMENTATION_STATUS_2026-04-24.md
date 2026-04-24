# TrustNode 7-Phase Implementation Status

Date: 2026-04-24  
Scope: Complete the multi-customer control-plane rollout with minimal disruption to the working edge data path.

## Phase 1 - Edge Pipeline Stabilization
Status: Completed
- Edge local-first durable write path remains in place (`telemetry_samples_raw` + `sync_outbox_v1` + `latest_machine_state`).
- Added cloud ingest bootstrap visibility endpoint:
  - `GET /api/control-plane/edge-bootstrap-status`
  - returns ingest URL readiness, outbox depth, oldest unsynced, last error, by-gateway backlog.

## Phase 2 - Control Plane Core
Status: Completed
- Control plane entities and lifecycle APIs are implemented:
  - tenants, customers, edges, licenses, users, activation codes, password reset.
- Runtime context endpoint active:
  - `GET /api/control-plane/runtime-context`
- Module catalog and license module mapping active.

## Phase 3 - Tenant URL and Isolation
Status: Completed (safe-default mode)
- Host-based tenant mapping from configured tenant domains (`cp_tenants.primary_domain`).
- Dynamic subdomain auto-mapping moved behind explicit opt-in env:
  - `TRUSTNODE_ALLOW_DYNAMIC_TENANT_SUBDOMAIN=1`
- Default behavior is safer (configured mapping first).

## Phase 4 - Security and Access Hardening
Status: Completed
- Control-plane endpoints now enforce authenticated access.
- Write operations require admin role.
- Cross-tenant access denied unless global admin (`role=admin` + `tenant_id=default`).
- Security audit logging added for control-plane write actions:
  - tenant/user/license/customer/edge updates
  - activation/password-reset issue/apply
  - edge heartbeat success/failure

## Phase 5 - Customer Login and Tenant Routing Reliability
Status: Completed
- Added control-plane cross-tenant login fallback for shared-host login pages:
  - `authenticate_user_any_tenant(username,password)` is used only when username maps to exactly one active tenant.
- Prevents false 401 when user exists in control-plane but host resolves to default tenant.

## Phase 6 - Edge-to-Cloud Operational Diagnostics
Status: Completed
- New endpoint provides operational health for rollout checks:
  - `GET /api/control-plane/edge-bootstrap-status`
- Existing telemetry diagnostics path remains available:
  - `GET /api/v1/edge/diagnostics`

## Phase 7 - Rollout Verification Automation
Status: Completed
- Added smoke script:
  - `Trustnode_edge_app/scripts/smoke-seven-phases.ps1`
- Verifies:
  - health
  - auth
  - control-plane runtime context
  - edge bootstrap status
  - control-plane summary

---

## Key Files Updated in This Pass
- `backend/app/routers/control_plane.py`
- `backend/app/routers/auth.py`
- `backend/app/services/control_plane_store.py`
- `backend/app/tenant.py`
- `frontend/src/api.js`
- `scripts/smoke-seven-phases.ps1`

## Notes
- This rollout keeps the existing edge collector and cloud sync architecture intact to avoid regressions.
- Changes are additive/hardening-oriented around tenant isolation, auth, and operations.
- Next production step: run smoke script on VPS after deploy and validate one customer subdomain host mapping path.
