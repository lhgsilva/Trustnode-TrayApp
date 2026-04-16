param(
  [string]$User = "admin",
  [string]$Pass = "admin",
  [int]$Runs = 20,
  [int]$CaptureSeconds = 25
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..\..")
Set-Location $root

$env:TRUSTNODE_DIAG_USER = $User
$env:TRUSTNODE_DIAG_PASS = $Pass
$env:TRUSTNODE_CAPTURE_SECONDS = "$CaptureSeconds"

Write-Host "Running Playwright runtime profile..."
node .\Trustnode_edge_app\client_page_tests\diagnostics\network_profile_playwright.mjs

Write-Host "Running API/data diagnostics..."
python .\Trustnode_edge_app\client_page_tests\diagnostics\run_client_diagnostics.py --runs $Runs --username $User --password $Pass

Write-Host "Done. Report: .\Trustnode_edge_app\client_page_tests\diagnostics\output\CLIENT_DIAGNOSTICS_REPORT.md"
