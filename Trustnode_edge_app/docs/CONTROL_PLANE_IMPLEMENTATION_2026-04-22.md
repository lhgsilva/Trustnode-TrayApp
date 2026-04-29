# Control Plane Implementation - 2026-04-22

## Implemented
- Backend control-plane service and API routes for tenant/customer/edge/license/user lifecycle.
- Tenant-aware login fallback to control-plane users.
- JWT includes `modules` claim for client module gating.
- Dynamic host->tenant mapping using `cp_tenants.primary_domain`.
- Frontend user management now writes users to control-plane APIs and mirrors legacy `users_access`.
- Frontend login/session restore now consumes `modules` and refreshes control-plane runtime context.
- CI backend syntax check now includes control-plane files.

## Files Added
- `backend/app/services/control_plane_store.py`
- `backend/app/routers/control_plane.py`
- `backend/sql/migrations/2026-04-22_control_plane_core.sql`
- `scripts/apply-control-plane-migration.ps1`

## Deploy Checklist
1. Push branch to `main` to trigger `trustnode-edge-cicd.yml`.
2. On VPS, verify backend healthy:
   - `curl -fsS http://127.0.0.1:8000/api/health`
3. Optional: apply Postgres/Supabase migration:
   - `pwsh -File Trustnode_edge_app/scripts/apply-control-plane-migration.ps1 -DatabaseUrl "$env:SUPABASE_DB_URL"`
4. Smoke test API with admin token:
   - `GET /api/control-plane/modules`
   - `GET /api/control-plane/runtime-context`
   - `GET /api/control-plane/users`

## New API Endpoints
- `GET /api/control-plane/modules`
- `GET/POST /api/control-plane/tenants`
- `GET/POST /api/control-plane/customers`
- `GET/POST /api/control-plane/edges`
- `POST /api/control-plane/edges/heartbeat`
- `GET/POST /api/control-plane/licenses`
- `GET/PUT /api/control-plane/licenses/{license_id}/modules`
- `GET/POST /api/control-plane/users`
- `DELETE /api/control-plane/users/{username}`
- `POST /api/control-plane/activation-code/issue`
- `POST /api/control-plane/activation-code/apply`
- `POST /api/control-plane/password-reset/issue`
- `POST /api/control-plane/password-reset/apply`
- `GET /api/control-plane/summary`
- `GET /api/control-plane/runtime-context`
