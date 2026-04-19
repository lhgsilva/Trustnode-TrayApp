# Client Pages Diagnostics Report

Generated: 2026-04-16T18:50:06.881705

## URL Performance

### https://trustnode.lsapps.app/client/client_test.html
- Mode: `Unknown`
- Title: `Trustnode Edge - Single File Client (Cloud API HTML)`
- Content-Type: `text/html`
- Success: `8/8`
- Failures: `0`
- Status codes: `{'200': 8}`
- Latency ms: min `1433.84`, p50 `1969.15`, p95 `9566.81`, max `9566.81`, avg `3043.31`
- Avg response size bytes: `2798009.0`

### https://trustnode.lsapps.app/client/client_test.php
- Mode: `PHP API proxy`
- Title: `Trustnode Edge - Single File Client (Cloud API PHP Proxy)`
- Content-Type: `text/html; charset=UTF-8`
- Success: `8/8`
- Failures: `0`
- Status codes: `{'200': 8}`
- Latency ms: min `915.75`, p50 `1012.61`, p95 `1447.53`, max `1447.53`, avg `1075.48`
- Avg response size bytes: `2798017.0`

### https://trustnode.lsapps.app/client/client_test_db_rest.html
- Mode: `Browser direct DB REST + API fallback`
- Title: `Trustnode Edge - Single File Client (Direct Cloud DB HTML)`
- Content-Type: `text/html`
- Success: `8/8`
- Failures: `0`
- Status codes: `{'200': 8}`
- Latency ms: min `818.98`, p50 `929.37`, p95 `966.87`, max `966.87`, avg `907.66`
- Avg response size bytes: `2803947.0`

### https://trustnode.lsapps.app/client/client_test_db_php.html
- Mode: `Browser direct DB REST + API fallback`
- Title: `Trustnode Edge - Single File Client (Direct Cloud DB HTML)`
- Content-Type: `text/html`
- Success: `8/8`
- Failures: `0`
- Status codes: `{'200': 8}`
- Latency ms: min `727.41`, p50 `938.91`, p95 `974.62`, max `974.62`, avg `908.95`
- Avg response size bytes: `2803947.0`

### https://trustnode.lsapps.app/client/client_test_db.php
- Mode: `PHP direct DB + API proxy`
- Title: `Trustnode Edge - Single File Client (Direct Cloud DB PHP)`
- Content-Type: `text/html; charset=UTF-8`
- Success: `8/8`
- Failures: `0`
- Status codes: `{'200': 8}`
- Latency ms: min `710.66`, p50 `780.92`, p95 `975.06`, max `975.06`, avg `821.5`
- Avg response size bytes: `2799452.0`

## Edge/Cloud Sync Diagnostics

- Local login ok: `True`
- Cloud login ok: `True`
- Local live latest: `ts=2026-04-16 17:47:40.617 gateway=gw-primary tag=SimDINT[4] value=1009.0`
- Cloud live latest: `ts=2026-04-16 17:47:48.365000+00:00 gateway=gw-primary tag=SimDINT[3] value=130.0`
- Local historian latest: `ts=2026-04-16 17:47:41.326 gateway=gw-primary tag=SimDINT[4] value=1011.0`
- Cloud historian latest: `ts=2026-04-16 17:47:48.365000+00:00 gateway=gw-primary tag=SimDINT[4] value=1008.0`
- Local outbox pending/failed/sent: `0/0/17980`
- Local data_sync last_error: ``
- Local backlog hist/logs: `0/0`
- Cloud outbox pending/failed/sent: `0/0/16031`
- Cloud data_sync last_error: ``
- Live lag local-cloud seconds (local_ts - cloud_ts): `{'min': -9.012, 'p50': 2.001, 'p95': 8.069, 'max': 8.069, 'avg': 1.28}`

## Stream Consistency (PLC + Meter Candidates)

- Candidate streams tested: `4`
### power_meter_01 :: energy_delivered_total_wh
- Local rows: `180` | Cloud rows: `180`
- Latest lag seconds (local-cloud): `2.994`
- Median interval seconds: local `1.002` | cloud `1.002`
- Value consistency: `{'overlap_rows': 173, 'equal_rows': 173, 'mismatch_rows': 0, 'equal_ratio': 1.0}`

### gw-primary :: SimDINT[4]
- Local rows: `180` | Cloud rows: `180`
- Latest lag seconds (local-cloud): `10.851`
- Median interval seconds: local `1.003` | cloud `1.003`
- Value consistency: `{'overlap_rows': 171, 'equal_rows': 171, 'mismatch_rows': 0, 'equal_ratio': 1.0}`

### power_meter_01 :: energy_consumed_total_wh
- Local rows: `180` | Cloud rows: `180`
- Latest lag seconds (local-cloud): `17.0`
- Median interval seconds: local `1.002` | cloud `1.002`
- Value consistency: `{'overlap_rows': 159, 'equal_rows': 159, 'mismatch_rows': 0, 'equal_ratio': 1.0}`

### power_meter_01 :: energy_total_wh
- Local rows: `180` | Cloud rows: `180`
- Latest lag seconds (local-cloud): `5.001`
- Median interval seconds: local `1.002` | cloud `1.002`
- Value consistency: `{'overlap_rows': 172, 'equal_rows': 172, 'mismatch_rows': 0, 'equal_ratio': 1.0}`


## Runtime Network Profile (Playwright)

