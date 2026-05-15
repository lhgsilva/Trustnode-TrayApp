# Client Portal Lite — operator notes

`client_portal_lite.html` is a slim single-file shell intended to live under a
customer's own domain (e.g. `dashboard.customerco.com`) or under a per-customer
subdomain on our VPS (`customer-x-trustnode.lsapps.app`). It does NOT contain
admin UI and cannot reach admin-only endpoints (those require
`role=admin` claims that the customer login JWT does not carry).

## Data flow

```
Customer browser
    │
    ├─ GET  /api/control-plane/portal-context     → resolves tenant by host
    ├─ POST /api/auth/login                       → tenant-scoped JWT
    ├─ GET  /api/auth/me                          → restore session
    ├─ EventSource /api/cloud-live/stream?token=… → live deltas (SSE)
    └─ iframe /portal/                            → existing read-only SPA
```

The shell never touches the edge directly. It speaks only to the cloud
backend on the same host it was served from (`window.location.origin`),
so a customer site embedding the file inherits same-origin isolation.

## Cyber-security posture

| Concern                          | How it is enforced                                              |
| -------------------------------- | --------------------------------------------------------------- |
| Tenant separation                | `resolve_request_tenant()` maps host→tenant; JWT `tenant_id` must match. Mismatch ⇒ 403. |
| Database isolation               | Supabase RLS policies key every read off `tenant_id`.           |
| No service keys in browser       | The page uses only the user JWT; service-role keys live on VPS. |
| SSE auth                         | `CloudLiveAuthMiddleware` (pure ASGI) validates Bearer / `?token=` before the stream opens. |
| No admin surface                 | Customer JWTs lack admin permissions; admin routes 403 even if probed. |
| Local-storage scoping            | Token is stored under a per-host key, so multiple customer sessions in the same browser do not collide. |
| Iframe sandboxing                | `<iframe sandbox="allow-forms allow-popups allow-scripts allow-same-origin">` keeps the SPA contained. |

## Cloud-sync tunings

| Env var                              | Default | Effect                                                    |
| ------------------------------------ | ------- | --------------------------------------------------------- |
| `TRUSTNODE_LIVE_SYNC_SECONDS`        | `0.10`  | How often the edge pushes `live_latest` rows to Supabase. |
| `TRUSTNODE_CLOUD_LIVE_SSE_MS`        | `250`   | SSE tick on the cloud backend (minimum 100 ms).           |
| `TRUSTNODE_CLOUD_LIVE_SSE_LIMIT`     | `200`   | Max rows fetched per tick (request override capped 800).  |

The SSE endpoint emits a `snapshot` event on first connect and then only
delta rows whose timestamp is newer than the previous tick, so wire and
browser cost stay flat regardless of tag count.

## Why SSE (not WebSocket)

* One-way push is all a dashboard needs.
* SSE traverses corporate proxies cleanly (plain HTTP).
* `EventSource` auto-reconnects with no client code.
* Nginx only needs `proxy_buffering off` (no `Upgrade` rules).

## Architectural note: bypassing BaseHTTPMiddleware

`StreamingResponse` cannot pass through Starlette's `BaseHTTPMiddleware`
(used by `@app.middleware("http")`) — it buffers the entire response and
trips `RuntimeError: No response returned.` on long-lived generators.

To keep the existing middleware (auth, tenant resolution, no-cache
headers) for normal requests AND let SSE stream, `app.main` exposes a
top-level ASGI dispatcher (`_AsgiDispatcher`) that routes
`/api/cloud-live/*` to a sibling FastAPI instance with its own pure-ASGI
auth middleware. Lifespan and every other request still flow through the
unchanged main app.
