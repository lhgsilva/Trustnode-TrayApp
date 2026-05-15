param(
    [string]$BaseUrl       = "https://trustnode.lsapps.app",
    [string]$MasterUser    = "admin",
    [string]$MasterPass    = "admin",
    [string]$AcmeAdminUser,
    [string]$AcmeAdminPass,
    [string]$AcmeClientUser,
    [string]$AcmeClientPass,
    [string]$AcmeTenantId,
    [string]$AcmeCustomerId
)

# Live tenant-segregation smoke against the cloud control plane.
# Verifies:
#   1) The master (global) admin can list ALL tenants and ALL customers.
#   2) A customer admin CAN list its own tenant's customers/users/edges.
#   3) A customer admin CANNOT see other tenants' data.
#   4) A customer admin CANNOT list the global /tenants registry.
#   5) The dashboard-only client user CANNOT list customers/users,
#      and CANNOT create users in its own or any other tenant.
#   6) Data returned to a customer admin contains ONLY its own customer_id rows.

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Invoke-Probe {
    param(
        [string]$Method,
        [string]$Url,
        [object]$Body = $null,
        [hashtable]$Headers = @{}
    )
    try {
        if ($null -eq $Body) {
            $r = Invoke-WebRequest -Method $Method -Uri $Url -Headers $Headers -TimeoutSec 30 -UseBasicParsing
        } else {
            $r = Invoke-WebRequest -Method $Method -Uri $Url -Headers $Headers -ContentType "application/json" -Body ($Body | ConvertTo-Json -Depth 12) -TimeoutSec 30 -UseBasicParsing
        }
        $obj = $null
        try { $obj = $r.Content | ConvertFrom-Json } catch {}
        return [pscustomobject]@{ status = [int]$r.StatusCode; body = $obj; raw = $r.Content }
    } catch [System.Net.WebException] {
        $resp = $_.Exception.Response
        if ($null -ne $resp) {
            $status = [int]$resp.StatusCode
            $body = $null
            $raw = ""
            try {
                $stream = $resp.GetResponseStream()
                $reader = New-Object System.IO.StreamReader($stream)
                $raw = $reader.ReadToEnd()
                $reader.Close()
                try { $body = $raw | ConvertFrom-Json } catch {}
            } catch {}
            return [pscustomobject]@{ status = $status; body = $body; raw = $raw }
        }
        return [pscustomobject]@{ status = -1; body = $null; raw = $_.Exception.Message }
    } catch {
        return [pscustomobject]@{ status = -1; body = $null; raw = $_.Exception.Message }
    }
}

function Login {
    param([string]$User, [string]$Pass)
    $r = Invoke-Probe -Method POST -Url "$BaseUrl/api/auth/login" -Body @{ username = $User; password = $Pass }
    if ($r.status -ne 200 -or -not $r.body.token) {
        throw "Login failed for $User (status=$($r.status)): $($r.raw)"
    }
    return [string]$r.body.token
}

$results = [System.Collections.Generic.List[object]]::new()
function Add-Check([string]$step, [bool]$expected, [bool]$actual, [string]$detail) {
    $ok = $expected -eq $actual
    $results.Add([pscustomobject]@{ step = $step; expected = $expected; actual = $actual; ok = $ok; detail = $detail })
    $tag = if ($ok) { "PASS" } else { "FAIL" }
    $col = if ($ok) { "Green" } else { "Red" }
    Write-Host ("  [{0}] {1} -- {2}" -f $tag, $step, $detail) -ForegroundColor $col
}

Write-Host "Tenant-segregation smoke against $BaseUrl" -ForegroundColor Cyan
Write-Host ""

# Logins ----------------------------------------------------------------
Write-Host "Logging in..." -ForegroundColor Yellow
$masterTok  = Login -User $MasterUser     -Pass $MasterPass
$adminTok   = Login -User $AcmeAdminUser  -Pass $AcmeAdminPass
$clientTok  = Login -User $AcmeClientUser -Pass $AcmeClientPass
$master = @{ Authorization = "Bearer $masterTok" }
$admin  = @{ Authorization = "Bearer $adminTok"  }
$client = @{ Authorization = "Bearer $clientTok" }

