$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$sidecarRoot = Join-Path $projectRoot "sidecar"
$specFile = Join-Path $sidecarRoot "trustnode-watchdog.spec"

Write-Host "Building external watchdog sidecar from $sidecarRoot"
Push-Location $sidecarRoot

try {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        throw "python not found in PATH. Install Python and retry."
    }

    # PyInstaller is already installed for the backend build; if not,
    # this picks it up.
    python -m pip install pyinstaller | Out-Null

    foreach ($dir in @("dist", "build")) {
        $full = Join-Path $sidecarRoot $dir
        if (Test-Path $full) {
            Get-ChildItem -LiteralPath $full -Force -ErrorAction SilentlyContinue | ForEach-Object {
                try { Remove-Item -Recurse -Force -LiteralPath $_.FullName -ErrorAction Stop } catch {}
            }
            try { Remove-Item -Recurse -Force -LiteralPath $full -ErrorAction Stop } catch {}
        }
    }

    if (-not (Test-Path $specFile)) {
        throw "Sidecar spec file not found: $specFile"
    }

    python -m PyInstaller --noconfirm --clean $specFile
    if ($LASTEXITCODE -ne 0) {
        throw "Sidecar build failed (exit code: $LASTEXITCODE)."
    }

    $exePath = Join-Path $sidecarRoot "dist\trustnode-watchdog\trustnode-watchdog.exe"
    if (-not (Test-Path $exePath)) {
        throw "Sidecar build failed: $exePath not found."
    }

    Write-Host "Sidecar bundle created: $(Join-Path $sidecarRoot 'dist\trustnode-watchdog')"
}
finally {
    Pop-Location
}
