$ErrorActionPreference = "Stop"

Write-Host "Stopping running Trustnode/Electron processes..." -ForegroundColor Cyan
$names = @("Trustnode", "trustnode-service", "backend", "electron")
foreach ($n in $names) {
  try {
    Get-Process -Name $n -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
  } catch {}
}

Start-Sleep -Seconds 1

$exe = Join-Path $PSScriptRoot "..\desktop\dist\Trustnode 0.1.0.exe"
$exe = [System.IO.Path]::GetFullPath($exe)
if (-not (Test-Path $exe)) {
  throw "Executable not found: $exe"
}

Write-Host "Starting: $exe" -ForegroundColor Green
Start-Process -FilePath $exe
Write-Host "Done." -ForegroundColor Green

