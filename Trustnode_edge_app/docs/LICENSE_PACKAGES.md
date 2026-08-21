# TrustNode License Packages

Source of truth for the master-admin portal's license editor and the edge
enforcement code. When you add a new module key or change package defaults,
update this file in the same commit.

> **2026-08-21 rewrite.** The earlier version of this document described a
> `studio.*` / `view.lan*` / `view.web*` vocabulary that was never implemented
> on the edge. The edge evaluates the keys of `MODULE_CATALOG`
> (`backend/app/services/control_plane_store.py`) — the list the portal already
> renders. This file now documents **those** keys; the old names remain
> accepted as aliases (`license_inspect._MODULE_ALIASES`) so a portal that
> emits either spelling gates the same features. Server-side enforcement of
> modules, roles and network origin lives in
> `backend/app/services/access_policy.py` (see
> `docs/edge-runtime-lan-access-and-view-licensing-plan-2026-08-21.md`).

## License payload shape

```json
{
  "license_id": "...",
  "package_key": "edge",
  "modules": [
    {"key": "historian", "enabled": true},
    {"key": "lan_access", "enabled": true},
    {"key": "local_web_app", "enabled": true},
    {"key": "remote_admin_lan", "enabled": true},
    {"key": "view_share_links", "enabled": false}
  ],
  "limits": {
    "max_edges": 1,
    "max_gateways_per_edge": 2,
    "max_tags": 100,
    "max_studio_admins": 2,
    "max_view_users": 3
  },
  "start_utc": "...",
  "end_utc": "...",
  "modules_grandfathered": []
}
```

- **`package_key`** is informational on the edge (banner, audit log, portal dropdown). Gating is driven by `modules` + `limits`.
- **`modules`** is an explicit allow-list. **When `package_key` is present, a key whose `enabled` is absent counts as OFF.** Legacy payloads (no `package_key`) keep the permissive reading (absent = ON) so no running customer changes behaviour until the licence is reissued.
- **`limits`**: `0`, `null` or absent = unlimited.
- Licences are ED25519-signed by the portal; the edge refreshes the mirror when the last verification is older than 24 h (not on catalog growth).

## Products (what sales sells) → package keys

| Product | `package_key` | Summary |
|---|---|---|
| **TrustNode Edge** | `edge` | the runtime + admin configuration on the edge machine; with `remote_admin_lan` the complete software is also reachable from any PC on the LAN with an admin/engineer login |
| **TrustNode Local View** | `local_view` | read-only dashboards + reports in the browser on the LAN (login required), data from the locally configured database |
| **TrustNode Cloud View** | `cloud_view` | read-only dashboards + reports over the internet, data from the cloud database |
| TrustNode Operations | `operations` | Edge + Local View for a supervisor team (bundle) |
| TrustNode Enterprise | `enterprise` | unlimited, multi-site, cloud-managed (bundle) |

## Module key catalog (`MODULE_CATALOG`)

### Gateways
| Key | What it gates |
|---|---|
| `tags` | Tag catalog page |
| `gateway_configuration` | Gateway Configuration / Devices pages |
| `gateway_runtime_control` | Gateway start/stop, PLC collection (a pure View licence without it cannot write data — `license_gate.is_data_writes_allowed` → `view_only_license`) |
| `plc_drivers` | Allen-Bradley / Siemens / Modbus drivers |
| `meter_drivers` | Power-meter drivers |

### Data, visualisation, alarms, reports
| Key | What it gates |
|---|---|
| `dashboard`, `custom_dashboards` | dashboards / dashboard designer |
| `historian`, `historian_export` | Historian pages / exports |
| `triggers_limits`, `alarms`, `email_notifications` | limits & triggers, alarm engine, e-mail |
| `reporting`, `scheduled_reports`, `report_templates` | Reports |
| `power_overview`, `power_management`, `oee_downtime` | Power / OEE |
| `interface` | Interface settings |

### Admin
| Key | What it gates |
|---|---|
| `database`, `local_database`, `cloud_database` | Database page, local write access, cloud sync |
| `users_and_access_control` | User and Access Control |

### Connections
| Key | What it gates |
|---|---|
| `connections` | Connections page |
| `lan_access` | Remote Access may bind a LAN listener ("LAN Sharing & LAN Web Access") |
| `opcua`, `mqtt` | OPC UA server / MQTT broker |

### Cloud / Web — the View tiers
| Key | What it gates (server-side) |
|---|---|
| `local_web_app` | **TrustNode Local View**: `/trustnode/client/app/` (+ legacy `/trustnode/lite/app/`) and `/api/lite-local/*` |
| `remote_admin_lan` | **TrustNode Edge over LAN**: `/trustnode/full/app/` and every configuration mutation from a non-loopback address (admin/engineer login still required) |
| `view_share_links` | no-login view-link tokens for Local View (`/api/lite-view/resolve/`, `?token=`); without it Local View always requires a login |
| `cloud_lite_access` | **TrustNode Cloud View** (read-only cloud portal) |
| `cloud_client_view` | Cloud View — React client view / multi-site |