### https://trustnode.lsapps.app/client/client_test.html
- Login attempted: `True` | success: `True`
- Requests captured: `24` | failures: `4`
- Approx response bytes: `2810161`
- WebSocket connections: `1`
- Top paths: `[{'path': '/api/app-store/bootstrap', 'count': 2}, {'path': '/api/v1/history', 'count': 2}, {'path': '/api/app-store/logs', 'count': 2}, {'path': '/api/app-store/historian', 'count': 2}, {'path': '/client/client_test.html', 'count': 1}, {'path': '/api/plc/gateways/status', 'count': 1}, {'path': '/api/app-store/tenant/context', 'count': 1}, {'path': '/api/ui-source/config', 'count': 1}, {'path': '/api/auth/me', 'count': 1}, {'path': '/api/health', 'count': 1}, {'path': '/api/app-store/retention/policy', 'count': 1}, {'path': '/api/app-store/retention/runs', 'count': 1}]`

### https://trustnode.lsapps.app/client/client_test.php
- Login attempted: `True` | success: `False`
- Requests captured: `17` | failures: `0`
- Approx response bytes: `2806435`
- WebSocket connections: `0`
- Top paths: `[{'path': '/api/app-store/bootstrap', 'count': 4}, {'path': '/client/client_test.php', 'count': 1}, {'path': '/api/plc/gateways/status', 'count': 1}, {'path': '/api/ui-source/config', 'count': 1}, {'path': '/api/app-store/tenant/context', 'count': 1}, {'path': '/api/auth/me', 'count': 1}, {'path': '/api/health', 'count': 1}, {'path': '/api/v1/history', 'count': 1}, {'path': '/api/app-store/logs', 'count': 1}, {'path': '/api/app-store/retention/policy', 'count': 1}, {'path': '/api/app-store/retention/runs', 'count': 1}, {'path': '/api/app-store/backups', 'count': 1}]`

### https://trustnode.lsapps.app/client/client_test_db_rest.html
- Login attempted: `True` | success: `True`
- Requests captured: `21` | failures: `2`
- Approx response bytes: `2808344`
- WebSocket connections: `1`
- Top paths: `[{'path': '/api/app-store/bootstrap', 'count': 2}, {'path': '/api/v1/history', 'count': 2}, {'path': '/api/v1/latest', 'count': 2}, {'path': '/client/client_test_db_rest.html', 'count': 1}, {'path': '/api/plc/gateways/status', 'count': 1}, {'path': '/api/app-store/tenant/context', 'count': 1}, {'path': '/api/ui-source/config', 'count': 1}, {'path': '/api/auth/me', 'count': 1}, {'path': '/api/health', 'count': 1}, {'path': '/api/app-store/retention/policy', 'count': 1}, {'path': '/api/app-store/retention/runs', 'count': 1}, {'path': '/api/app-store/backups', 'count': 1}]`

### https://trustnode.lsapps.app/client/client_test_db_php.html
- Login attempted: `True` | success: `True`
- Requests captured: `29` | failures: `2`
- Approx response bytes: `2814148`
- WebSocket connections: `1`
- Top paths: `[{'path': '/api/app-store/bootstrap', 'count': 3}, {'path': '/api/v1/history', 'count': 3}, {'path': '/api/v1/edge/diagnostics', 'count': 3}, {'path': '/api/v1/latest', 'count': 3}, {'path': '/api/auth/me', 'count': 2}, {'path': '/api/app-store/inspector', 'count': 2}, {'path': '/client/client_test_db_php.html', 'count': 1}, {'path': '/api/plc/gateways/status', 'count': 1}, {'path': '/api/ui-source/config', 'count': 1}, {'path': '/api/app-store/tenant/context', 'count': 1}, {'path': '/api/app-store/retention/policy', 'count': 1}, {'path': '/api/health', 'count': 1}]`

### https://trustnode.lsapps.app/client/client_test_db.php
- Login attempted: `True` | success: `True`
- Requests captured: `22` | failures: `5`
- Approx response bytes: `2803896`
- WebSocket connections: `1`
- Top paths: `[{'path': '/api/app-store/bootstrap', 'count': 2}, {'path': '/api/v1/history', 'count': 2}, {'path': '/api/app-store/logs', 'count': 2}, {'path': '/api/app-store/historian', 'count': 2}, {'path': '/client/client_test_db.php', 'count': 1}, {'path': '/api/plc/gateways/status', 'count': 1}, {'path': '/api/ui-source/config', 'count': 1}, {'path': '/api/app-store/tenant/context', 'count': 1}, {'path': '/api/auth/me', 'count': 1}, {'path': '/api/health', 'count': 1}, {'path': '/api/app-store/retention/policy', 'count': 1}, {'path': '/api/app-store/retention/runs', 'count': 1}]`

## Recommendation

- Ranking (lower fail/p95/bytes is better for scale):
- 1. https://trustnode.lsapps.app/client/client_test_db_rest.html | mode=Browser direct DB REST + API fallback | fail=0/8 | p95=966.87 ms | avg_bytes=2803947.0
- 2. https://trustnode.lsapps.app/client/client_test_db_php.html | mode=Browser direct DB REST + API fallback | fail=0/8 | p95=974.62 ms | avg_bytes=2803947.0
- 3. https://trustnode.lsapps.app/client/client_test_db.php | mode=PHP direct DB + API proxy | fail=0/8 | p95=975.06 ms | avg_bytes=2799452.0
- 4. https://trustnode.lsapps.app/client/client_test.php | mode=PHP API proxy | fail=0/8 | p95=1447.53 ms | avg_bytes=2798017.0
- 5. https://trustnode.lsapps.app/client/client_test.html | mode=Unknown | fail=0/8 | p95=9566.81 ms | avg_bytes=2798009.0

## Notes

- `client_test_db_rest.html` and `client_test_db_php.html` are browser direct-DB variants; they require DB key in browser and increase exposure risk.
- `client_test_db.php` keeps DB credentials server-side and is safer than browser direct-DB while still reducing backend API roundtrips.
- For production multi-tenant scale, prefer API or PHP server-proxy variants with WebSocket + bounded polling.