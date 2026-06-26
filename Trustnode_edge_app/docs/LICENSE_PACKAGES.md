# TrustNode License Packages

Source of truth for the master-admin portal's license editor and the edge
enforcement code. When you add a new module key or change package
defaults, update this file in the same commit.

## License payload shape

Every license sent to an edge carries the following fields. The edge
reads these on every license check and gates UI/API access accordingly.

```json
{
  "license_id": "...",
  "package_key": "operations",
  "modules": [
    {"key": "historian", "enabled": true},
    {"key": "alarms", "enabled": true},
    {"key": "reporting", "enabled": true},
    {"key": "view.lan", "enabled": true},
    {"key": "view.web", "enabled": false}
  ],
  "limits": {
    "max_edges": 2,
    "max_gateways_per_edge": 5,
    "max_tags": 100,
    "max_studio_admins": 1,
    "max_view_users": 3
  },
  "start_utc": "...",
  "end_utc": "...",
  "modules_grandfathered": []
}
```

**`package_key`** is informational on the edge — it's used in the license
banner and audit log. All gating is driven by `modules` + `limits`. This
means sales can ship a "custom Edge with View Web" license without us
needing to invent a new package key.

**`modules`** is an explicit allow-list. A key not in this list is OFF.
Legacy installs with no modules carry `modules_grandfathered` so existing
features keep working until the customer's license is reissued.

**`limits`** drives numeric enforcement. A field missing or set to `0` or
`null` means "no limit enforced for this license".

## Module key catalog

Every key the edge knows about. New keys MUST be added here and given a
default per-package value. Legacy keys (cloud_database, lan_access, etc.)
remain as-is for back-compat.

### Core (almost always on)
| Key | What it gates |
|---|---|
| `historian` | Historian Live + Export pages, range queries |
| `alarms` | Alarms page + alarm engine |
| `reporting` | Reports + Scheduled Reports pages |
| `notifications` | Email notifications |
| `data_log` | Data History menu group |
| `interface` | Interface settings page |
| `gateway_runtime` | Gateway start/stop, PLC collection |

### Studio (admin-only configuration)
| Key | What it gates |
|---|---|
| `studio.gateway_configuration` | Gateway Configuration page |
| `studio.tags` | Tag catalog page |
| `studio.devices` | Device catalog page |
| `studio.triggers_limits` | Triggers and Limits page |
| `studio.dashboards_edit` | Dashboard designer edit mode |
| `studio.users_access` | User and Access Control |
| `studio.backup_retention` | Backup and Retention page |
| `studio.system_diagnostics` | Edge / Diagnostics pages |
| `studio.batch_management` | Batch Management module config |
| `studio.oee` | OEE configuration (future) |
| `studio.energy` | Energy configuration (future) |

### View — local LAN read-only (the renamed "Lite")
| Key | What it gates |
|---|---|
| `view.lan` | Lite UI at `/lite/` is enabled |
| `view.lan_dashboards` | Dashboard view on Lite |
| `view.lan_historian` | Historian view on Lite |
| `view.lan_alarms` | Alarm view on Lite |
| `view.lan_reports` | Report view on Lite |
| `view.lan_export` | Export buttons in Lite views |

### View — hosted web cloud read-only
| Key | What it gates |
|---|---|
| `view.web` | The customer's cloud portal view |
| `view.web_dashboards` | Cloud dashboards |
| `view.web_historian` | Cloud historian |
| `view.web_alarms` | Cloud alarms |
| `view.web_reports` | Cloud reports |
| `view.web_multi_site` | Multi-site selector on the cloud portal |

### Cloud infrastructure
| Key | What it gates |
|---|---|
| `cloud.database` | Cloud DB sync card + write path (legacy: `cloud_database`) |
| `cloud.hosting` | The edge is hosted as a cloud service vs on-prem |
| `cloud.multi_site_admin` | Master-admin can manage other customers (portal users only) |

### Edge integrations / extras
| Key | What it gates |
|---|---|
| `lan_access` | LAN Sharing toggle + endpoint (legacy: existing) |
| `opcua` | OPC UA server endpoint (legacy: existing) |
| `mqtt` | MQTT broker endpoint (legacy: existing) |
| `power_overview` | Power Overview page (legacy: existing) |
| `power_configuration` | Power Configuration page (legacy: existing) |
| `batch_management` | Batch Management module (existing) |

## Numeric limits

| Field | Meaning | Enforced where |
|---|---|---|
| `max_edges` | How many physical edges this license activates | Cloud portal (registration) |
| `max_gateways_per_edge` | Gateways one edge can run | Edge: `/api/plc/gateways/start` |
| `max_tags` | Total configured tags across all gateways on the edge | Edge: tag save + gateway start |
| `max_studio_admins` | Max admin/engineer accounts | Edge: user create |
| `max_view_users` | Max concurrent View (LAN + web) sessions | Edge: session issue / cloud auth |

