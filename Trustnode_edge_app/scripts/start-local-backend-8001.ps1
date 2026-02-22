$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repoRoot "backend"

if (-not (Test-Path $backendDir)) {
    throw "Backend directory not found: $backendDir"
}

# Stop any process already listening on 127.0.0.1:8001.
$lines = netstat -ano | Select-String ":8001" | Select-String "LISTENING"
foreach ($line in $lines) {
    $parts = ($line -replace "\s+", " ").Trim().Split(" ")
    if ($parts.Length -ge 5) {
        $procId = [int]$parts[4]
        if ($procId -gt 0) {
            try { Stop-Process -Id $procId -Force -ErrorAction Stop } catch {}
        }
    }
}

$env:TRUSTNODE_HOST = "127.0.0.1"
$env:TRUSTNODE_PORT = "8001"

Write-Output "Starting updated backend on http://127.0.0.1:8001 from $backendDir"
Start-Process -FilePath "python" -ArgumentList "-m app" -WorkingDirectory $backendDir
Start-Sleep -Seconds 2

try {
    $health = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8001/api/health" -TimeoutSec 3
    Write-Output "Health: $($health.StatusCode)"
} catch {
    Write-Output "Health check failed: $($_.Exception.Message)"
}
