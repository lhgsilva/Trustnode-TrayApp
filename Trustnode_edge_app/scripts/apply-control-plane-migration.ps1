param(
  [Parameter(Mandatory = $true)]
  [string]$DatabaseUrl,
  [string]$MigrationPath = "Trustnode_edge_app/backend/sql/migrations/2026-04-22_control_plane_core.sql"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $MigrationPath)) {
  throw "Migration file not found: $MigrationPath"
}

$psql = Get-Command psql -ErrorAction SilentlyContinue
if (-not $psql) {
  throw "psql not found in PATH. Install PostgreSQL client tools first."
}

Write-Host "Applying migration: $MigrationPath"
& psql "$DatabaseUrl" -v ON_ERROR_STOP=1 -f "$MigrationPath"
if ($LASTEXITCODE -ne 0) {
  throw "Migration failed with exit code $LASTEXITCODE"
}

Write-Host "Control-plane migration applied successfully."
