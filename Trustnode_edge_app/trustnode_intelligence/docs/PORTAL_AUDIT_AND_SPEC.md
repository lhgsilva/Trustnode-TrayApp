# Portal Audit — Modules + Licenses Editor

What the developer portal needs so module-based features (Batch Management,
Reporting, **TrustNode Intelligence**, future modules) can be:

1. **Catalogued** centrally with metadata (label, description, default state, per-license config schema).
2. **Attached** to a customer license with optional per-module config (e.g. AI endpoint URL).
3. **Validated** by the Edge automatically when the license bundle pushes down.

## Current State (audited from `App.jsx`)

### Portal → Modules page
- Read-only list. Shows `module_key`, `label`, `default_enabled`.
- Data source: `cp_license_modules` table.
- **No way to add/edit/delete modules from the portal UI.**
- **No per-module config schema.** The portal has no idea that `trustnode_intelligence` needs `endpoint_url + model + auth_token`.

### Portal → Licenses page
- List with columns: `license_id`, `customer_id`, `plan_code`, `max_edges/max_users`, window, `status`.
- Edit modal (`cpLicenseForm`) covers: license_id, customer_id, plan_code, status, start_utc, end_utc, max_edges, max_users.
- **Modules**: stored in a freeform textarea `cpLicenseModulesText` — no validation, no checkbox UI.
- **`module_configs` not in the form at all** — there's no way to set the AI endpoint URL, model, or auth token from the portal UI.
- License pushes to Edge via existing license-snapshot mechanism (well-tested).

### Gap summary
| What | Edge expects | Portal provides today |
|---|---|---|
| `modules` array | List of module_keys | Freeform text |
| `module_configs.<key>` | Per-module JSON config | Nothing |
| Per-module metadata | (label, description, requires fields) | Nothing |
| Module catalog editor | (add/remove modules in the registry) | Nothing |

## What the Portal Needs (proposed)

### A. New table: `cp_module_schemas`
Defines the **shape** of each module's `module_configs` blob. The portal license-editor renders fields from this schema; the Edge validates the saved config against it.

```sql
CREATE TABLE cp_module_schemas (
    module_key      TEXT PRIMARY KEY,            -- e.g. 'trustnode_intelligence'
    label           TEXT NOT NULL,               -- 'TrustNode Intelligence'
    description     TEXT NOT NULL DEFAULT '',
    icon            TEXT NOT NULL DEFAULT '',    -- icon hint for menus
    config_schema   TEXT NOT NULL DEFAULT '{}',  -- JSON Schema for module_configs.<key>
    default_config  TEXT NOT NULL DEFAULT '{}',  -- seed values for new licenses
    enabled_default INTEGER NOT NULL DEFAULT 0,  -- check by default in new licenses?
    sort_order      INTEGER NOT NULL DEFAULT 100,
    updated_utc     TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Seed entries (the modules we ship today):

| module_key | label | enabled_default | config_schema (summary) |
|---|---|---|---|
| `batch_management` | Batch Management | 0 | `{}` (no module config needed) |
| `reporting` | Reporting + Scheduled Reports | 0 | `{}` |
| `trustnode_intelligence` | TrustNode Intelligence (AI) | 0 | endpoint_url, model, auth_token, rate_limits.queries_per_day, rate_limits.max_tokens_per_query, features.insights, features.email_schedule, allowed_tools |

### B. Portal pages

**1. Modules Catalog** (replaces the read-only Modules page)
- Add / Edit / Delete module entries in `cp_module_schemas`.
- Form: module_key, label, description, icon, default_enabled, sort_order.
- JSON Schema editor (or simple key-value form) for `config_schema`.
- "Reset to ship defaults" button (re-seeds the 3 built-in modules).

**2. License Editor — Modules tab** (new tab inside the existing license edit modal)
- Checkbox list of every module from `cp_module_schemas`, sorted by `sort_order`.
- When a module is checked, render its `config_schema` as an inline form (right-hand panel or accordion).
- Save writes:
  - `license.modules` = `["batch_management", "trustnode_intelligence", ...]`
  - `license.module_configs.<key>` = the per-module form values
- Validation: required schema fields highlighted before Save.

**3. License preview**
- Show the JSON that will be pushed to the Edge, so the developer can see exactly what's being sent.

### C. Backend (Portal API) endpoints needed

| Method | Path | Purpose |
|---|---|---|
| GET    | `/api/control-plane/module-schemas` | List all module schemas (for the Modules + License editor pages) |
| POST   | `/api/control-plane/module-schemas` | Create new module schema |
| PATCH  | `/api/control-plane/module-schemas/{key}` | Update module schema |
| DELETE | `/api/control-plane/module-schemas/{key}` | Delete (only if no licenses reference it) |
| GET    | `/api/control-plane/licenses/{id}` | Already exists; ensure response includes `module_configs` |
| PUT    | `/api/control-plane/licenses/{id}` | Already exists; accept `module_configs` in the body |

### D. Edge-side validation (already partially done)

The Edge's `license_inspect.py` already reads `module_configs` (we added that earlier today). What's missing:

- **JSON Schema validation** — when the license bundle arrives, validate each `module_configs.<key>` against the schema the portal sent (or against the schema registered in the Edge's own module). Reject with a clear log line if invalid; the module degrades gracefully.

A small library: `jsonschema==4.x` (~50KB, pure Python). Optional — if missing, the Edge just trusts the portal.

## Implementation Order (recommended)

**Sprint 1 (portal-side, ~1 day)**
- Add `cp_module_schemas` table + seed the 3 known modules.
- Build Modules Catalog page (CRUD on `cp_module_schemas`).
- Build License Editor → Modules tab (checkbox list + inline config form).
- Extend `PUT /licenses/{id}` to round-trip `module_configs`.

**Sprint 2 (Edge-side, ~2 hours)**
- Add JSON Schema validation in `license_inspect._evaluate()`.
- Log warnings for invalid module_configs; module degrades gracefully.

**Sprint 3 (test, ~30 min)**
- Add `trustnode_intelligence` to mari's license via the new UI.
- Set the AI endpoint URL + token in the inline form.
- Save → license pushes to Edge → Edge picks up `module_configs` → Intelligence menu appears → Chat works.

## What I built today (already in place, ready for the portal to use)

| Layer | Status |
|---|---|
| Edge: `license_inspect.module_configs` parsing | ✅ |
| Edge: TrustNode Intelligence module reading `module_configs.trustnode_intelligence` | ✅ |
| Edge: `/api/intelligence/status` reports `licensed`, `endpoint_configured`, `features`, `rate_limits` | ✅ |
| Edge: Menu auto-hides when not licensed (already wired) | ✅ |
| Test license injection script (`smoke_test/inject_license.py`) | ✅ |
| Portal license blob template (`smoke_test/PORTAL_LICENSE_BLOB.md`) | ✅ |

When the portal Sprint 1 work lands, the Edge side is **already accepting the JSON** — no further Edge changes required for the basic flow.
