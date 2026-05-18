# Control-Plane → Supabase-Canonical Redesign

**Started:** 2026-05-19
**Status:** Design phase — no code changes yet.

## Why this exists

Today the control-plane data (`cp_*` tables) lives in **two places**:

1. **VPS local SQLite** at `/opt/trustnode-edge/data/trustnode_app_store.db`
   on `87.106.7.67`. Written by the edge backend (FastAPI) via
   `control_plane_store` whenever the portal calls a `/api/control-plane/*`
   endpoint. Has 86 customers (mostly smoke tests + 4 real customers).
2. **Cloud Supabase** `postgres.tsfreqjcrgbxdwvmxeuk` `public.cp_*`
   tables. Has 3 customers, written somewhere we never identified
   (probably the portal frontend writing directly with the service-role
   key, bypassing the FastAPI backend).

The split caused every multi-tenant issue we hit in the 2026-05-18 session:

- Portal showed 0 customers because GET `/customers?tenant_id=default`
  returned VPS-local rows tagged 'default' but not the per-customer-tenant
  rows the new code created.
- New customer creation worked server-side but the customer list view
  filtered them out by tenant.
- Edge activation tied the edge to whichever store was being read at
  the moment of activation, so an edge could be "linked" in one store
  and "missing" in the other.
- Lite views (which read directly from Supabase) couldn't see customers
  that lived only in VPS local SQLite.

The decision (2026-05-19): **Supabase becomes the single source of truth
for every `cp_*` row.** VPS local SQLite for control-plane data goes
away entirely. Edges keep their own local SQLite for their own
historian/buffer/cache — that's a separate concern.

## What stays vs what moves

### Moves to Supabase (canonical)

All 11 `cp_*` tables — every row that today lives in
`/opt/trustnode-edge/data/trustnode_app_store.db`:

- `cp_tenants` — tenant directory
- `cp_customers` — customers per tenant
- `cp_edges` — edge devices per customer
- `cp_licenses` — license records per customer
- `cp_license_modules` — module entitlements per license
- `cp_users` — portal / Lite user accounts
- `cp_user_tenant_memberships` — user → tenant grants
- `cp_edge_activation_codes` — issued activation codes
- `cp_password_reset_events` — password reset tokens
- `cp_security_audit_log` — audit trail

### Stays on the edge SQLite

- `historian_readings`, `plc_readings`, `live_latest`, `app_logs` —
  edge-local time-series. Sync to Supabase happens via the existing
  outbox flow. Not changing.
- `config_documents`, `config_documents_scoped` — edge configuration.
  Per-edge, mirrored selectively to Supabase via `_mirror_config_doc_to_cloud`.
- `sync_outbox`, `data_sync_state`, `sync_targets` — store-and-forward
  bookkeeping. Lives only on the edge that owns the data.
- `outbox_readings` — per-gateway store-and-forward queue. Lives only
  on the edge.

### Stays on the VPS

- A short-lived **read-through cache** for control-plane reads, so
  hot lookups like `get_customer_tenant_id(cid)` don't go over the
  wire on every request. 60 second TTL. Eviction on local writes.
  Implementation: an in-memory dict, no disk file.

That's it. The VPS no longer needs the local `trustnode_app_store.db`
file at all for control-plane purposes. It still uses
`config_documents` etc. for its own edge-side config (it IS an edge
when you point at it via the desktop EXE in cloud mode), but the
`cp_*` tables drop out.

## Architecture: ControlPlaneStoreCloud

`backend/app/services/control_plane_store_cloud.py` — new module.

Same 55-method public interface as the existing SQLite class, so the
routers don't change. Each method:

```python
def upsert_customer(self, *, tenant_id, customer_id, company_name, ...):
    with self._engine.begin() as conn:
        # Single SQL UPSERT into public.cp_customers via SQLAlchemy text().
        # Returns the row that was inserted/updated.
```

Uses the same `_get_cloud_database_target()` / `_get_or_create_cloud_engine()`
helpers `app_store` already has, so it benefits from the existing
Pooler connection + engine cache.

### Feature flag

