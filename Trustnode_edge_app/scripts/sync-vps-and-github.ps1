<#
.SYNOPSIS
  Post-build sync: commit changed files to GitHub and push updated backend
  files to the VPS. Run AFTER a successful build-release.ps1.

  Safe by design:
    - Never force-pushes.
    - Only commits when there ARE changes.
    - VPS sync only copies the backend app files that changed (rsync-style),
      then restarts the trustnode-backend service.
    - Every step is guarded; a failure in one does not abort the others.

.PARAMETER GitPush     Commit + push to GitHub (default: $true)
.PARAMETER VpsSync      Copy changed backend files to the VPS + restart (default: $true)
.PARAMETER VpsHost      SSH target (default from env TRUSTNODE_VPS_HOST or root@87.106.7.67)
.PARAMETER VpsKey       SSH key path (default ~/.ssh/trustnode_codex)
.PARAMETER CommitMessage  Commit message.
#>
param(
    [bool]$GitPush = $true,
    [bool]$VpsSync = $true,
    [string]$VpsHost = "",
    [string]$VpsKey = "",
    [string]$CommitMessage = ""
)

$ErrorActionPreference = "Continue"
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)  # .../Tray_app
$edgeRoot = Split-Path -Parent $PSScriptRoot                        # .../Trustnode_edge_app

if (-not $VpsHost) { $VpsHost = if ($env:TRUSTNODE_VPS_HOST) { $env:TRUSTNODE_VPS_HOST } else { "root@87.106.7.67" } }
if (-not $VpsKey)  { $VpsKey  = if ($env:TRUSTNODE_VPS_KEY)  { $env:TRUSTNODE_VPS_KEY }  else { "$HOME/.ssh/trustnode_codex" } }
if (-not $CommitMessage) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm"
    $CommitMessage = "build: sync $ts"
}

Write-Host "=== TrustNode post-build sync ===" -ForegroundColor Cyan
Write-Host "  repo:   $repoRoot"
Write-Host "  edge:   $edgeRoot"
Write-Host "  vps:    $VpsHost"

# ---- 1. GitHub commit + push ------------------------------------------------
if ($GitPush) {
    Write-Host "`n[1/2] GitHub commit + push..." -ForegroundColor Yellow
    Push-Location $repoRoot
    try {
        git add -A 2>&1 | Out-Null
        $status = git status --porcelain 2>&1
        if ($status) {
            git commit -m $CommitMessage 2>&1 | Write-Host
            # Push to the current branch's upstream; never force.
            git push 2>&1 | Write-Host
            Write-Host "  GitHub: pushed." -ForegroundColor Green
        } else {
            Write-Host "  GitHub: nothing to commit." -ForegroundColor Green
        }
    } catch {
        Write-Host "  GitHub sync FAILED: $_" -ForegroundColor Red
    } finally {
        Pop-Location
    }
}

# ---- 2. VPS backend sync + restart -----------------------------------------
if ($VpsSync) {
    Write-Host "`n[2/2] VPS backend sync + restart..." -ForegroundColor Yellow
    # Files the VPS actually runs (backend + intelligence module). We copy the
    # whole backend app + trustnode_intelligence tree; scp is idempotent.
    $vpsAppRoot = "/opt/trustnode-edge/app/Trustnode_edge_app"
    $paths = @(
        "backend/app",
        "trustnode_intelligence"
    )
    try {
        foreach ($p in $paths) {
            $src = Join-Path $edgeRoot $p
            if (-not (Test-Path $src)) { continue }
            # Copy the directory INTO its parent on the VPS so the tree lands
            # at exactly $vpsAppRoot/$p. scp -r copies the folder itself, so we
            # target the PARENT dir on the remote.
            $remoteParent = "$vpsAppRoot/" + ($p -replace '[\\/][^\\/]+$', '')
            $remoteParent = $remoteParent.TrimEnd('/')
            Write-Host "  scp $p -> $remoteParent"
            ssh -i $VpsKey $VpsHost "mkdir -p '$remoteParent'" 2>&1 | Out-Null
            scp -q -i $VpsKey -r $src "$VpsHost`:$remoteParent/" 2>&1 | Write-Host
        }
        Write-Host "  Restarting trustnode-backend on VPS..."
        $active = ssh -i $VpsKey $VpsHost "systemctl restart trustnode-backend; sleep 3; systemctl is-active trustnode-backend" 2>&1
        Write-Host "  VPS service: $active"
        if ("$active".Trim() -eq "active") {
            Write-Host "  VPS: synced + restarted OK." -ForegroundColor Green
        } else {
            Write-Host "  VPS restart did not report 'active' — check the service." -ForegroundColor Red
        }
    } catch {
        Write-Host "  VPS sync FAILED: $_" -ForegroundColor Red
    }
}

Write-Host "`n=== sync complete ===" -ForegroundColor Cyan
