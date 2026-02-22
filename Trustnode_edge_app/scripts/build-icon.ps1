$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$repoRoot = Resolve-Path (Join-Path $projectRoot "..")
$sourcePng = Join-Path $repoRoot "trustnode_logo.png"
$desktopAssets = Join-Path $projectRoot "desktop\assets"
$targetIco = Join-Path $desktopAssets "trustnode_logo.ico"

if (-not (Test-Path $sourcePng)) {
    throw "Source PNG not found: $sourcePng"
}

New-Item -ItemType Directory -Force -Path $desktopAssets | Out-Null

$pythonCandidates = @(
    (Join-Path $repoRoot "tray_app_env\Scripts\python.exe"),
    "python"
)

$pythonExe = $null
foreach ($candidate in $pythonCandidates) {
    if ($candidate -eq "python") {
        if (Get-Command python -ErrorAction SilentlyContinue) {
            $pythonExe = "python"
            break
        }
    } elseif (Test-Path $candidate) {
        $pythonExe = $candidate
        break
    }
}

if (-not $pythonExe) {
    throw "Python executable not found for icon generation."
}

$script = @"
from PIL import Image
src = r"$sourcePng"
out = r"$targetIco"
img = Image.open(src).convert("RGBA")
if img.width < 512 or img.height < 512:
    img = img.resize((512, 512), Image.Resampling.LANCZOS)
img.save(out, format="ICO", sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
print(out)
"@

$script | & $pythonExe -
if ($LASTEXITCODE -ne 0) {
    throw "Icon generation failed (exit code: $LASTEXITCODE). Ensure Pillow is installed."
}

Write-Host "Icon created: $targetIco"
