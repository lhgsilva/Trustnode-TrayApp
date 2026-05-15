# Client View — single-file customer portal

`client_view.html` is a **single self-contained HTML file** (~1.5 MB) that is
the exact TrustNode Edge React UI built in cloud-readonly + client-view
mode. JS and CSS are inlined; no external CDN calls. Drop the file under
any domain and it works.

## What it shows

The customer's `admin` for that tenant decides which modules each cloud
user can see (per-user permission keys, same flow used today in the
desktop Users and Access page). The single-file build hides every admin
surface and renders only the customer-facing modules the JWT permits:

| Module          | Permission keys (any one)                          |
| --------------- | -------------------------------------------------- |
| Dashboards      | `dashboard`                                        |
| Power Overview  | `power_overview`                                   |
| Historian       | `historian` or `data_log`                          |
| Alarms          | `client_module_alarms` or `alarms`                 |
| Reporting       | `client_module_reporting` or `reporting`           |
| Interface       | `client_module_interface` or `interface`           |

If the customer's admin enables an extra module later, the same JWT path
exposes it — no rebuild of the file is needed.

## How it is built

```powershell
cd D:\Trustnode\Trustnode-AB\Tray_app\Trustnode_edge_app
powershell -ExecutionPolicy Bypass -File .\scripts\build-client-view-singlefile.ps1 `
  -CloudApiUrl "https://trustnode.lsapps.app"
```

The script runs `npm run build:clientview`, which Vite handles by:

* Setting `VITE_TRUSTNODE_READONLY=true` (admin-write paths blocked client-side)
* Setting `VITE_TRUSTNODE_CLIENT_VIEW=true` (top-level admin menus hidden)
* Setting `VITE_TRUSTNODE_FORCE_CLOUD_URL=<cloud>` (API base baked in)
* Loading `vite-plugin-singlefile` to inline JS + CSS into one HTML.

CI/CD ships the artefact to `https://trustnode.lsapps.app/client/client_view.html`.

## Where to deploy

Anywhere. The file is self-contained — open it via `file://`, host it on
the customer's own website, drop it on a CDN, or serve it from the VPS
under any customer subdomain. The cloud URL is baked into the bundle, so
the page does not depend on its hosting origin.

## Security posture

| Concern                       | How it is enforced                                                  |
| ----------------------------- | ------------------------------------------------------------------- |
| Tenant separation             | Cloud backend resolves tenant from host + JWT, RLS keys every read. |
| No admin surface              | `isClientViewMode()` short-circuits `canOpenPage` to client modules.|
| No service keys in browser    | The page holds only the user JWT (in `localStorage`).               |
| Read-only UI                  | `isForcedReadonlyCloudMode()` disables every write button.          |
| Permissions still authoritative on server | Backend re-checks JWT claims on every request.          |

## Cloud-sync tunings

| Env var                              | Default | Effect                                                    |
| ------------------------------------ | ------- | --------------------------------------------------------- |
| `TRUSTNODE_LIVE_SYNC_SECONDS`        | `0.10`  | Edge → Supabase `live_latest` push cadence.               |
| `TRUSTNODE_CLOUD_LIVE_SSE_MS`        | `250`   | SSE tick on the cloud backend (minimum 100 ms).           |
| `TRUSTNODE_CLOUD_LIVE_SSE_LIMIT`     | `200`   | Max rows fetched per tick (request override capped 800).  |
