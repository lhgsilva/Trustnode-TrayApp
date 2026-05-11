# Trustnode Project Scan (Initial Baseline)

Date: 2026-05-07
Repository: `/workspace/Dolibarr-Trustnode`

## What was scanned

The current repository snapshot contains only two tracked files:

1. `htdocs/custom/test_chart.php`
2. `test` (newline-only placeholder)

No frontend app source files (such as `Trustnode_edge_app/frontend/...`) are present in this repository checkout.

## What the existing code currently does

### `htdocs/custom/test_chart.php`

This is a Dolibarr custom test page that:

- Boots Dolibarr context (`main.inc.php`) through several fallback path checks.
- Hides Dolibarr UI chrome (left menu, top menu, banner) for a focused chart view.
- Queries the `mfg_batch_read` table:
  - uses `tms` for timestamp display (`%H:%i`),
  - reads `part_tag_ref1` as numeric value,
  - gets the latest 20 rows,
  - reverses rows for chronological rendering.
- Renders a line chart titled **"Back Deflection angle"**.
- Loads Chart.js from a local custom path first, then CDN fallback.

This indicates the repository currently includes at least one data-visualization proof page for time-series industrial data.

## Interpreted product architecture (from your description)

Based on your description, the target platform appears to be:

1. **Local Windows Edge Gateway App**
   - Connects to PLCs, energy meters, and industrial devices.
   - Collects signals with gateway-side timestamps.
   - Buffers/stores mirrored raw data in a local database.
   - Continues collecting offline; syncs to cloud when internet is available.

2. **Cloud Data Layer**
   - Receives mirrored edge data with near-real-time targets (<2s latency).
   - Maintains tenant isolation (strict customer data separation + security boundaries).
   - Supports historian/reporting/analytics workloads.

3. **Portal / Control Plane (VPS-hosted)**
   - Manages customers, edge apps, licenses, modules, users, activation codes.
   - Provisioning and lifecycle management for tenants and deployments.

4. **Customer-Facing Web/App Experience**
   - Per-customer domain/app view.
   - Module-based UX and RBAC from DB-defined permissions.
   - Access to live and historical data from web/mobile/tablet.

## Current gap in this repo vs your stated app

This checkout does **not** currently include enough source code to perform a full technical scan of:

- edge collector services,
- protocol drivers (OPC-UA/Modbus/etc.),
- local buffer/sync queue logic,
- cloud ingest APIs,
- tenant security model,
- frontend pages/components referenced in your IDE context.

## Recommended next scan package

To perform the full architecture/quality scan you requested, add or point to the repositories/directories that contain:

1. Edge collector service code (device connectors, scheduler, buffering, retry/sync).
2. Local database schema and migrations.
3. Cloud ingest service/API and auth middleware.
4. Multi-tenant authorization/isolation implementation.
5. Customer portal + client web app source.
6. Deployment manifests (Windows service config, VPS stack, DB topology).

## Immediate improvements checklist (high impact)

When full code is available, prioritize validating:

- Exactly-once or idempotent cloud ingestion semantics.
- Clock synchronization + timestamp provenance (device vs gateway time).
- Store-and-forward durability under power/network interruptions.
- Tenant boundary enforcement at DB + API + app layers.
- RBAC consistency between edge/local/cloud portals.
- End-to-end latency budget instrumentation (<2s target).
- Data lineage/auditability for historian trust.

---

This document is an initial baseline scan of the repository content currently available in this environment.
