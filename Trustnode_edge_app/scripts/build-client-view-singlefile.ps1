param(
    [string]$CloudApiUrl,
    [string]$OutFile
)

$ErrorActionPreference = "Stop"

if (-not $CloudApiUrl) {
    throw "CloudApiUrl is required. Example: -CloudApiUrl 'https://trustnode.lsapps.app'"
}

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$frontendRoot = Join-Path $projectRoot "frontend"
$clientPageTestsDir = Join-Path $projectRoot "client_page_tests"

if (-not $OutFile) {
    $OutFile = Join-Path $clientPageTestsDir "client_view.html"
}

$npmCmd = $null
$nodeDir = "C:\Program Files\nodejs"
$npmInPath = Get-Command npm -ErrorAction SilentlyContinue
if ($npmInPath) {
    $npmCmd = "npm"
} else {
    $fallbackNpm = Join-Path $nodeDir "npm.cmd"
    if (Test-Path $fallbackNpm) {
        $npmCmd = $fallbackNpm
        if ($env:Path -notlike "*$nodeDir*") {
            $env:Path = "$nodeDir;$env:Path"
        }
    } else {
        throw "npm not found in PATH and fallback '$fallbackNpm' not found."
    }
}

Push-Location $frontendRoot
try {
    & $npmCmd install
    if ($LASTEXITCODE -ne 0) { throw "Frontend dependency install failed." }

    $env:VITE_TRUSTNODE_READONLY = "true"
    $env:VITE_TRUSTNODE_CLIENT_VIEW = "true"
    $env:VITE_TRUSTNODE_FORCE_CLOUD_URL = $CloudApiUrl

    & $npmCmd run build:clientview
    if ($LASTEXITCODE -ne 0) { throw "Single-file client view build failed." }
} finally {
    Pop-Location
}

$builtIndex = Join-Path $frontendRoot "dist_client_view\index.html"
if (-not (Test-Path $builtIndex)) {
    throw "Expected single-file output at $builtIndex but it was not produced."
}

$outDir = Split-Path -Parent $OutFile
if ($outDir -and -not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Path $outDir | Out-Null
}

Copy-Item -Force $builtIndex $OutFile

$sizeKb = [math]::Round((Get-Item $OutFile).Length / 1024, 1)
Write-Host "Single-file client view written to: $OutFile ($sizeKb KB)"
