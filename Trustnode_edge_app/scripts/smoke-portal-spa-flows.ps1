param(
    [string]$BaseUrl       = "https://trustnode.lsapps.app",
    [string]$MasterUser    = "admin",
    [string]$MasterPass    = "admin",
    [string]$AcmeAdminUser,
    [string]$AcmeAdminPass,
    [string]$AcmeClientUser,
    [string]$AcmeClientPass,
    [string]$AcmeTenantId,
    [string]$AcmeCustomerId,
    [string]$AcmeLicenseId
)

# Replays the exact same API calls the React /portal/ SPA makes after
# the three roles sign in, and asserts each role sees only what it
# should. This is the "test through the portal page" version of the
# segregation smoke -- the SPA itself just renders these payloads, so if
# the payloads are scoped, the SPA is too.

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Invoke-Probe {
    param([string]$Method, [string]$Url, [hashtable]$Headers = @{})
    try {
        $r = Invoke-WebRequest -Method $Method -Uri $Url -Headers $Headers -TimeoutSec 30 -UseBasicParsing
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
    $r = Invoke-WebRequest -Method POST -Uri "$BaseUrl/api/auth/login" -ContentType "application/json" -Body (@{ username = $User; password = $Pass } | ConvertTo-Json) -TimeoutSec 30 -UseBasicParsing
    $body = $r.Content | ConvertFrom-Json
    if (-not $body.token) { throw "Login failed for $User" }
    return [string]$body.token
}

$results = @()
function Check([string]$step, [bool]$expected, [bool]$actual, [string]$detail) {
    $ok = $expected -eq $actual
    $script:results += [pscustomobject]@{ step = $step; expected = $expected; actual = $actual; ok = $ok; detail = $detail }
    $tag = if ($ok) { "PASS" } else { "FAIL" }
    $col = if ($ok) { "Green" } else { "Red" }
    Write-Host ("  [{0}] {1} -- {2}" -f $tag, $step, $detail) -ForegroundColor $col
}

Write-Host "Portal SPA flow smoke against $BaseUrl" -ForegroundColor Cyan

# Sign in (the same calls /portal/ makes from its login screen) ---------
$masterTok = Login -User $MasterUser     -Pass $MasterPass
$adminTok  = Login -User $AcmeAdminUser  -Pass $AcmeAdminPass
$clientTok = Login -User $AcmeClientUser -Pass $AcmeClientPass
$master = @{ Authorization = "Bearer $masterTok" }
$admin  = @{ Authorization = "Bearer $adminTok"  }
$client = @{ Authorization = "Bearer $clientTok" }

# ----- /api/auth/me (the SPA calls this on every page load) ----------
Write-Host "`n[/auth/me] reflects the right tenant + role" -ForegroundColor Yellow
$mMe = Invoke-Probe -Method GET -Url "$BaseUrl/api/auth/me" -Headers $master
Check "master:/me-default-tenant" $true ([string]$mMe.body.user.tenant_id -eq "default") "tenant=$($mMe.body.user.tenant_id) role=$($mMe.body.user.role)"
$aMe = Invoke-Probe -Method GET -Url "$BaseUrl/api/auth/me" -Headers $admin
Check "admin:/me-acme-tenant"     $true ([string]$aMe.body.user.tenant_id -eq $AcmeTenantId) "tenant=$($aMe.body.user.tenant_id) role=$($aMe.body.user.role)"
$cMe = Invoke-Probe -Method GET -Url "$BaseUrl/api/auth/me" -Headers $client
Check "client:/me-acme-tenant"    $true ([string]$cMe.body.user.tenant_id -eq $AcmeTenantId) "tenant=$($cMe.body.user.tenant_id) role=$($cMe.body.user.role)"

# ----- Runtime context (the portal home page calls this) -----------
Write-Host "`n[/runtime-context] portal home page bootstrap" -ForegroundColor Yellow
$mCtx = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/runtime-context" -Headers $master
Check "master:runtime-context"  $true ($mCtx.status -eq 200) "status=$($mCtx.status)"
$aCtx = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/runtime-context" -Headers $admin
Check "admin:runtime-context"   $true ($aCtx.status -eq 200) "status=$($aCtx.status)"
$cCtx = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/runtime-context" -Headers $client
Check "client:runtime-context"  $true ($cCtx.status -eq 200) "status=$($cCtx.status)"

# ----- Tenants tab (master-only on the portal) ---------------------
Write-Host "`n[Tenants tab] master-only" -ForegroundColor Yellow
$mT = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/tenants" -Headers $master
Check "master:tenants-200"           $true ($mT.status -eq 200) "status=$($mT.status) count=$([int]@($mT.body.rows).Count)"
$aT = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/tenants" -Headers $admin
Check "admin:tenants-forbidden"      $true ($aT.status -eq 403) "status=$($aT.status)"
$cT = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/tenants" -Headers $client
Check "client:tenants-forbidden"     $true ($cT.status -eq 403) "status=$($cT.status)"

# ----- Customers tab ---------------------------------------------
Write-Host "`n[Customers tab] each role sees only own scope" -ForegroundColor Yellow
$mC = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/customers?tenant_id=default" -Headers $master
Check "master:customers-default"      $true ($mC.status -eq 200) "status=$($mC.status) rows=$([int]@($mC.body.rows).Count)"
$aC = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/customers?tenant_id=$AcmeTenantId" -Headers $admin
$aOther = @($aC.body.rows | Where-Object { $_.customer_id -ne $AcmeCustomerId })
Check "admin:customers-own"           $true ($aC.status -eq 200 -and $aOther.Count -eq 0) "rows=$([int]@($aC.body.rows).Count) other=$($aOther.Count)"
$aCx = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/customers?tenant_id=default" -Headers $admin
Check "admin:customers-cross-tenant"  $true ($aCx.status -eq 403) "status=$($aCx.status)"

# ----- Users tab (Users and Access page in the portal) -----------
Write-Host "`n[Users tab]" -ForegroundColor Yellow
$mU = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/users?tenant_id=default" -Headers $master
Check "master:users-default"       $true ($mU.status -eq 200) "rows=$([int]@($mU.body.rows).Count)"
$aU = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/users?tenant_id=$AcmeTenantId" -Headers $admin
$leak = @($aU.body.rows | Where-Object { $_.username -eq $MasterUser })
Check "admin:users-own-noleak"     $true ($aU.status -eq 200 -and $leak.Count -eq 0) "rows=$([int]@($aU.body.rows).Count) master_leak=$($leak.Count)"
$cU = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/users?tenant_id=$AcmeTenantId" -Headers $client
# Client viewer should be able to READ (so the portal can show their profile) but not write.
Check "client:users-read-ok"       $true ($cU.status -eq 200) "status=$($cU.status)"

# ----- Licenses tab (and modules) -------------------------------
Write-Host "`n[Licenses tab] now tenant-bound on modules sub-resource" -ForegroundColor Yellow
$aLic = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/licenses?tenant_id=$AcmeTenantId" -Headers $admin
Check "admin:licenses-own"         $true ($aLic.status -eq 200) "rows=$([int]@($aLic.body.rows).Count)"
$aMod = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/licenses/$AcmeLicenseId/modules" -Headers $admin
Check "admin:license-modules-own"  $true ($aMod.status -eq 200) "status=$($aMod.status)"

# Build a foreign license id from master's view of any other tenant
$foreignLid = $null
foreach ($t in $mT.body.rows) {
    if (-not $t -or -not $t.tenant_id) { continue }
    if ($t.tenant_id -eq $AcmeTenantId) { continue }
    $tid = $t.tenant_id
    $lic = (Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/licenses?tenant_id=$tid" -Headers $master).body.rows
    if (@($lic).Count -gt 0) { $foreignLid = ($lic | Select-Object -First 1).license_id; break }
}
if ($foreignLid) {
    $aForeign = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/licenses/$foreignLid/modules" -Headers $admin
    Check "admin:license-modules-foreign"  $true ($aForeign.status -eq 403) "status=$($aForeign.status) lid=$foreignLid"
    $mForeign = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/licenses/$foreignLid/modules" -Headers $master
    Check "master:license-modules-foreign" $true ($mForeign.status -eq 200) "status=$($mForeign.status) lid=$foreignLid"
}

# ----- Edges tab ------------------------------------------------
Write-Host "`n[Edges tab]" -ForegroundColor Yellow
$aE = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/edges?tenant_id=$AcmeTenantId" -Headers $admin
$wrong = @($aE.body.rows | Where-Object { $_.customer_id -and $_.customer_id -ne $AcmeCustomerId })
Check "admin:edges-own-only"       $true ($aE.status -eq 200 -and $wrong.Count -eq 0) "rows=$([int]@($aE.body.rows).Count) wrong=$($wrong.Count)"

# ----- Activation codes tab -------------------------------------
Write-Host "`n[Activation codes tab]" -ForegroundColor Yellow
$aA = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/activation-codes?tenant_id=$AcmeTenantId" -Headers $admin
Check "admin:activation-codes-own" $true ($aA.status -eq 200) "rows=$([int]@($aA.body.rows).Count)"
$aAx = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/activation-codes?tenant_id=default" -Headers $admin
Check "admin:activation-codes-cross-tenant" $true ($aAx.status -eq 403) "status=$($aAx.status)"

# ----- App-store / dashboard data the SPA fetches ---------------
Write-Host "`n[Dashboard / app-store reads]" -ForegroundColor Yellow
$aLive = Invoke-Probe -Method GET -Url "$BaseUrl/api/app-store/live" -Headers $admin
Check "admin:app-store-live"  $true ($aLive.status -eq 200) "status=$($aLive.status) rows=$([int]@($aLive.body.rows).Count)"
$cLive = Invoke-Probe -Method GET -Url "$BaseUrl/api/app-store/live" -Headers $client
Check "client:app-store-live" $true ($cLive.status -eq 200) "status=$($cLive.status) rows=$([int]@($cLive.body.rows).Count)"

# Cross-tenant data probe -- viewer/admin should not see other tenants' live rows.
$aLiveRows = @($aLive.body.rows)
$crossTenantRows = @($aLiveRows | Where-Object { $_.tenant_id -and $_.tenant_id -ne $AcmeTenantId })
Check "admin:live-only-own-tenant"  $true ($crossTenantRows.Count -eq 0) "cross_tenant_rows=$($crossTenantRows.Count)"

# Summary
Write-Host ""
$results | Format-Table -AutoSize step, expected, actual, ok, detail
$failed = @($results | Where-Object { -not $_.ok }).Count
$passed = @($results | Where-Object { $_.ok }).Count
if ($failed -gt 0) {
    Write-Host "PORTAL SPA FLOW SMOKE FAILED ($failed failure(s), $passed passes)" -ForegroundColor Red
    exit 1
}
Write-Host "PORTAL SPA FLOW SMOKE PASSED: $passed checks" -ForegroundColor Green
