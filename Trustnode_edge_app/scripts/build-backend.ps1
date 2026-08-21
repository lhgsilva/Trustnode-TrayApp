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

    # Operator 2026-06-25: COMPILE GUARD. Before PyInstaller runs,
    # verify every .py under app/ compiles cleanly. PyInstaller does
    # NOT fail on syntax errors — it silently logs "invalid module
    # named X" to warn-*.txt and ships an EXE with the broken module
    # missing. The EXE then crashes at import time with
    # ModuleNotFoundError, the splash shows the failure, and we lose
    # time chasing a "missing module" that was actually a syntax bug.
    # This guard catches it BEFORE bundling.
    Write-Host "Compile-checking app/ tree before bundling..."
    # Write the checker script to a temp file. Calling `python -c` with
    # a here-string + `2>&1` made PowerShell 5.1 misreport benign
    # SyntaxWarning lines as errors. A file + plain invocation +
    # `-W ignore` (so deprecation/syntax warnings don't print to
    # stderr) avoids both issues. We test $LASTEXITCODE directly so
    # the build only fails on real SyntaxErrors.
    $checkerPath = Join-Path $env:TEMP "trustnode_compile_check.py"
    @"
import py_compile, pathlib, sys, warnings
warnings.simplefilter('ignore')
errs = []
for p in pathlib.Path('app').rglob('*.py'):
    try:
        py_compile.compile(str(p), doraise=True)
    except py_compile.PyCompileError as e:
        errs.append(f'{p}: {e}')
if errs:
    for e in errs: print('SYNTAX-ERROR:', e)
    sys.exit(1)
print('compile-check OK')
"@ | Set-Content -Path $checkerPath -Encoding UTF8
    & python -W ignore $checkerPath
    if ($LASTEXITCODE -ne 0) {
        throw "Backend compile-check FAILED. Fix the syntax errors above before bundling."
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
    # Operator 2026-06-19: tolerate a locked empty directory — Windows
    # sometimes holds a handle on the parent inode after the contents are
    # deleted. PyInstaller will repopulate the directory in place; the
    # "stale onefile leak" risk only matters when files remain.
    foreach ($dir in @("dist", "build")) {
        $full = Join-Path $backendRoot $dir
        if (Test-Path $full) {
            # First delete CONTENTS (this is what matters for stale-leak avoidance).
            Get-ChildItem -LiteralPath $full -Force -ErrorAction SilentlyContinue | ForEach-Object {
                try {
                    Remove-Item -Recurse -Force -LiteralPath $_.FullName -ErrorAction Stop
                } catch {
                    Write-Host "WARN: could not delete $($_.FullName): $($_.Exception.Message)"
                }
            }
            # Then try to remove the (now-empty) dir, but DON'T fail the build if it's locked.
            try {
                Remove-Item -Recurse -Force -LiteralPath $full -ErrorAction Stop
            } catch {
                Write-Host "WARN: $full directory locked by another process; reusing in place."
            }
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

    # Operator 2026-08-21 (BOOT-HEALTH FIX): every build must PROVE the bundled
    # backend answers /api/health fast on a cold start. The probe runs the EXE
    # against a throwaway data dir on an ephemeral port (no .env, no cloud) and
    # fails the build if the first 200 takes longer than the budget.
    Write-Host "Running backend boot self-test (--boot-probe)..."
    & $exePath --boot-probe
    if ($LASTEXITCODE -ne 0) {
        throw "Backend boot self-test FAILED (exit code: $LASTEXITCODE). /api/health did not come up in time - do not ship this build."
    }

    Write-Host "Backend bundle created: $(Join-Path $backendRoot 'dist\trustnode-service')"
}
finally {
    Pop-Location
}
