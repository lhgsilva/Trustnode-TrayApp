param(
    [string]$CloudApiUrl,
    [string]$WebBasePath
)

$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$desktopRoot = Join-Path $projectRoot "desktop"

Write-Host "Generating installer icon..."
powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build-icon.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "Icon generation step failed (exit code: $LASTEXITCODE)."
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

Write-Host "Installing desktop dependencies..."
Push-Location $desktopRoot
& $npmCmd install
if ($LASTEXITCODE -ne 0) {
    Pop-Location
    throw "Desktop dependency install failed (exit code: $LASTEXITCODE)."
}

Write-Host "Preparing unsigned build environment..."
$env:CSC_IDENTITY_AUTO_DISCOVERY = "false"
$env:WIN_CSC_LINK = ""
$env:WIN_CSC_KEY_PASSWORD = ""

$winCodeSignCache = Join-Path $env:LOCALAPPDATA "electron-builder\Cache\winCodeSign"
if (Test-Path $winCodeSignCache) {
    Write-Host "Cleaning stale winCodeSign cache..."
    Remove-Item -Recurse -Force $winCodeSignCache
}

Write-Host "Building full Windows installer (frontend + backend + desktop)..."
& $npmCmd run dist
if ($LASTEXITCODE -ne 0) {
    Pop-Location
    throw "Release build failed (exit code: $LASTEXITCODE)."
}
Pop-Location

Write-Host "Done. Outputs are in: $desktopRoot\\dist"
Write-Host "Expected files:"
Write-Host "- NSIS installer: Trustnode Edge Setup *.exe"
Write-Host "- Portable app: Trustnode Edge *.exe"

if (-not $PSBoundParameters.ContainsKey("CloudApiUrl") -or -not $CloudApiUrl) {
    $CloudApiUrl = $env:TRUSTNODE_CLOUD_API_URL
}
if (-not $PSBoundParameters.ContainsKey("WebBasePath") -or -not $WebBasePath) {
    $WebBasePath = $env:TRUSTNODE_WEB_BASE_PATH
}
if (-not $WebBasePath) {
    $WebBasePath = "/"
}
$webCloudUrl = $CloudApiUrl
if (-not $webCloudUrl) {
    $webCloudUrl = "https://api.example.com"
}

Write-Host "Building cloud web bundle (admin-capable by default)..."
powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build-web-cloud-readonly.ps1") `
    -CloudApiUrl $webCloudUrl `
    -BasePath $WebBasePath
if ($LASTEXITCODE -ne 0) {
    throw "Cloud web bundle build failed (exit code: $LASTEXITCODE)."
}
if (-not $CloudApiUrl) {
    Write-Host "WARNING: CloudApiUrl not provided. Web build used placeholder https://api.example.com"
    Write-Host "Set -CloudApiUrl or env TRUSTNODE_CLOUD_API_URL for production-ready web output."
}