Limits set to `0`, `null`, or absent mean "unlimited". This is the
fail-open default so a legacy license payload doesn't suddenly start
rejecting things.

## Packages

### Package: `edge`
Single-edge, single-operator, on-premise. The starter SKU.

**Default modules ON:**
- `historian`, `alarms`, `reporting`, `notifications`, `data_log`, `interface`, `gateway_runtime`
- `studio.gateway_configuration`, `studio.tags`, `studio.devices`, `studio.triggers_limits`, `studio.dashboards_edit`, `studio.users_access`, `studio.backup_retention`, `studio.system_diagnostics`
- `view.lan`, `view.lan_dashboards`, `view.lan_historian`, `view.lan_alarms`, `view.lan_reports`

**Default modules OFF (available as add-ons):**
- All `cloud.*` (no cloud sync by default)
- All `view.web.*`
- `studio.batch_management`, `studio.oee`, `studio.energy`
- `lan_access`, `opcua`, `mqtt`
- `power_overview`, `power_configuration`

**Default limits:**
```
max_edges: 1
max_gateways_per_edge: 2
max_tags: 50
max_studio_admins: 1
max_view_users: 1
```

### Package: `operations`
Two edges, supervisor team, LAN-shared View. Mid-tier.

**Default modules ON:** everything in `edge` PLUS:
- `lan_access` (LAN sharing for the View clients)
- `view.lan_export` (operators can export from Lite)
- `studio.batch_management` (Batch module enabled by default)

**Default limits:**
```
max_edges: 2
max_gateways_per_edge: 5
max_tags: 100
max_studio_admins: 1
max_view_users: 3
```

### Package: `view.lan_only`
Pure read-only LAN install — the edge runs but no admin UI is offered to
the customer. Useful for "this edge is locked down, only the OEM admins
who installed it can change it remotely" deployments.

**Default modules ON:**
- `historian`, `alarms`, `reporting`, `gateway_runtime`, `data_log`
- `view.lan`, `view.lan_dashboards`, `view.lan_historian`, `view.lan_alarms`, `view.lan_reports`

**Default modules OFF:**
- ALL `studio.*` (admin UI is hidden — admins must log in remotely via the portal)

**Default limits:**
```
max_edges: 1
max_gateways_per_edge: 2
max_tags: 50
max_studio_admins: 0
max_view_users: 3
```

### Package: `view.web_only`
The customer has no edge, only cloud-portal access (e.g. they're a parent
company viewing a subsidiary's edges).

**Default modules ON:**
- `view.web`, `view.web_dashboards`, `view.web_historian`, `view.web_alarms`, `view.web_reports`
- `cloud.database`

**Default modules OFF:** all `studio.*`, all `view.lan.*`, `gateway_runtime`.

**Default limits:**
```
max_edges: 0
max_view_users: 5
```

### Package: `cloud`
Cloud-hosted Edge service. TrustNode runs the edge for the customer.

**Default modules ON:** everything in `operations` PLUS:
- `cloud.hosting`
- `cloud.database`
- `view.web`, `view.web_dashboards`, `view.web_historian`, `view.web_alarms`, `view.web_reports`

**Default limits:**
```
max_edges: 5
max_gateways_per_edge: 10
max_tags: 500
max_studio_admins: 3
max_view_users: 10
```

### Package: `enterprise`
Unlimited multi-site, cloud-managed.

**Default modules ON:** everything in `cloud` PLUS:
- `view.web_multi_site`
- `cloud.multi_site_admin`

**Default limits:**
```
max_edges: 0          # 0 = unlimited
max_gateways_per_edge: 0
max_tags: 0
max_studio_admins: 0
max_view_users: 0
```

## How the portal license editor uses this

1. User picks a package from a dropdown.
2. The portal preselects the default module checkboxes and limit values
   from this catalog.
3. User can tick additional optional modules or change limit numbers
   before saving.
4. On save, the portal serializes the final `modules: [...]` + `limits:
   {...}` + `package_key` and pushes the signed license payload to the
   edge (or stores it on the master license record for cloud-only).

The edge does NOT need to know what packages exist. It just reads the
final `modules` + `limits` and gates accordingly. Add a new package next
month → no edge change.

## Grandfathering rules

A legacy license (no `package_key`, no `modules` list) is treated as:
- `package_key = "edge"` (informational only)
- `modules` = whatever the edge previously inferred (grandfathered list
  from `app_settings.grandfathered_modules`)
- `limits` = all unlimited

This guarantees we don't break a running customer when the new schema
ships. The customer's license is reissued at next renewal.
