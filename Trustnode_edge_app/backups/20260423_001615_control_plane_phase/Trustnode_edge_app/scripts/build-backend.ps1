$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$backendRoot = Join-Path $projectRoot "backend"

Write-Host "Building backend executable from $backendRoot"
Push-Location $backendRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "python not found in PATH. Install Python and retry."
}

python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Pop-Location
    throw "Backend dependency install failed (exit code: $LASTEXITCODE)."
}

python -m pip install pyinstaller
if ($LASTEXITCODE -ne 0) {
    Pop-Location
    throw "PyInstaller install failed (exit code: $LASTEXITCODE)."
}

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --paths . `
    --collect-submodules app `
    --hidden-import app.main `
    --hidden-import app.config `
    --hidden-import app.models `
    --hidden-import app.state `
    --hidden-import app.routers.health `
    --hidden-import app.routers.app_store `
    --hidden-import app.routers.auth `
    --hidden-import app.routers.control_plane `
    --hidden-import app.routers.plc `
    --hidden-import app.services.app_store `
    --hidden-import app.services.control_plane_store `
    --hidden-import app.services.plc_manager `
    --name trustnode-service `
    app\__main__.py
if ($LASTEXITCODE -ne 0) {
    Pop-Location
    throw "Backend executable build failed (exit code: $LASTEXITCODE)."
}

if (-not (Test-Path ".\dist\trustnode-service.exe")) {
    throw "Backend build failed: dist\\trustnode-service.exe not found."
}

Write-Host "Backend executable created: $backendRoot\\dist\\trustnode-service.exe"
Pop-Location
