# Portal License Editor — Package Dropdown Spec

For the master-admin developer portal at
`https://trustnode.lsapps.app/developer-portal/`.

This document tells the portal team exactly what to add to the license
editor so sales can issue packaged licenses. The edge already
understands the schema (shipped 2026-06-23) — only the portal UI needs
to catch up.

## Goal

When the master-admin creates or edits a license, they pick a
**Package** from a dropdown. The portal preselects the default module
checkboxes and limit values for that package from
[LICENSE_PACKAGES.md](./LICENSE_PACKAGES.md). The admin can tick extra
modules or override limits before saving. On save, the portal pushes
the final `modules` + `limits` + `package_key` to the edge.

## License payload — required new fields

The license record in the cloud DB must add three fields:

| Field | Type | Default | Notes |
|---|---|---|---|
| `package_key` | text | `"edge"` for existing licenses | One of: `edge`, `operations`, `view.lan_only`, `view.web_only`, `cloud`, `enterprise`, or any custom string. Informational only on the edge; drives the portal dropdown. |
| `modules` | jsonb / json | unchanged for legacy | List of `{key, enabled}` entries. Existing `modules` columns may keep their shape. |
| `limits` | jsonb / json | `{}` for legacy | `{max_edges, max_gateways_per_edge, max_tags, max_studio_admins, max_view_users}`. Missing keys or `0` mean unlimited. |

The edge already reads all three from the `app_settings.license`
payload it receives via the cloud sync.

## UI changes

### 1. Add a "Package" dropdown above the existing module list

Position: at the top of the License Edit form, above the modules
section. Options (label / value):

- Edge / `edge`
- Operations / `operations`
- View LAN only / `view.lan_only`
- View Web only / `view.web_only`
- Cloud / `cloud`
- Enterprise / `enterprise`
- Custom / `custom` (no preselection)

On change → preselect the default module checkboxes and prefill the
limit input fields per the package's defaults in
[LICENSE_PACKAGES.md](./LICENSE_PACKAGES.md). Don't lock subsequent
edits — the admin can still tick extras or change numbers.

### 2. Limit input fields

Add five number inputs (or a "Limits" section with named rows):

- Max edges
- Max gateways per edge
- Max tags
- Max Studio admins
- Max View users

Empty / 0 = unlimited. Persist as integers in the license row's
`limits` jsonb column.

### 3. Module checkboxes — new keys

The catalog adds new keys the portal doesn't display today. Add them
to the module-tree UI under group headers:

**Studio (admin UI gating)**
```
studio.gateway_configuration
studio.tags
studio.devices
studio.triggers_limits
studio.dashboards_edit
studio.users_access
studio.backup_retention
studio.system_diagnostics
studio.batch_management
studio.oee                # future
studio.energy             # future
```

**View — local LAN**
```
view.lan
view.lan_dashboards
view.lan_historian
view.lan_alarms
view.lan_reports
view.lan_export
```

**View — web cloud**
```
view.web
view.web_dashboards
view.web_historian
view.web_alarms
view.web_reports
view.web_multi_site
```

**Cloud infrastructure**
```
cloud.database
cloud.hosting
cloud.multi_site_admin
```

The existing legacy keys (`cloud_database`, `lan_access`, `opcua`,
`mqtt`, `power_overview`, `power_configuration`, `batch_management`)
**must stay** alongside the new keys. The edge accepts both
representations during the rollover period.

### 4. Save behavior

On save, the license record's `modules` field becomes a deduped union
of (preselected from package) + (admin-ticked extras), each as
`{key: "...", enabled: true}`. Unticked checkboxes either omit the
key or write `{key, enabled: false}` — the edge treats both the same
way.

The `limits` field is the verbatim form values.

### 5. License view (read-only) — show the package

When viewing a license without editing, surface the package name
clearly (e.g. badge next to the license name: "Operations"). The
existing modules-table can stay as-is.

## Validation

The portal should reject these invalid combinations at save time:

- `package_key=enterprise` with any non-zero limit (Enterprise is
  unlimited by definition — show a warning, allow override).
- `view.lan_only` package with any `studio.*` module ticked (View LAN
  only excludes Studio).
- `view.web_only` package with `gateway_runtime` ticked (no edge
  runtime in that package).

Warnings only — don't hard-block. Sales sometimes need to issue weird
mixes.

## Backwards compatibility

Existing licenses that have no `package_key` and no `limits` block
**must keep working unchanged**. The edge already handles this:
treats them as `package_key=edge` with unlimited limits, and falls
back to the historical `grandfathered_modules` set for module
visibility.

The portal should:
1. Show "Edge (grandfathered)" in the package field for any pre-2026
   license without `package_key`.
2. NOT auto-rewrite these records on view — only set `package_key`
   when the admin saves the form.

## Acceptance checks

After the portal change ships, verify with one new license and one
old license:

1. New "Operations" license → edge's License Details panel shows
   `Package: operations`, Tags `0/100`, Gateways `0/5`, View users
   `0/3`.
2. New "Enterprise" license → edge shows `Package: enterprise`,
   everything `0 (unlimited)`.
3. Old pre-2026 license → edge shows `Package: edge`, all limits
   `(unlimited)`, plus the grandfathered modules listed.
4. Ticking off `studio.gateway_configuration` on an Operations
   license → Gateway Configuration page disappears from the edge nav
   for admins on that customer's edge within ~60 s (next license
   check).
5. Setting `max_view_users=1` and opening Lite from two different
   browsers → second login is rejected with `License limit reached`.

## Rollout

1. Ship portal change.
2. Reissue one existing customer's license with `package_key=edge`
   and matching limits. Verify the edge banner shows the new fields.
3. Reissue remaining customers gradually at renewal time. No rush —
   legacy payloads work indefinitely.

---

## Addendum 2026-08-21 — tier names and keys actually enforced by the edge

The edge evaluates `MODULE_CATALOG` keys (see the rewritten `LICENSE_PACKAGES.md`); the `studio.*` / `view.*` names above are accepted as aliases only.

**Package dropdown values (owner decision 2026-08-21):**

- TrustNode Edge / `edge`
- TrustNode Local View / `local_view`
- TrustNode Cloud View / `cloud_view`
- TrustNode Operations / `operations`
- TrustNode Enterprise / `enterprise`

**Two new checkboxes** (group "Cloud / Web", default OFF):

- `remote_admin_lan` — "TrustNode Edge over LAN (remote admin/engineer access)"
- `view_share_links` — "Local View share links (no-login tokens)"

**Rules the edge applies to payloads that carry a `package_key`:** a module entry without an explicit `enabled` is OFF; `local_view` must not tick `remote_admin_lan` nor any admin key; `cloud_view` must not tick `gateway_runtime_control`, `lan_access`, `local_web_app`, `remote_admin_lan`; `enterprise` limits are all 0 (unlimited).

**Mirror columns:** the edge stores `package_key` and `limits_json` on its `cp_licenses` mirror row (`control_plane_store.update_license_tier`); add the same two columns to the portal database so tiers can be reported in SQL.

**Refresh:** the edge re-pulls the licence when its last verification is older than 24 h; adding keys to the catalog no longer forces every edge to re-sync.
