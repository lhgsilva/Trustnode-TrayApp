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
# Operator 2026-06-21: rewrite relative asset paths (./assets/...) to absolute
# (/assets/...). When the developer-portal stub is served at /developer-portal/
# the browser resolves ./assets/ against /developer-portal/, fetches a 404,
# and nginx's SPA fallback returns HTML — which the browser refuses to
# execute as a JS module (strict MIME type checking). Absolute paths skip
# that trap. This was the root cause of the Jun 21 production outage.
# The developer-portal stub is a transformed copy of the freshly-built root
# index.html (title + absolute asset paths). It MUST be generated into BOTH:
#   - web_cloud_readonly/   (the human-facing bundle dir), AND
#   - frontend/dist_cloud_readonly/  (what push_portal_to_vps.py actually uploads).
# Operator 2026-07-15: previously the stub was only written to web_cloud_readonly,
# so the push (DIST_DIR = dist_cloud_readonly) NEVER refreshed the dev-portal stub
# on the VPS — /developer-portal/ stayed pinned to an old asset hash and missed new
# features (e.g. the Infrastructure Endpoints menu). Generating it into the pushed
# dir keeps /developer-portal/ tracking the current bundle on every deploy.
$distRoot = Join-Path $frontendRoot "dist_cloud_readonly"
$stubTargets = @($outputRoot, $distRoot)
foreach ($tgt in $stubTargets) {
    $tgtRootIndex = Join-Path $tgt "index.html"
    $tgtPortalDir = Join-Path $tgt "developer-portal"
    if (-not (Test-Path $tgtPortalDir)) {
        New-Item -ItemType Directory -Path $tgtPortalDir | Out-Null
    }
    if (Test-Path $tgtRootIndex) {
        $idx = Get-Content -Path $tgtRootIndex -Raw
        $idx = $idx -replace "<title>Trustnode Edge</title>", "<title>Trustnode Developer Portal</title>"
        $idx = $idx -replace 'src="\./assets/', 'src="/assets/'
        $idx = $idx -replace 'href="\./assets/', 'href="/assets/'
        Set-Content -Path (Join-Path $tgtPortalDir "index.html") -Value $idx -Encoding UTF8
    }
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