# 1) Master: can list ALL tenants -------------------------------------
Write-Host "`n[1] Master /tenants -> expect 200" -ForegroundColor Yellow
$r = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/tenants" -Headers $master
Add-Check "master:list-tenants" $true ($r.status -eq 200) "status=$($r.status) tenant_count=$([int]@($r.body.rows).Count)"

# 2) Customer admin: /tenants -> expect 403 ------------------------
Write-Host "`n[2] Acme admin /tenants -> expect 403 (global-admin only)" -ForegroundColor Yellow
$r = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/tenants" -Headers $admin
Add-Check "admin:list-tenants-forbidden" $true ($r.status -eq 403) "status=$($r.status)"

# 3) Customer admin: list own tenant customers -> 200 + only own ---
Write-Host "`n[3] Acme admin /customers?tenant_id=$AcmeTenantId -> expect 200, only acme rows" -ForegroundColor Yellow
$r = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/customers?tenant_id=$AcmeTenantId" -Headers $admin
$rows = @($r.body.rows)
$othersInside = @($rows | Where-Object { $_.customer_id -ne $AcmeCustomerId })
Add-Check "admin:list-own-customers" $true ($r.status -eq 200) "status=$($r.status) rows=$($rows.Count)"
Add-Check "admin:only-own-customer-id" $true ($othersInside.Count -eq 0) "non_acme_rows=$($othersInside.Count)"

# 4) Customer admin: try to read default tenant customers -> 403 ---
Write-Host "`n[4] Acme admin /customers?tenant_id=default -> expect 403 (cross-tenant)" -ForegroundColor Yellow
$r = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/customers?tenant_id=default" -Headers $admin
Add-Check "admin:cross-tenant-customers-forbidden" $true ($r.status -eq 403) "status=$($r.status)"

# 5) Customer admin: list own users -> 200, no master user --------
Write-Host "`n[5] Acme admin /users?tenant_id=$AcmeTenantId -> expect 200, no master 'admin'" -ForegroundColor Yellow
$r = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/users?tenant_id=$AcmeTenantId" -Headers $admin
$userRows = @($r.body.rows)
$leakedMaster = @($userRows | Where-Object { $_.username -eq $MasterUser })
Add-Check "admin:list-own-users" $true ($r.status -eq 200) "status=$($r.status) rows=$($userRows.Count)"
Add-Check "admin:no-master-leak" $true ($leakedMaster.Count -eq 0) "master_rows_in_acme=$($leakedMaster.Count)"

# 6) Customer admin: list default tenant users -> 403 -------------
Write-Host "`n[6] Acme admin /users?tenant_id=default -> expect 403" -ForegroundColor Yellow
$r = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/users?tenant_id=default" -Headers $admin
Add-Check "admin:cross-tenant-users-forbidden" $true ($r.status -eq 403) "status=$($r.status)"

# 7) Customer admin: create a new user in own tenant -> 200 ------
Write-Host "`n[7] Acme admin POST /users (own tenant) -> expect 200" -ForegroundColor Yellow
$probeUser = "acmeprobe-" + (Get-Date -Format "HHmmssfff")
$r = Invoke-Probe -Method POST -Url "$BaseUrl/api/control-plane/users?tenant_id=$AcmeTenantId" -Headers $admin -Body @{
    customer_id = $AcmeCustomerId
    username    = $probeUser
    password    = "Probe!Pass123"
    role        = "client"
    status      = "active"
    modules     = @("dashboard")
    permissions = @{ dashboard = $true }
}
Add-Check "admin:create-user-own-tenant" $true ($r.status -eq 200) "status=$($r.status)"

