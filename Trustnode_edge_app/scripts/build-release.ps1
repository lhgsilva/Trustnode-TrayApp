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

# --- Code signing -----------------------------------------------------------
# 2026-10-30, reported: Smart App Control blocks the portable and the setup,
# "same for the tnlink", saying the publisher cannot be verified.
#
# That is exactly what Windows does with an UNSIGNED executable, and no change
# to the code can alter it: the block is about who vouches for the binary, not
# what it contains. It needs a Windows code-signing certificate from a CA.
#
# So the build now signs whenever one is available and stays unsigned when it
# is not - no half state, and the log says which happened. Provide:
#
#   $env:TRUSTNODE_CSC_LINK      full path to the .pfx (or a base64 blob)
#   $env:TRUSTNODE_CSC_PASSWORD  its password
#
# electron-builder signs the app, the installer and the portable from those.
# The PyInstaller binaries (trustnode-service.exe, and tnlink.exe in the
# prototype) are NOT built by electron-builder, so they are signed below with
# signtool - an installer that is signed while the service inside it is not
# still trips SmartScreen the first time the service runs.
$signPfx = $env:TRUSTNODE_CSC_LINK
$signPwd = $env:TRUSTNODE_CSC_PASSWORD
$script:SigningEnabled = $false
if ($signPfx -and (Test-Path $signPfx)) {
    Write-Host "Code-signing certificate found - this build WILL be signed."
    $env:CSC_IDENTITY_AUTO_DISCOVERY = "true"
    $env:WIN_CSC_LINK = $signPfx
    $env:WIN_CSC_KEY_PASSWORD = $signPwd
    $script:SigningEnabled = $true
} else {
    Write-Host "No code-signing certificate (TRUSTNODE_CSC_LINK unset or missing)."
    Write-Host "  The build will be UNSIGNED. Windows Smart App Control and"
    Write-Host "  SmartScreen will warn that the publisher cannot be verified."
    $env:CSC_IDENTITY_AUTO_DISCOVERY = "false"
    $env:WIN_CSC_LINK = ""
    $env:WIN_CSC_KEY_PASSWORD = ""
}

$winCodeSignCache = Join-Path $env:LOCALAPPDATA "electron-builder\Cache\winCodeSign"
if ((-not $script:SigningEnabled) -and (Test-Path $winCodeSignCache)) {
    # Only for unsigned builds. Deleting this cache during a SIGNED build
    # removes the very tools electron-builder is about to use.
    Write-Host "Cleaning stale winCodeSign cache..."
    Remove-Item -Recurse -Force $winCodeSignCache
}

# The PyInstaller binaries are produced before electron-builder packages
# them, so they are signed HERE - after the backend build, before the app is
# assembled. A signed installer wrapped around an unsigned service is still a
# SmartScreen prompt the first time that service runs.
function Invoke-TrustNodeSign {
    param([string[]]$Paths)
    if (-not $script:SigningEnabled) { return }
    $sdkRoot = Join-Path ${env:ProgramFiles(x86)} (Join-Path "Windows Kits" (Join-Path "10" "bin"))
    $signtool = Get-ChildItem $sdkRoot -Recurse `
                  -Filter signtool.exe -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -match "x64" } |
                Sort-Object FullName -Descending | Select-Object -First 1
    if (-not $signtool) {
        Write-Host "  WARN: signtool.exe not found (Windows SDK). These stay unsigned:"
        $Paths | ForEach-Object { Write-Host "        $_" }
        return
    }
    foreach ($f in $Paths) {
        if (-not (Test-Path $f)) { continue }
        & $signtool.FullName sign /fd SHA256 /f $env:TRUSTNODE_CSC_LINK `
            /p $env:TRUSTNODE_CSC_PASSWORD /tr http://timestamp.digicert.com `
            /td SHA256 $f | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Host "  signed: $f" }
        else { throw "signtool failed for $f (exit $LASTEXITCODE) - do not ship a half-signed build." }
    }
}

Invoke-TrustNodeSign @(
    (Join-Path (Join-Path (Join-Path $projectRoot "backend") "dist") "trustnode-service" | Join-Path -ChildPath "trustnode-service.exe")
)

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
    # 2026-10-17: this used to fall back to "https://api.example.com", so a
    # build with no CloudApiUrl produced a cloud bundle pointing at a
    # placeholder host - and said so in a WARNING nobody reads at the end of a
    # fifteen-minute build. The real address is in
    # docs/CONTROL_PLANE_DEPLOY_CHECKLIST_2026-04-23.md; use it.
    $webCloudUrl = "https://trustnode.lsapps.app"
}

Write-Host "Building cloud web bundle (admin-capable by default)..."
powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build-web-cloud-readonly.ps1") `
    -CloudApiUrl $webCloudUrl `
    -BasePath $WebBasePath
if ($LASTEXITCODE -ne 0) {
    throw "Cloud web bundle build failed (exit code: $LASTEXITCODE)."
}
if (-not $CloudApiUrl) {
    Write-Host "NOTE: CloudApiUrl not provided. Web build used the documented default $webCloudUrl"
    Write-Host "Set -CloudApiUrl or env TRUSTNODE_CLOUD_API_URL to build for a different deployment."
}
