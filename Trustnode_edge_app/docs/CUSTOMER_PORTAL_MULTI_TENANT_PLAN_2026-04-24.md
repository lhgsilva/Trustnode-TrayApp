# TrustNode Multi-Customer Portal Plan (Admin + Customer Subdomains)
Date: 2026-04-24  
Scope: `trustnode.lsapps.app` + 3 customer subdomains + edge linking + Supabase segregation.

## 1) Current State Evaluation
- Good foundation already exists:
  - Control-plane tables: tenants, customers, edges, licenses, users, activation codes, security audit log.
  - Tenant-aware auth tokens already include `tenant_id`.
  - Host-based tenant resolution already exists.
  - Edge telemetry path already supports tenant-aware ingest and outbox.
- Main gaps for production rollout:
  - No single atomic provisioning endpoint for new customer tenant bundles.
  - Edge installer/portable onboarding path via activation code was not fully installer-friendly.
  - No explicit portal-context endpoint for subdomain diagnostics/health checks.
  - Operational runbook for creating customer-a/b/c tenants was manual.

## 2) Recommended Segregation Strategy (Supabase)
- Recommended baseline (lowest change, scalable now): **shared tables + strict RLS by `tenant_id`**.
- Keep one Supabase project for now, with:
  - `tenant_id` mandatory on all user-facing rows.
  - RLS policies enforced on telemetry/history/latest/reporting/control-plane reads.
  - Service-role credentials only on backend/VPS, never in edge/browser.
- Upgrade path:
  - Premium customers can move to dedicated schema or dedicated project later.
  - Maintain same API contract; route by tenant metadata.

Why this now:
- Minimal migration cost.
- Fast onboarding for first customers.
- Strong isolation if RLS is strict and tested.

## 3) Subdomain and Portal Model
- Admin control plane (global admin): `https://trustnode.lsapps.app`
  - Create/update tenants/customers/licenses/users/modules.
  - Issue activation codes and password reset tokens.
  - Cross-tenant visibility only for global admin.
- Customer portals:
  - `https://customer-a-trustnode.lsapps.app`
  - `https://customer-b-trustnode.lsapps.app`
  - `https://customer-c-trustnode.lsapps.app`
- Tenant resolution:
  - Subdomain host -> `cp_tenants.primary_domain` -> `tenant_id`.
  - Token tenant must match resolved tenant (except global admin on admin host).

## 4) Edge Linking / Installer Flow (Target)
1. Customer receives activation code (short TTL) from admin portal.
2. Edge installer/portable asks for:
   - `edge_id`, `edge_name`, site/area/equipment, activation code.
3. Edge calls VPS endpoint to validate/apply code and get bootstrap payload.
4. VPS returns:
   - `tenant_id`, `customer_id`, license/modules, cloud API URL, app settings patch.
5. Edge writes local app config and starts local collection + cloud mirror push.

## 5) Phase Plan

### Phase 1 (Completed in this pass)
- Add one-call customer provisioning API.
- Add portal-context endpoint for host->tenant diagnostics.
- Add edge-link bootstrap API for installer/portable usage.
- Add provisioning script for customer-a/b/c.

### Phase 2
- Add admin UI workflows for:
  - “Create Customer Bundle” wizard.
  - “Issue Activation Code” per customer/edge.
  - “Reset Customer Password” with audited workflow.

### Phase 3
- Add installer UX:
  - Activation-code bootstrap form.
  - Connectivity checks + tenant/module preview before apply.

### Phase 4
- Enforce module-based UI gating in cloud client pages by tenant license.

### Phase 5
- Add tenant RLS verification tests and cross-tenant negative tests.

### Phase 6
- Add billing/license enforcement hooks (edge count, user count, module entitlements).

### Phase 7
- Add tenant-specific branding/domain templates + optional dedicated DB/schema routing.

## 6) What Was Implemented Now
- New backend endpoints:
  - `POST /api/control-plane/provision/customer-bundle`
  - `GET /api/control-plane/portal-context`
  - `POST /api/control-plane/edge-link/bootstrap`
- Improved activation endpoint behavior:
  - `POST /api/control-plane/activation-code/apply` now supports device/installer use without requiring pre-login token.
- New store capabilities:
  - Tenant lookup by domain.
  - Customer license lookup.
  - Atomic customer bundle provisioning.
  - Edge bootstrap payload builder from activation code.
- New automation script:
  - `Trustnode_edge_app/scripts/provision-three-customers.ps1`
  - Seeds `customer_a`, `customer_b`, `customer_c` with matching subdomains and default licenses/users.

## 7) Operational Next Steps
1. Run provisioning script against VPS.
2. Configure Nginx vhosts/wildcard certs to route all customer subdomains to same frontend/backend stack.
3. Run RLS policy verification for telemetry + reporting endpoints.
4. Update edge installer/portable flow to call `edge-link/bootstrap`.
5. Run end-to-end test:
   - local edge -> cloud ingest -> customer subdomain live dashboard.

## 8) Risks and Controls
- Risk: misconfigured domain->tenant mapping.
  - Control: `portal-context` endpoint + startup checks.
- Risk: cross-tenant data leak.
  - Control: token-tenant enforcement + RLS tests + audit logging.
- Risk: leaked service credentials.
  - Control: keep service creds only server-side env.
- Risk: activation code abuse.
  - Control: short TTL + one-time use + audit trail.
