$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$backendRoot = Join-Path $projectRoot "backend"
$specFile = Join-Path $backendRoot "trustnode-service.spec"

Write-Host "Building backend executable from $backendRoot"
Push-Location $backendRoot

try {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        throw "python not found in PATH. Install Python and retry."
    }

    python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        throw "Backend dependency install failed (exit code: $LASTEXITCODE)."
    }

    python -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller install failed (exit code: $LASTEXITCODE)."
    }

    # Clean previous output so stale files (e.g. an old onefile exe) cannot
    # leak into the new onedir bundle that electron-builder picks up.
    foreach ($dir in @("dist", "build")) {
        $full = Join-Path $backendRoot $dir
        if (Test-Path $full) {
            Remove-Item -Recurse -Force -LiteralPath $full
        }
    }

    if (-not (Test-Path $specFile)) {
        throw "Spec file not found: $specFile"
    }

    # Drive the build via the spec file: it pins onedir, collects native DLLs,
    # embeds branding assets, and ships vcruntime — none of which is achievable
    # with pure CLI flags.
    python -m PyInstaller --noconfirm --clean $specFile
    if ($LASTEXITCODE -ne 0) {
        throw "Backend executable build failed (exit code: $LASTEXITCODE)."
    }

    $exePath = Join-Path $backendRoot "dist\trustnode-service\trustnode-service.exe"
    if (-not (Test-Path $exePath)) {
        throw "Backend build failed: $exePath not found."
    }

    Write-Host "Backend bundle created: $(Join-Path $backendRoot 'dist\trustnode-service')"
}
finally {
    Pop-Location
}
