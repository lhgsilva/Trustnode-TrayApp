param(
    [string]$CommitMessage = "",
    [string]$CloudApiUrl = "https://trustnode.lsapps.app",
    [switch]$SkipLocalBuild
)

$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $projectRoot

if (-not $SkipLocalBuild) {
    Write-Host "Building local release (desktop/backend/web)..."
    powershell -ExecutionPolicy Bypass -File ".\Trustnode_edge_app\scripts\build-release.ps1" -CloudApiUrl $CloudApiUrl -WebBasePath "/"
}

if (-not $CommitMessage) {
    $CommitMessage = "ci: automated release $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
}

Write-Host "Committing and pushing to main..."
git add -A
try {
    git commit -m $CommitMessage
} catch {
    Write-Host "No new commit created (possibly no changes). Continuing with push."
}
git push origin main

Write-Host ""
Write-Host "Done. GitHub Actions pipeline will run build + deploy + VPS checks automatically."
