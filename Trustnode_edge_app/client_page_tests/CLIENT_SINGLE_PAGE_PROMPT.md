# Reusable Prompt: Build Single-File TrustNode Client Pages (HTML + PHP)

You are building **two standalone test client pages** for TrustNode data visualization:

1. `client_test.html` (pure static HTML + JS)
2. `client_test.php` (single-file PHP + JS)

## Goal
Create client pages that customers can drop into their hosting and use to view TrustNode cloud data in near real time.

## Hard Requirements
- Single-file only (no local extra JS/CSS assets).
- Include 4 navigable views in each file:
  - Dashboard
  - Reporting
  - Historian
  - Power Management Overview
- Use visual style aligned with TrustNode edge frontend palette (dark/light friendly, card layout, clean industrial UI).
- Real-time feeling with automatic refresh and chart updates.
- Keep polling efficient and safe for many concurrent clients.
- Must work with TrustNode cloud API endpoints, not direct DB credentials in browser.

## API Endpoints to Use
- `POST /api/auth/login`
- `GET /api/app-store/live?limit=...`
- `GET /api/app-store/historian?limit=...`
- `GET /api/app-store/logs?limit=...`
- `GET /api/power/latest?device_id=...`
- `GET /api/power/history?limit=...&device_id=...`
- Optional live push when available:
  - `wss://<host>/ws/cloud-live?token=<jwt>`

## Data/Latency Strategy (must implement)
Use a hybrid model:
1. **Primary**: WebSocket (`/ws/cloud-live`) for low latency updates.
2. **Fallback**: Polling with adaptive cadence and jitter.
   - Dashboard/Power: every 2s
   - Historian: every 4-5s
   - Reporting: on-demand + optional auto-refresh every 10-15s
3. Pause/reduce polling when tab is hidden (Page Visibility API).
4. De-duplicate and order rows by timestamp + tag + gateway to avoid flicker/backward motion.
5. Keep in-memory rolling windows (do not re-render massive arrays each tick).

## SQL/Backend Query Best Practices (for scalability)
- Do **not** query raw large history every second.
- For live cards/charts use latest snapshot endpoint (`/api/app-store/live` and `/api/power/latest`).
- For historian/reporting use bounded windows (`limit`, time range filters where available).
- Prefer server-side pagination and aggregation (minute/hour/day) instead of client-side full scans.
- Ensure indexes exist on: `(gateway_id, ts)`, `(tag, ts)`, `(device_id, ts)`.

## Security Rules
- Never expose direct DB credentials in client pages.
- Use JWT from `/api/auth/login`.
- Keep auth token in memory/session; avoid long-lived localStorage when possible.
- For PHP page, add optional same-file proxy mode using PHP session to bypass CORS safely.
- Separate human login from machine ingest path.

## UI Behavior Requirements
### Dashboard
- KPIs: live count, gateways, avg sample age, last update time.
- Live chart with rolling series (time on X, value on Y).

### Historian
- Filter by tag text.
- Table with timestamp, gateway, tag, value, quality.
- Auto-refresh toggle.

### Reporting
- Time range selector + tag filter.
- Aggregation selector (raw/minute/hour/day).
- Chart and table summary.

### Power Management Overview
- Cards: Voltage, Current, Active Power, Energy, PF, Frequency.
- Power trend chart and energy trend chart.

## Deliverables
- `client_test.html`
- `client_test.php`
- Both files fully runnable as-is.
- Include inline comments for where to configure cloud base URL.

## Quality Constraints
- No framework build step.
- Keep code modular inside each file (small helper functions).
- Graceful error states and reconnect logic.
- Keep UI responsive for desktop and mobile.
