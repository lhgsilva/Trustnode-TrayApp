# Master Prompt: Four Single-File Client Variants (Exact Edge UI Copy)

Generate **4 separate single-file clients**, all using the exact current edge frontend bundle as source of truth (same layout, fonts, cards, controls, charts, pages, popups, and behavior), with only data transport changed.

## Required output files

1. `client_test.html`  
   HTML single file using **App API** (`/api/*` + `/ws/*` remapped to cloud base).

2. `client_test_db_rest.html`  
   HTML single file using **direct cloud DB data** (Supabase/PostgREST style for data endpoints), while keeping API fallback for non-data app endpoints.

3. `client_test.php`  
   PHP single file using **App API** via server proxy (`?proxy=...`).

4. `client_test_db.php`  
   PHP single file using **direct cloud DB queries** (PDO SQL on server) for data endpoints, with API proxy fallback for the rest.

## Source-of-truth rule (non-negotiable)

Do not hand-recreate UI.  
Always embed the exact production bundle:

- `web_cloud_readonly/assets/index-*.css`
- `web_cloud_readonly/assets/index-*.js`
- `web_cloud_readonly/trustnode_logo.png`

Inline CSS + JS + logo into each single file.

## Functional parity

All 4 variants must render and behave like edge frontend for:

- Dashboard
- Power Overview
- Historian
- Reporting

Including:

- add/edit/delete dashboard items
- chart mode toggles (line/bar/kpi as available)
- realtime updates
- historian filters and exports
- reporting filters/chart/table/export flows

## Data strategy by variant

- API variants: full `/api/*` + `/ws/*` transport through cloud app API.
- Direct DB variants: only data-heavy endpoints (`live`, `historian`, `logs`, power series) from DB direct path; all config/auth/control endpoints fallback to app API.

## Runtime configuration

- API base override: `?api_base=https://your-domain`
- HTML DB variant:
  - `?db_url=https://<supabase-ref>.supabase.co`
  - `?db_key=<anon-or-limited-key>`
- PHP DB variant DB credentials via env:
  - `TRUSTNODE_DB_DSN`
  - `TRUSTNODE_DB_USER`
  - `TRUSTNODE_DB_PASS`

## Validation checklist

- No external `/assets/*` dependency.
- No placeholder UI.
- Same look/feel as current edge build.
- Login + data pages work.
- Direct DB variants return valid rows for live/historian/reporting datasets.
- Websocket path still mapped for realtime UI triggers.

## Regeneration policy

Regenerate these files after every frontend build to keep exact UI parity.
