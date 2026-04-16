# Client Diagnostics Scripts

This folder contains repeatable diagnostics for the 5 client test URLs.

## Scripts

- `run_all_diagnostics.ps1`
  - Runs browser runtime profiling (Playwright) + API/data diagnostics.
- `network_profile_playwright.mjs`
  - Opens each client URL, attempts login, captures request counts/bytes/websockets.
- `run_client_diagnostics.py`
  - Benchmarks page latency/size, checks local/cloud sync status, builds markdown report.

## Quick Run

```powershell
powershell -ExecutionPolicy Bypass -File .\Trustnode_edge_app\client_page_tests\diagnostics\run_all_diagnostics.ps1 -User admin -Pass admin -Runs 20 -CaptureSeconds 20
```

## Outputs

Generated in:

- `Trustnode_edge_app/client_page_tests/diagnostics/output/CLIENT_DIAGNOSTICS_REPORT.md`
- `Trustnode_edge_app/client_page_tests/diagnostics/output/client_diagnostics_raw.json`
- `Trustnode_edge_app/client_page_tests/diagnostics/output/playwright_profile.json`

## Notes

- Browser-direct DB pages require DB URL/key query or localStorage values.
- Default creds used unless overridden by env vars:
  - `TRUSTNODE_DIAG_USER`
  - `TRUSTNODE_DIAG_PASS`
