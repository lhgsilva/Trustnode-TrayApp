# TrustNode Intelligence — installer for an existing Edge install.
#
# Run from this folder (trustnode_intelligence/). Idempotent: re-running
# replaces the module's own files; modifications to host files (main.py,
# App.jsx, license_inspect.py) are only applied once.
#
# Usage:
#   .\INSTALL.ps1 -EdgeRoot D:\Trustnode\Trustnode-AB\Tray_app\Trustnode_edge_app

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [string]$EdgeRoot
)

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$ts = Get-Date -Format "yyyyMMdd-HHmmss"

function Backup-File($path) {
    if (Test-Path $path) {
        Copy-Item $path "$path.bak.$ts"
        Write-Host "  backed up → $path.bak.$ts"
    }
}

function Insert-If-Missing($path, $marker, $insertAfterRegex, $insertion) {
    if (-not (Test-Path $path)) { throw "Host file not found: $path" }
    $content = Get-Content $path -Raw
    if ($content -match [regex]::Escape($marker)) {
        Write-Host "  already wired: $marker"
        return
    }
    Backup-File $path
    $regex = [regex]$insertAfterRegex
    $match = $regex.Match($content)
    if (-not $match.Success) {
        throw "Could not find insertion anchor in $path (regex: $insertAfterRegex)"
    }
    $insertAt = $match.Index + $match.Length
    $newContent = $content.Substring(0, $insertAt) + "`n" + $insertion + "`n" + $content.Substring($insertAt)
    Set-Content -Path $path -Value $newContent -Encoding UTF8
    Write-Host "  injected at line " + (($content.Substring(0, $insertAt) -split "`n").Count)
}

Write-Host ""
Write-Host "TrustNode Intelligence — installing into $EdgeRoot"
Write-Host ""

# --- 1. Backend module files ------------------------------------------------
$dst = Join-Path $EdgeRoot "trustnode_intelligence"
Write-Host "1. Copy module folder to $dst"
if (Test-Path $dst) {
    Remove-Item -Recurse -Force $dst
}
Copy-Item -Recurse $here $dst -Force
Write-Host "  done"

# --- 2. Register router in main.py -----------------------------------------
$mainPy = Join-Path $EdgeRoot "backend\app\main.py"
Write-Host "2. Register router in $mainPy"
Insert-If-Missing -path $mainPy `
    -marker "trustnode_intelligence.backend.router" `
    -insertAfterRegex "^app\s*=\s*FastAPI\([^)]*\)" `
    -insertion @"

# TrustNode Intelligence module (optional bolt-on)
try:
    from trustnode_intelligence.backend.router import router as _intelligence_router
    app.include_router(_intelligence_router)
except Exception as _exc:
    import logging as _log
    _log.getLogger('trustnode.intelligence').warning('module not loaded: %s', _exc)
"@

# --- 3. Register module key in license_inspect.py --------------------------
$licInspect = Join-Path $EdgeRoot "backend\app\services\license_inspect.py"
if (Test-Path $licInspect) {
    Write-Host "3. Register module key in $licInspect"
    $content = Get-Content $licInspect -Raw
    if ($content -match "trustnode_intelligence") {
        Write-Host "  already registered"
    } else {
        Backup-File $licInspect
        # No-op: most license_inspect modules read modules from license JSON
        # dynamically. The key just needs to be a known string. Documented
        # for awareness — no source-code change required.
        Write-Host "  no source change required (modules read dynamically from license)"
    }
}

# --- 4. Frontend menu mount in App.jsx -------------------------------------
$appJsx = Join-Path $EdgeRoot "frontend\src\App.jsx"
Write-Host "4. Wire up frontend in $appJsx"
Insert-If-Missing -path $appJsx `
    -marker "IntelligenceMenu" `
    -insertAfterRegex "import\s+\{[^}]+\}\s+from\s+['\""]react['\""];" `
    -insertion @"
// TrustNode Intelligence module
import IntelligenceMenu from "../../trustnode_intelligence/frontend/IntelligenceMenu.jsx";
import IntelligenceChatPage from "../../trustnode_intelligence/frontend/IntelligenceChatPage.jsx";
import IntelligenceInsightsPage from "../../trustnode_intelligence/frontend/IntelligenceInsightsPage.jsx";
"@

Write-Host ""
Write-Host "Backend + imports wired. Manual steps remaining:"
Write-Host ""
Write-Host "  a) In App.jsx, render <IntelligenceMenu activePage={activePage} onNavigate={setActivePage} />"
Write-Host "     just ABOVE the navbar divider above the user-login section."
Write-Host ""
Write-Host "  b) In App.jsx, add page routes for 'intelligence_chat' and 'intelligence_insights':"
Write-Host "       {activePage === 'intelligence_chat'     && <IntelligenceChatPage />}"
Write-Host "       {activePage === 'intelligence_insights' && <IntelligenceInsightsPage />}"
Write-Host ""
Write-Host "  c) Rebuild the Edge installer:"
Write-Host "       $EdgeRoot\scripts\build-release.ps1"
Write-Host ""
Write-Host "  d) On the developer portal, set the module config for the customer license."
Write-Host "     See docs/PORTAL_LICENSE_FIELDS.md."
Write-Host ""
Write-Host "Install complete."
