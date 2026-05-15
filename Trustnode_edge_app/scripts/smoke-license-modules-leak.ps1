param(
    [string]$BaseUrl       = "http://127.0.0.1:9091",
    [string]$MasterUser    = "admin",
    [string]$MasterPass    = "admin",
    [string]$AcmeAdminUser,
    [string]$AcmeAdminPass,
    [string]$AcmeTenantId,
    [string]$AcmeLicenseId
)

# Verifies that /licenses/{id}/modules cannot be accessed cross-tenant
# AFTER the tenant-binding fix:
#   * Master admin reading own + foreign license modules -> 200 (both).
#   * Master admin writing foreign license modules -> 200 (global allowed).
#   * Acme admin reading OWN license modules -> 200.
#   * Acme admin reading a foreign license modules -> 403.
#   * Acme admin writing a foreign license modules -> 403.
#   * Unknown license id from any caller -> 404.

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Invoke-Probe {
    param([string]$Method, [string]$Url, [object]$Body = $null, [hashtable]$Headers = @{})
    try {
        if ($null -eq $Body) {
            $r = Invoke-WebRequest -Method $Method -Uri $Url -Headers $Headers -TimeoutSec 30 -UseBasicParsing
        } else {
            $r = Invoke-WebRequest -Method $Method -Uri $Url -Headers $Headers -ContentType "application/json" -Body ($Body | ConvertTo-Json -Depth 12) -TimeoutSec 30 -UseBasicParsing
        }
        $obj = $null; try { $obj = $r.Content | ConvertFrom-Json } catch {}
        return [pscustomobject]@{ status = [int]$r.StatusCode; body = $obj }
    } catch [System.Net.WebException] {
        $resp = $_.Exception.Response
        if ($null -ne $resp) { return [pscustomobject]@{ status = [int]$resp.StatusCode; body = $null } }
        return [pscustomobject]@{ status = -1; body = $null }
    } catch {
        return [pscustomobject]@{ status = -1; body = $null }
    }
}

function Login([string]$User, [string]$Pass) {
    $r = Invoke-Probe -Method POST -Url "$BaseUrl/api/auth/login" -Body @{ username = $User; password = $Pass }
    if ($r.status -ne 200 -or -not $r.body.token) { throw "Login failed for $User (status=$($r.status))" }
    return [string]$r.body.token
}

$results = @()
function Check([string]$name, [int]$expectedStatus, [object]$probe) {
    $ok = $probe.status -eq $expectedStatus
    $script:results += [pscustomobject]@{ step = $name; expected = $expectedStatus; actual = $probe.status; ok = $ok }
    $tag = if ($ok) { "PASS" } else { "FAIL" }
    $col = if ($ok) { "Green" } else { "Red" }
    Write-Host ("  [{0}] {1} -- expected={2} actual={3}" -f $tag, $name, $expectedStatus, $probe.status) -ForegroundColor $col
}

Write-Host "Logging in..."
$masterTok = Login -User $MasterUser    -Pass $MasterPass
$adminTok  = Login -User $AcmeAdminUser -Pass $AcmeAdminPass
$master = @{ Authorization = "Bearer $masterTok" }
$admin  = @{ Authorization = "Bearer $adminTok"  }

# Find a license that belongs to a tenant OTHER than acme.
Write-Host "`nLooking up a foreign license id via master /tenants + /licenses..." -ForegroundColor Yellow
$tenants = (Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/tenants" -Headers $master).body.rows
$foreignLicenseId = $null
foreach ($t in $tenants) {
    if (-not $t -or -not $t.tenant_id) { continue }
    if ($t.tenant_id -eq $AcmeTenantId) { continue }
    $tid = $t.tenant_id
    $lic = (Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/licenses?tenant_id=$tid" -Headers $master).body.rows
    if (@($lic).Count -gt 0) { $foreignLicenseId = ($lic | Select-Object -First 1).license_id; break }
}
if (-not $foreignLicenseId) { throw "No foreign license available to probe; provision a second tenant first." }
Write-Host "Foreign license to probe: $foreignLicenseId (tenant=$tid)"

# Probes
Write-Host "`n[1] Master READ own + foreign license modules -> 200" -ForegroundColor Yellow
Check "master:read-own"     200 (Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/licenses/$AcmeLicenseId/modules" -Headers $master)
Check "master:read-foreign" 200 (Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/licenses/$foreignLicenseId/modules" -Headers $master)

Write-Host "`n[2] Acme admin READ OWN license modules -> 200" -ForegroundColor Yellow
Check "admin:read-own" 200 (Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/licenses/$AcmeLicenseId/modules" -Headers $admin)

Write-Host "`n[3] Acme admin READ FOREIGN license modules -> 403" -ForegroundColor Yellow
Check "admin:read-foreign" 403 (Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/licenses/$foreignLicenseId/modules" -Headers $admin)

Write-Host "`n[4] Acme admin WRITE FOREIGN license modules -> 403" -ForegroundColor Yellow
$writeBody = @{ modules = @(@{ module_key = "dashboard"; enabled = $false }) }
Check "admin:write-foreign" 403 (Invoke-Probe -Method PUT -Url "$BaseUrl/api/control-plane/licenses/$foreignLicenseId/modules" -Headers $admin -Body $writeBody)

Write-Host "`n[5] Acme admin WRITE OWN license modules -> 200" -ForegroundColor Yellow
$ownWrite = @{ modules = @(@{ module_key = "dashboard"; enabled = $true }) }
Check "admin:write-own" 200 (Invoke-Probe -Method PUT -Url "$BaseUrl/api/control-plane/licenses/$AcmeLicenseId/modules" -Headers $admin -Body $ownWrite)

Write-Host "`n[6] Any caller READ unknown license -> 404" -ForegroundColor Yellow
Check "admin:read-unknown"  404 (Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/licenses/lic-does-not-exist-xyz/modules" -Headers $admin)
Check "master:read-unknown" 404 (Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/licenses/lic-does-not-exist-xyz/modules" -Headers $master)

# Summary
Write-Host ""
$results | Format-Table -AutoSize step, expected, actual, ok
$failed = @($results | Where-Object { -not $_.ok }).Count
if ($failed -gt 0) {
    Write-Host "LICENSE-MODULES LEAK SMOKE FAILED ($failed)" -ForegroundColor Red
    exit 1
}
Write-Host "LICENSE-MODULES LEAK SMOKE PASSED" -ForegroundColor Green
