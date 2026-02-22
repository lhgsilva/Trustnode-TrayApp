param(
    [string]$CloudApiUrl,
    [string]$BasePath
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

$env:VITE_TRUSTNODE_READONLY = "true"
$env:VITE_TRUSTNODE_FORCE_CLOUD_URL = $CloudApiUrl
& $npmCmd run build:cloudro -- --base $BasePath
if ($LASTEXITCODE -ne 0) {
    Pop-Location
    throw "Cloud read-only build failed."
}
Pop-Location

if (Test-Path $outputRoot) {
    Remove-Item -Recurse -Force $outputRoot
}
New-Item -ItemType Directory -Path $outputRoot | Out-Null
Copy-Item -Recurse -Force (Join-Path $frontendRoot "dist_cloud_readonly\*") $outputRoot

$readme = @"
Trustnode Cloud Read-Only Web Build

This folder contains a static frontend bundle configured as:
- Forced cloud backend URL: $CloudApiUrl
- Read-only mode: enabled
- Base path: $BasePath

Deploy this folder content under your web server subfolder.
"@
Set-Content -Path (Join-Path $outputRoot "README.txt") -Value $readme -Encoding UTF8

Write-Host "Cloud read-only web bundle created at: $outputRoot"
