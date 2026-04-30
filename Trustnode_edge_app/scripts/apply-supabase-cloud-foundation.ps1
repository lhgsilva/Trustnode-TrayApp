param(
  [Parameter(Mandatory = $true)]
  [string]$DatabaseUrl
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$migrations = @(
  (Join-Path $root "backend\sql\migrations\2026-04-11_telemetry_v1_core.sql"),
  (Join-Path $root "backend\sql\migrations\2026-04-22_control_plane_core.sql"),
  (Join-Path $root "backend\sql\migrations\2026-04-30_supabase_control_plane_hardening.sql")
)

$psql = Get-Command psql -ErrorAction SilentlyContinue
if (-not $psql) {
  throw "psql not found in PATH. Install PostgreSQL client tools first."
}

foreach ($migration in $migrations) {
  if (-not (Test-Path $migration)) {
    throw "Migration file not found: $migration"
  }
  Write-Host "Applying: $migration" -ForegroundColor Cyan
  & psql "$DatabaseUrl" -v ON_ERROR_STOP=1 -f "$migration"
  if ($LASTEXITCODE -ne 0) {
    throw "Migration failed: $migration (exit $LASTEXITCODE)"
  }
}

Write-Host "Supabase cloud foundation is ready." -ForegroundColor Green
