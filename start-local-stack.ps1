param(
  [int]$BackendPort = 8000,
  [int]$FrontendPort = 5173,
  [int]$HtmlPort = 8090,
  [int]$PhpPort = 8091
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Join-Path $repoRoot "Trustnode_edge_app\backend"
$frontendDir = Join-Path $repoRoot "Trustnode_edge_app\frontend"
$clientTestsDir = Join-Path $repoRoot "Trustnode_edge_app\client_page_tests"

$backendPython = Join-Path $backendDir ".venv\Scripts\python.exe"
if (-not (Test-Path $backendPython)) {
  throw "Backend venv python not found at: $backendPython"
}

if (-not (Test-Path (Join-Path $frontendDir "node_modules"))) {
  Write-Host "frontend/node_modules missing. Running npm install..." -ForegroundColor Yellow
  Push-Location $frontendDir
  npm install
  Pop-Location
}

Write-Host ""
Write-Host "Starting Trustnode local stack..." -ForegroundColor Cyan
Write-Host "Backend:  http://127.0.0.1:$BackendPort" -ForegroundColor Green
Write-Host "Frontend: http://127.0.0.1:$FrontendPort" -ForegroundColor Green
Write-Host "HTML tests: http://127.0.0.1:$HtmlPort" -ForegroundColor Green
Write-Host "PHP tests:  http://127.0.0.1:$PhpPort" -ForegroundColor Green
Write-Host ""

Start-Process powershell -ArgumentList @(
  "-NoExit",
  "-Command",
  "Set-Location '$backendDir'; .\.venv\Scripts\activate; uvicorn app.main:app --host 127.0.0.1 --port $BackendPort"
) | Out-Null

Start-Process powershell -ArgumentList @(
  "-NoExit",
  "-Command",
  "Set-Location '$frontendDir'; npm run dev -- --host 127.0.0.1 --port $FrontendPort"
) | Out-Null

Start-Process powershell -ArgumentList @(
  "-NoExit",
  "-Command",
  "Set-Location '$clientTestsDir'; python -m http.server $HtmlPort"
) | Out-Null

Start-Process powershell -ArgumentList @(
  "-NoExit",
  "-Command",
  "Set-Location '$clientTestsDir'; php -S 127.0.0.1:$PhpPort"
) | Out-Null

Write-Host "All local services launched in new terminals." -ForegroundColor Cyan
Write-Host ""
Write-Host "Main URLs:" -ForegroundColor White
Write-Host "  Edge/Web UI:              http://127.0.0.1:$FrontendPort" -ForegroundColor White
Write-Host "  Developer portal:          http://127.0.0.1:$FrontendPort/portal" -ForegroundColor White
Write-Host "  Backend health:            http://127.0.0.1:$BackendPort/api/health" -ForegroundColor White
Write-Host "  HTML client test:          http://127.0.0.1:$HtmlPort/client_test.html" -ForegroundColor White
Write-Host "  HTML DB REST client test:  http://127.0.0.1:$HtmlPort/client_test_db_rest.html" -ForegroundColor White
Write-Host "  HTML DB PHP-style test:    http://127.0.0.1:$HtmlPort/client_test_db_php.html" -ForegroundColor White
Write-Host "  PHP client test:           http://127.0.0.1:$PhpPort/client_test.php" -ForegroundColor White
Write-Host "  PHP direct DB test:        http://127.0.0.1:$PhpPort/client_test_db.php" -ForegroundColor White
Write-Host ""
Write-Host "Tip: close each spawned PowerShell window to stop each service." -ForegroundColor Yellow