Environment variable: `TRUSTNODE_CONTROL_PLANE_BACKEND`
- `local` (default) — existing SQLite class. Today's behaviour.
- `cloud` — new Supabase-backed class.
- `dual_write` — writes to both, reads from cloud. For migration window.

`app/state.py` picks the backend at startup based on the env var,
exposes the chosen instance as `control_plane_store`. Routers never
know which one they're using.

### Auth and authorization

No change. JWT-based auth, master = `tenant_id=default + role=admin`,
RLS bypass policies on Supabase already handle the master view. The
ControlPlaneStoreCloud uses the **service-role key** (via the cloud
engine), which bypasses RLS — same as the SQLite path does (no RLS
on SQLite).

### Performance

Every write is now a Pooler round-trip (~10-50ms). Today's hot paths:

| Endpoint | Calls today | After redesign |
| -------- | ----------- | -------------- |
| `GET /customers` (portal page load) | 1× local SQL | 1× Pooler |
| `POST /customers` | 2-3× local SQL | 2-3× Pooler |
| `POST /activation-code/issue` | 4-5× local SQL | 4-5× Pooler |
| `POST /edge-link/local-finalize` (the slow one) | 5-7× local + 2-3× cloud | 5-7× Pooler |

Acceptable. The local-finalize endpoint is run once per edge activation,
not per request. The portal page loads stay <300ms with the Pooler.

The 60-second read cache covers the very-hot `get_customer_tenant_id`
path (called from every `_customer_tenant_id` resolution) and
`list_customers` (refreshed on every portal nav).

## Data migration

One-time script: `scripts/migrate_cp_to_supabase.py`.

For each `cp_*` table:
1. SELECT every row from VPS local SQLite.
2. INSERT ... ON CONFLICT DO UPDATE into Supabase.
3. Conflict resolution: cloud row wins if `updated_utc` is newer; else
   local row wins. (Both sides have `updated_utc`.)
4. Verify post-migration: cloud row count >= local row count for every
   table.

Smoke-test customers (`smoke-*`) excluded by default so we don't
clutter the canonical Supabase with old test rows. Override with
`--include-smoke`.

Idempotent. Dry-run first, then `--commit`.

## Cutover plan

1. **Build the cloud store class.** Unit-test each method against the
   live Supabase using a `tenant-test-cutover` tenant that we'll drop
   afterwards.
2. **Add the feature flag.** Deploy with `local` mode — no change in
   behaviour. Confirm portal still works.
3. **Switch to `dual_write` mode.** Every write goes to both stores;
   reads still come from local SQLite. Run for a few minutes to fill
   cloud with anything portal touches.
4. **Run the migration script.** Pushes the historical 86 customers
   into Supabase under their proper per-customer tenants (using the
   tenants we created in the 2026-05-18 backfill).
5. **Switch to `cloud` mode.** Routers now read from Supabase.
6. **Watch logs for an hour.** Any 5xx or fall-through to local =
   bug to fix before locking in.
7. **Remove the local SQLite `cp_*` writes** in a follow-up commit
   (the class methods become no-ops). Drop the SQLite tables once
   we're confident.

## Rollback at each step

- Step 1-2: nothing deployed → no rollback needed.
- Step 3 (dual_write enabled): flip env var back to `local`, restart.
- Step 4 (data migrated): cloud has more rows than before; harmless.
- Step 5 (cloud reads): flip env var back to `local`, restart. The
  cloud data stays in Supabase but is no longer authoritative.
- Step 6+: at this point Supabase has been canonical for an hour. Any
  rollback means accepting we lose any portal writes that happened
  during that hour. Treat the 1-hour observation as a hard gate.

## What this does NOT include

- **The portal frontend** is not in this repo. If it currently writes
  to Supabase directly with the service-role key (bypassing the
  FastAPI backend), that's its problem — and once everything is in
  Supabase, that direct-write path becomes the canonical path, no
  conflict.
- **The 4.9M historian/plc_readings rows** stay where they are. No
  per-customer tenancy backfill for them; new rows from upgraded
  edges will carry the right tenant_id automatically.
- **Lite app changes.** Lite already reads Supabase directly; no
  change needed.