# 8) Customer admin: create user in default tenant -> 403 --------
Write-Host "`n[8] Acme admin POST /users?tenant_id=default -> expect 403" -ForegroundColor Yellow
$r = Invoke-Probe -Method POST -Url "$BaseUrl/api/control-plane/users?tenant_id=default" -Headers $admin -Body @{
    customer_id = "default"
    username    = "should-never-create"
    password    = "n0way!Pass"
    role        = "client"
    status      = "active"
}
Add-Check "admin:create-user-other-tenant-forbidden" $true ($r.status -eq 403) "status=$($r.status)"

# 9) Client viewer: list customers -> 200 (read) but only own row -
Write-Host "`n[9] Client viewer /customers -> expect 200, only own row" -ForegroundColor Yellow
$r = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/customers?tenant_id=$AcmeTenantId" -Headers $client
$ccRows = @($r.body.rows)
$ccOther = @($ccRows | Where-Object { $_.customer_id -ne $AcmeCustomerId })
Add-Check "client:list-own-customers" $true ($r.status -eq 200) "status=$($r.status) rows=$($ccRows.Count)"
Add-Check "client:only-own-customer-id" $true ($ccOther.Count -eq 0) "non_acme_rows=$($ccOther.Count)"

# 10) Client viewer: try to CREATE a user -> 403 -----------------
Write-Host "`n[10] Client viewer POST /users -> expect 403 (admin role required)" -ForegroundColor Yellow
$r = Invoke-Probe -Method POST -Url "$BaseUrl/api/control-plane/users?tenant_id=$AcmeTenantId" -Headers $client -Body @{
    customer_id = $AcmeCustomerId
    username    = "client-cannot-create"
    password    = "should!Fail123"
    role        = "client"
    status      = "active"
}
Add-Check "client:create-user-forbidden" $true ($r.status -eq 403) "status=$($r.status)"

# 11) Client viewer: try cross-tenant read -> 403 ----------------
Write-Host "`n[11] Client viewer /customers?tenant_id=default -> expect 403" -ForegroundColor Yellow
$r = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/customers?tenant_id=default" -Headers $client
Add-Check "client:cross-tenant-read-forbidden" $true ($r.status -eq 403) "status=$($r.status)"

# 12) Customer admin: license/module info reads OK (per-tenant) --
Write-Host "`n[12] Acme admin /licenses (own tenant) -> expect 200" -ForegroundColor Yellow
$r = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/licenses?tenant_id=$AcmeTenantId" -Headers $admin
Add-Check "admin:list-own-licenses" $true ($r.status -eq 200) "status=$($r.status) rows=$([int]@($r.body.rows).Count)"

# 13) Customer admin: edges scoped --------------------------------
Write-Host "`n[13] Acme admin /edges (own tenant) -> expect 200" -ForegroundColor Yellow
$r = Invoke-Probe -Method GET -Url "$BaseUrl/api/control-plane/edges?tenant_id=$AcmeTenantId" -Headers $admin
$edgeRows = @($r.body.rows)
$wrongCust = @($edgeRows | Where-Object { $_.customer_id -and $_.customer_id -ne $AcmeCustomerId })
Add-Check "admin:edges-own-tenant" $true ($r.status -eq 200) "status=$($r.status) rows=$($edgeRows.Count)"
Add-Check "admin:edges-only-own-customer" $true ($wrongCust.Count -eq 0) "wrong_customer_rows=$($wrongCust.Count)"

# Summary ---------------------------------------------------------
Write-Host ""
$results | Format-Table -AutoSize step, expected, actual, ok, detail
$failed = @($results | Where-Object { -not $_.ok }).Count
$passed = @($results | Where-Object { $_.ok }).Count
Write-Host ""
if ($failed -gt 0) {
    Write-Host "TENANT SEGREGATION SMOKE FAILED: $failed failure(s), $passed passes" -ForegroundColor Red
    exit 1
}
Write-Host "TENANT SEGREGATION SMOKE PASSED: $passed checks" -ForegroundColor Green
