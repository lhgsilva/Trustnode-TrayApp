# Master Prompt: Rebuild TrustNode Client As True Single-File Hosted Pages (HTML + PHP)

You are generating hosted client pages that must mirror the TrustNode Edge frontend behavior for these modules:

1. Dashboard
2. Reporting
3. Historian
4. Power Management Overview

Output **single-file pages only** (no extra local assets besides optional CDN chart library):
- `client_test.html` (API/WebSocket mode)
- `client_test.php` (API proxy mode via same file)
- `client_test_db_rest.html` (DB REST mode)
- `client_test_db_php.html` (DB PHP endpoint consumer)

## Core Requirements
- Match TrustNode style tokens and structure:
  - dark/light mode
  - header + left nav + content cards
  - KPI cards, chart cards, table cards
- Include full navigation across the 4 pages.
- Include auth/connection section.
- Must preserve low-latency “live feeling”.
- DD/MM/YYYY HH:MM:SS timestamp display.
- Numeric display max 3 decimals.
- CSV/JSON exports where relevant.

## Functional Parity Requirements
### Dashboard
- Gateway/device/tag selectors.
- Rolling chart.
- KPI cards: rows, gateways, devices, tags, sample age, current value.
- Auto refresh.

### Historian
- Filters: from/to, gateway, device, tag, quality.
- Scroll table with latest rows.
- Export CSV and JSON.
- Refresh button.

### Reporting
- Filters: from/to, gateway, tag search.
- Aggregation interval: raw/minute/hour/day.
- Aggregation method: avg/sum/min/max.
- Chart + side table.
- Export CSV.

### Power Management Overview
- KPI cards: V, A, kW, kWh, PF, Hz.
- Two charts (primary metric + energy trend).
- Metric selector and line/bar selector.
- Device selector and history window.

## Data Strategy (must implement)
Use hybrid live architecture:
- WebSocket first (when endpoint exists) for push updates.
- Polling fallback with adaptive intervals and jitter.
- Reduce polling when tab is hidden.
- De-duplicate rows by timestamp+gateway+tag to avoid flicker.
- Keep bounded in-memory windows.

## Modes
### 1) API/WebSocket mode (preferred)
Use endpoints:
- POST /api/auth/login
- GET /api/app-store/live
- GET /api/app-store/historian
- GET /api/app-store/logs
- GET /api/power/latest
- GET /api/power/history
- WS /ws/cloud-live

### 2) DB REST mode
- Query readonly views/tables only.
- No service-role key in browser.
- Respect RLS.
- Map response to TrustNode-like row schema.

### 3) DB PHP endpoint mode
- Browser never sees DB credentials.
- Frontend calls PHP endpoint (e.g. `client_test_db.php?dbq=...`).
- PHP handles SQL + safe parameter binding + limits.

## Security
- No hardcoded DB superuser credentials in JS.
- JWT/session token handling.
- Readonly querying for hosted dashboards.
- No direct OT access.

## Performance / Scale Requirements
- Never full-scan raw history every second.
- Use limits and bounded windows.
- Aggregate server-side when possible.
- Keep chart updates incremental and non-animated.

## Deliverables
- All four files fully runnable standalone.
- Inline comments for required config fields.
- No broken references to local assets.
- Responsive desktop/mobile layout.
