param(
    [string]$CloudApiUrl,
    [string]$BasePath,
    [switch]$ReadOnly
)

$ErrorActionPreference = "Stop"

if (-not $PSBoundParameters.ContainsKey("BasePath") -or -not $BasePath) {
    $BasePath = "/"
}

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$frontendRoot = Join-Path $projectRoot "frontend"
$outputRoot = Join-Path $projectRoot "web_cloud_readonly"

if (-not $CloudApiUrl) {
    throw "CloudApiUrl is required. Example: -CloudApiUrl 'https://api.yourdomain.com'"
}

if (-not $BasePath.StartsWith("/")) {
    $BasePath = "/$BasePath"
}
if (-not $BasePath.EndsWith("/")) {
    $BasePath = "$BasePath/"
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
& $npmCmd install
if ($LASTEXITCODE -ne 0) {
    Pop-Location
    throw "Frontend dependency install failed."
}

$env:VITE_TRUSTNODE_READONLY = $(if ($ReadOnly) { "true" } else { "false" })
$env:VITE_TRUSTNODE_FORCE_CLOUD_URL = $CloudApiUrl
& $npmCmd run build -- --outDir dist_cloud_readonly --base $BasePath
if ($LASTEXITCODE -ne 0) {
    Pop-Location
    throw "Cloud web build failed."
}
Pop-Location

if (Test-Path $outputRoot) {
    Remove-Item -Recurse -Force $outputRoot
}
New-Item -ItemType Directory -Path $outputRoot | Out-Null
Copy-Item -Recurse -Force (Join-Path $frontendRoot "dist_cloud_readonly\*") $outputRoot

# Create dedicated standalone portal entry path, outside the main app route flow.
$rootIndexPath = Join-Path $outputRoot "index.html"
$portalDir = Join-Path $outputRoot "developer-portal"
if (-not (Test-Path $portalDir)) {
    New-Item -ItemType Directory -Path $portalDir | Out-Null
}
if (Test-Path $rootIndexPath) {
    $idx = Get-Content -Path $rootIndexPath -Raw
    $idx = $idx -replace "<title>Trustnode Edge</title>", "<title>Trustnode Developer Portal</title>"
    Set-Content -Path (Join-Path $portalDir "index.html") -Value $idx -Encoding UTF8
}

$readme = @"
Trustnode Cloud Web Build

This folder contains a static frontend bundle configured as:
- Forced cloud backend URL: $CloudApiUrl
- Read-only mode: $($ReadOnly.IsPresent)
- Base path: $BasePath

Deploy this folder content under your web server subfolder.
"@
Set-Content -Path (Join-Path $outputRoot "README.txt") -Value $readme -Encoding UTF8

Write-Host "Cloud web bundle created at: $outputRoot"