### Applications / AI
| Key | What it gates |
|---|---|
| `batch_management` | Batch Management module (404 when absent) |
| `trustnode_intelligence` | AI assistant (fails closed) |

### Legacy licences (no `package_key`)
`remote_admin_lan` is derived as `lan_access AND local_web_app`; `view_share_links` as `lan_access`. New tier licences must carry both explicitly.

### Aliases accepted from older portal builds
`view.lan*` → `local_web_app` · `view.lan_export` → `historian_export` · `view.web*` → `cloud_lite_access` · `view.web_multi_site` → `cloud_client_view` · `cloud.database`/`cloud.hosting` → `cloud_database` · `studio.gateway_configuration` → `gateway_configuration` · `studio.tags` → `tags` · `studio.triggers_limits` → `triggers_limits` · `studio.dashboards_edit` → `custom_dashboards` · `studio.users_access` → `users_and_access_control` · `studio.backup_retention`/`studio.system_diagnostics` → `database` · `studio.batch_management` → `batch_management` · `studio.remote_admin`/`lan_admin` → `remote_admin_lan` · `gateway_runtime` → `gateway_runtime_control` · `notifications` → `email_notifications`.

## Numeric limits

| Field | Meaning | Enforced where |
|---|---|---|
| `max_edges` | physical edges this licence activates | portal (registration) |
| `max_gateways_per_edge` | gateways one edge can run | edge: `/api/plc/gateways/start` |
| `max_tags` | configured tags across all gateways | edge: tag save + gateway start |
| `max_studio_admins` | admin/engineer/super accounts | edge: user create (`control_plane.upsert_user`) |
| `max_view_users` | concurrent View sessions (5-min liveness window) | edge: login (`view_sessions`) |

## Package defaults (portal dropdown preselection)

### `edge` — TrustNode Edge
ON: `dashboard`, `custom_dashboards`, `historian`, `historian_export`, `triggers_limits`, `alarms`, `email_notifications`, `reporting`, `scheduled_reports`, `report_templates`, `interface`, `tags`, `gateway_configuration`, `gateway_runtime_control`, `plc_drivers`, `database`, `local_database`, `users_and_access_control`, `connections`, `lan_access`, `local_web_app`, `remote_admin_lan`.
OFF (add-ons): `cloud_*`, `view_share_links`, `opcua`, `mqtt`, `meter_drivers`, `power_*`, `oee_downtime`, `batch_management`, `trustnode_intelligence`.
Limits: `max_edges 1`, `max_gateways_per_edge 2`, `max_tags 50`, `max_studio_admins 2`, `max_view_users 3`.

### `local_view` — TrustNode Local View
ON: `dashboard`, `historian`, `alarms`, `reporting`, `report_templates`, `lan_access`, `local_web_app`, plus `gateway_runtime_control`/`plc_drivers` **only when the same edge also runs collection under an Edge licence** (a pure Local View licence has no `gateway_runtime_control` and cannot write data).
OFF: every admin/studio key, `remote_admin_lan`, `cloud_*`.
Limits: `max_view_users 3`, `max_studio_admins 0`.

### `cloud_view` — TrustNode Cloud View
ON: `cloud_database`, `cloud_lite_access`, `cloud_client_view`, `dashboard`, `historian`, `alarms`, `reporting`.
OFF: everything local (`gateway_runtime_control`, `lan_access`, `local_web_app`, `remote_admin_lan`, studio keys).
Limits: `max_edges 0`, `max_view_users 5`.

### `operations`
Everything in `edge` + `batch_management`, `historian_export`, `view_share_links`; limits `max_edges 2`, `max_gateways_per_edge 5`, `max_tags 100`, `max_studio_admins 3`, `max_view_users 5`.

### `enterprise`
Everything in `operations` + all `cloud_*`, `opcua`, `mqtt`, `meter_drivers`, `power_*`, `oee_downtime`, `trustnode_intelligence`; all limits `0` (unlimited).

## Roles vs. network origin (enforced by `access_policy`)

| | desktop (loopback) | LAN with `remote_admin_lan` | LAN without it |
|---|---|---|---|
| `admin` / `super` | everything | everything | reads + Local View only |
| `engineer` | configuration (not users/licence/LAN) | same | reads + Local View only |
| `operator` | reads + operational actions (alarm ack, batch, reports, gateway start/stop) | same | same |
| `viewer` | reads | reads | reads |

Network origin never grants rights; it can only remove them. `TRUSTNODE_RBAC_MODE` / `TRUSTNODE_LICENSE_GATES` = `lan` (default: enforce remote, log-only loopback) | `enforce` | `log` | `off`.

## Grandfathering rules

A legacy licence (no `package_key`) is treated as `package_key = "edge"` for the banner, its `modules` are read permissively (absent `enabled` = ON), `limits` are unlimited, and the two 2026-08-21 keys are derived as described above. The customer's licence is reissued with explicit keys at the next renewal.
