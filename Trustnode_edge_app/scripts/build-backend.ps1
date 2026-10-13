$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$backendRoot = Join-Path $projectRoot "backend"
$specFile = Join-Path $backendRoot "trustnode-service.spec"

Write-Host "Building backend executable from $backendRoot"

# 2026-08-31: PyInstaller's analysis pass imports app.state, and importing it
# constructs AppStore(), which runs _ensure_schema() - CREATE TABLE / ALTER
# TABLE - against whatever workspace it resolves. With nothing set that is the
# DEFAULT path: the operator's live ~/.trustnode_edge database. The build log
# showed it being attempted and refused only because the running app held the
# file read-only. A build must never touch the data it is being built for, so
# the import side effects are pointed at a scratch workspace and the previous
# values are restored afterwards.
$buildWorkspace = Join-Path $env:TEMP ("trustnode-build-ws-" + [System.Guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force $buildWorkspace | Out-Null
$prevDataDir = $env:TRUSTNODE_DATA_DIR
$prevStorePath = $env:TRUSTNODE_APP_STORE_PATH
$prevSkipDotenv = $env:TRUSTNODE_SKIP_DOTENV
$env:TRUSTNODE_DATA_DIR = $buildWorkspace
$env:TRUSTNODE_APP_STORE_PATH = Join-Path $buildWorkspace "build_only.db"
$env:TRUSTNODE_SKIP_DOTENV = "1"
Write-Host "Import side effects isolated to $buildWorkspace"

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

    # Operator 2026-10-07 (UPGRADE BOOT FIX): the probe above sets
    # TRUSTNODE_APP_STORE_PATH, which takes the first branch of
    # _resolve_db_path() - so it has never exercised the path an UPGRADE
    # takes, where a legacy ~\.trustnode_edge store exists and
    # %ProgramData% does not. That is exactly where the build that "failed
    # to start on a computer with an older version" went wrong: the whole
    # legacy database was copied before anything could answer /api/health.
    #
    # This pass drives the packaged EXE the way the desktop shell does -
    # no store-path override - with HOME and PROGRAMDATA pointed at scratch
    # directories holding a real (empty) SQLite file, and fails the build if
    # first health takes longer than the shell would wait.
    Write-Host "Running upgrade-shape boot self-test..."
    $upg = Join-Path $env:TEMP ("tn-upgrade-probe-" + [System.Guid]::NewGuid().ToString("N").Substring(0, 8))
    $upgHome = Join-Path $upg "home"
    $upgPd = Join-Path $upg "programdata"
    $legacyDir = Join-Path $upgHome ".trustnode_edge\data"
    New-Item -ItemType Directory -Force $legacyDir | Out-Null
    New-Item -ItemType Directory -Force $upgPd | Out-Null
    # A VALID SQLite file with real pages in it. sqlite3.connect() alone
    # leaves a ZERO-byte file, and a 0 MB store is not "over the cap" - the
    # first run of this probe copied it and failed itself. ~2 MB against a
    # 1 MB cap exercises the adopt-in-place decision.
    $legacyDb = Join-Path $legacyDir "trustnode_app_store.db"
    & python -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('CREATE TABLE pad(x TEXT)'); c.executemany('INSERT INTO pad VALUES (?)', [('x'*200,)]*12000); c.commit(); c.close()" $legacyDb
    if (-not (Test-Path $legacyDb)) { throw "upgrade probe: could not seed a legacy store" }
    $legacyMb = [math]::Round((Get-Item $legacyDb).Length / 1MB, 2)
    if ($legacyMb -lt 1.5) { throw "upgrade probe: seeded store is only $legacyMb MB - it must exceed the cap below or this proves nothing" }
    Write-Host "[upgrade-probe] seeded a $legacyMb MB legacy store"

    $probePort = 8793
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exePath
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.EnvironmentVariables["USERPROFILE"] = $upgHome
    $psi.EnvironmentVariables["PROGRAMDATA"] = $upgPd
    $psi.EnvironmentVariables["TRUSTNODE_PORT"] = "$probePort"
    $psi.EnvironmentVariables["TRUSTNODE_SKIP_DOTENV"] = "1"
    $psi.EnvironmentVariables["TRUSTNODE_DISABLE_CONFIG_PUSH"] = "1"
    $psi.EnvironmentVariables["TRUSTNODE_BOOT_INTEGRITY_CHECK"] = "never"
    # A 1 MB cap against the ~2 MB store seeded above, so this exercises the
    # adopt-in-place decision rather than a copy.
    $psi.EnvironmentVariables["TRUSTNODE_MIGRATE_MAX_MB"] = "1"
    foreach ($k in @("TRUSTNODE_DATA_DIR", "TRUSTNODE_APP_STORE_PATH")) {
        $psi.EnvironmentVariables.Remove($k) | Out-Null
    }
    $upgProc = [System.Diagnostics.Process]::Start($psi)
    $budget = 60
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $healthy = $false
    while ($sw.Elapsed.TotalSeconds -lt $budget) {
        Start-Sleep -Milliseconds 1000
        if ($upgProc.HasExited) { break }
        try {
            $r = Invoke-WebRequest "http://127.0.0.1:$probePort/api/health" -TimeoutSec 3 -UseBasicParsing
            if ($r.StatusCode -eq 200) { $healthy = $true; break }
        } catch { }
    }
    $elapsed = [int]$sw.Elapsed.TotalSeconds
    if (-not $upgProc.HasExited) { try { $upgProc.Kill() } catch { } }
    Start-Sleep -Milliseconds 500
    $copied = Test-Path (Join-Path $upgPd "TrustNode\edge\trustnode_app_store.db")
    Remove-Item -Recurse -Force $upg -ErrorAction SilentlyContinue
    if (-not $healthy) {
        throw "Upgrade-shape boot self-test FAILED: /api/health did not answer within ${budget}s with a legacy store present. This is the shape that failed on a customer machine - do not ship this build."
    }
    if ($copied) {
        throw "Upgrade-shape boot self-test FAILED: the legacy store was COPIED into ProgramData despite being over the migration cap. That copy is what delayed startup past the launcher's budget."
    }
    Write-Host "[upgrade-probe] PASS: /api/health 200 after ${elapsed}s, legacy store adopted in place"

    Write-Host "Backend bundle created: $(Join-Path $backendRoot 'dist\trustnode-service')"
}
finally {
    # Put the environment back, and take the scratch workspace with us.
    $env:TRUSTNODE_DATA_DIR = $prevDataDir
    $env:TRUSTNODE_APP_STORE_PATH = $prevStorePath
    $env:TRUSTNODE_SKIP_DOTENV = $prevSkipDotenv
    if ($buildWorkspace -and (Test-Path $buildWorkspace)) {
        try { Remove-Item -Recurse -Force $buildWorkspace -ErrorAction Stop } catch {}
    }
    Pop-Location
}
