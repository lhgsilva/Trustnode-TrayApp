<#
.SYNOPSIS
  One-command LOCAL DEV launcher for the TrustNode edge app in the browser.

  Starts the FastAPI backend (:8000) and the Vite dev server (:5173) HIDDEN
  (no console windows pop up) with the correct env so the browser app at
  http://127.0.0.1:5173/ behaves like the packaged edge app:
    - backend points at the REAL app-store DB (your gateways/dashboards/batches)
    - tenant forced so the config scope resolves to your data
    - dev license bypass (frontend + backend) so the cloud license handshake
      doesn't block the UI or 404 the AI module

  Edit frontend files -> browser hot-reloads (no restart). Edit backend files
  -> re-run this script. Logs stream to scripts\logs\ (tail them if needed).

.NOTES
  DEV ONLY. Bypass flags are never set in production builds. Quit the packaged
  edge app first — it uses the same port 8000 and DBs.
#>
param(
  [string]$AppStoreDb = "$env:USERPROFILE\.trustnode_edge\data\trustnode_app_store.db",
  [string]$TenantId   = "tenant-cust-e5916328",
  [int]$BackendPort   = 8000,
  [int]$FrontendPort  = 5173
)

$ErrorActionPreference = "Stop"
$root     = Split-Path -Parent $PSScriptRoot          # Trustnode_edge_app
$backend  = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"
$py       = Join-Path $backend ".venv\Scripts\python.exe"
$logDir   = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$beLog    = Join-Path $logDir "backend.log"
$feLog    = Join-Path $logDir "frontend.log"

if (-not (Test-Path $py)) { throw "Python venv not found: $py" }

# resolve node + the vite bin so we DON'T depend on npm.cmd through Start-Process
$node = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $node) { throw "node not found on PATH" }
$viteBin = Join-Path $frontend "node_modules\vite\bin\vite.js"
if (-not (Test-Path $viteBin)) { throw "vite not installed: $viteBin (run npm install in frontend)" }

Write-Host "== TrustNode DEV launcher ==" -ForegroundColor Cyan
Write-Host "  app DB : $AppStoreDb"
Write-Host "  tenant : $TenantId"
Write-Host "  logs   : $logDir"

# --- stop any prior dev processes so restarts are clean ------------------
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -match 'uvicorn|app\.main' } |
  ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force } catch {} }
Get-CimInstance Win32_Process -Filter "Name='node.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -match 'vite' } |
  ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force } catch {} }
Start-Sleep -Milliseconds 800

# --- ensure frontend has the dev bypass env ------------------------------
$envLocal = Join-Path $frontend ".env.local"
if (-not (Test-Path $envLocal) -or -not (Select-String -Path $envLocal -Pattern "VITE_TRUSTNODE_DEV_LICENSE_BYPASS" -Quiet)) {
  "VITE_TRUSTNODE_DEV_LICENSE_BYPASS=true" | Out-File -FilePath $envLocal -Encoding utf8 -Append
}

# helper: launch a process fully HIDDEN, stdout+stderr -> a log file, return the Process
function Start-Hidden([string]$file, [string[]]$args, [string]$workdir, [string]$log, [hashtable]$env) {
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $file
  foreach ($a in $args) { [void]$psi.ArgumentList.Add($a) }
  $psi.WorkingDirectory = $workdir
  $psi.UseShellExecute = $false          # required to set env + redirect + hide
  $psi.CreateNoWindow  = $true           # NO console window
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError  = $true
  if ($env) { foreach ($k in $env.Keys) { $psi.EnvironmentVariables[$k] = [string]$env[$k] } }
  $p = New-Object System.Diagnostics.Process
  $p.StartInfo = $psi
  # async pump both streams to the log file
  $sw = [System.IO.StreamWriter]::new($log, $false)
  $sw.AutoFlush = $true
  $onData = { param($s,$e) if ($e.Data -ne $null) { $sw.WriteLine($e.Data) } }.GetNewClosure()
  Register-ObjectEvent -InputObject $p -EventName OutputDataReceived -Action $onData | Out-Null
  Register-ObjectEvent -InputObject $p -EventName ErrorDataReceived  -Action $onData | Out-Null
  [void]$p.Start()
  $p.BeginOutputReadLine(); $p.BeginErrorReadLine()
  return $p
}

# --- start backend (hidden) ---------------------------------------------
Write-Host "`nStarting backend (hidden)..." -ForegroundColor Yellow
$beEnv = @{
  TRUSTNODE_APP_STORE_PATH    = $AppStoreDb
  TRUSTNODE_TENANT_ID         = $TenantId
  TRUSTNODE_DEV_LICENSE_BYPASS= "1"
  TRUSTNODE_INTELLIGENCE      = "on"
}
$beProc = Start-Hidden $py @("-m","uvicorn","app.main:app","--host","127.0.0.1","--port","$BackendPort","--no-access-log") $backend $beLog $beEnv

$ok = $false
for ($i = 0; $i -lt 40; $i++) {
  try { if ((Invoke-WebRequest "http://127.0.0.1:$BackendPort/api/health" -TimeoutSec 2 -UseBasicParsing).StatusCode -eq 200) { $ok = $true; break } } catch {}
  Start-Sleep -Seconds 1
}
if ($ok) { Write-Host "  backend UP  (pid $($beProc.Id))  log: $beLog" -ForegroundColor Green }
else     { Write-Warning "  backend not healthy in 40s — see $beLog" }

# --- start frontend / Vite (hidden) -------------------------------------
Write-Host "Starting frontend (hidden)..." -ForegroundColor Yellow
$feProc = Start-Hidden $node @($viteBin) $frontend $feLog $null

$okv = $false
for ($i = 0; $i -lt 30; $i++) {
  try { $c = (Invoke-WebRequest "http://127.0.0.1:$FrontendPort/" -TimeoutSec 2 -UseBasicParsing).StatusCode; if ($c -eq 200 -or $c -eq 304) { $okv = $true; break } } catch {}
  Start-Sleep -Seconds 1
}
if ($okv) { Write-Host "  frontend UP (pid $($feProc.Id))  log: $feLog" -ForegroundColor Green }
else      { Write-Warning "  frontend not responding in 30s — see $feLog" }

Write-Host "`n==============================================" -ForegroundColor Cyan
Write-Host "  Open:  http://127.0.0.1:$FrontendPort/" -ForegroundColor Green
Write-Host "  Login: admin-mari  (your edge-app password)"
Write-Host "  Both run HIDDEN (no popup). Logs: $logDir"
Write-Host "  Stop:  powershell -File scripts\dev-stop.ps1"
Write-Host "==============================================" -ForegroundColor Cyan
