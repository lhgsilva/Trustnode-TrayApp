# Master Prompt: Exact Edge-Like Single-File Client Pages

Build hosted client pages that visually and functionally mirror the Trustnode Edge frontend for these 4 modules:

1. `Dashboard`
2. `Power Overview`
3. `Historian`
4. `Reporting`

Generate these standalone files:
- `client_test.html` (direct API mode)
- `client_test.php` (same UI, with built-in PHP proxy endpoint)
- `client_test_db_rest.html` (same UI, DB REST backed)
- `client_test_db_php.html` (same UI, PHP DB endpoint backed)

No local asset files. CDN libs are allowed.

## Mandatory Layout Parity
- Dark edge-style shell:
  - top black header
  - left sidebar nav
  - content cards with rounded corners and subtle borders
- Typography, spacing, controls and card density must closely match edge screenshots.
- Responsive behavior: desktop first, mobile fallback.

## Mandatory Feature Parity
### Dashboard
- Add Item flow to create widgets from gateway/device/tag.
- Widgets must support:
  - chart mode line/bar
  - KPI mode
  - delete
  - monitor popup
- Grid density control (`Per Row`).
- Show last value, timestamp, status context.
- Live updates with bounded time window.

### Power Overview
- Realtime/Historical mode tabs.
- Filters: meter, period, interval, aggregation.
- KPI row with:
  - Energy Efficiency
  - Energy Costs
  - Total kWh Consumption
  - Live kW Consumption
  - Peak Demand Indicator
  - Downtime Energy Cost
- Two main cards:
  - left: metric chart with metric selector + line/bar toggle
  - right: total consumption by hour with window selector + line/bar toggle
- Meters table with status and include-in-chart selection.

### Historian
- Filters: from, to, tag, gateway, device.
- Export CSV and JSON.
- Scroll table with columns:
  - timestamp, tag, value, quality, device, gateway, database, plc.

### Reporting
- 40/60 split layout:
  - left: report filters + generated reports
  - right: chart + data series table
- Filters include:
  - datetime range
  - max rows
  - batch/source
  - interval
  - aggregation
  - gateway checklist
  - tag checklist + axis + color rows
- Actions:
  - Load Data
  - Generate CSV
  - Generate PDF (or placeholder event creating report record)
- Saved reports list with delete action.

## Data & Live Strategy
- Prefer WebSocket if available; otherwise polling fallback.
- Polling must be bounded and efficient.
- Deduplicate rows by `timestamp + gateway + tag`.
- Keep in-memory ring buffer per chart.
- Render timestamps as `DD/MM/YYYY HH:MM:SS`.
- Numeric values max 3 decimals.

## Security
- Never expose DB superuser/service keys in browser code.
- For PHP mode, use server-side proxy for auth and data calls.
- Respect tenant boundaries and readonly behavior for dashboard pages.

## Output Quality Rules
- Do not output simplified placeholder layout.
- Match edge visual structure and interaction patterns as closely as possible.
- Keep code in one file per variant.
