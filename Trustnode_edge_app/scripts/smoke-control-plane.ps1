param(
    [string]$BaseUrl = "https://trustnode.lsapps.app",
    [string]$Username = "admin",
    [string]$Password = "admin",
    [string]$TenantId = "default"
)

$ErrorActionPreference = "Stop"

function Invoke-Json {
    param(
        [string]$Method,
        [string]$Url,
        [object]$Body = $null,
        [hashtable]$Headers = @{}
    )
    if ($null -eq $Body) {
        return Invoke-RestMethod -Method $Method -Uri $Url -Headers $Headers
    }
    return Invoke-RestMethod -Method $Method -Uri $Url -Headers $Headers -ContentType "application/json" -Body ($Body | ConvertTo-Json -Depth 12)
}

Write-Host "Control-plane smoke test against $BaseUrl" -ForegroundColor Cyan

$login = Invoke-Json -Method "POST" -Url "$BaseUrl/api/auth/login" -Body @{ username = $Username; password = $Password }
$token = [string]$login.token
if (-not $token) { throw "Login failed: missing token." }
$auth = @{ Authorization = "Bearer $token" }

$stamp = Get-Date -Format "yyyyMMddHHmmss"
$customerId = "smoke-customer-$stamp"
$edgeId = "smoke-edge-$stamp"
$licenseId = "smoke-lic-$stamp"

$results = [System.Collections.Generic.List[object]]::new()
function Add-Result([string]$step, [bool]$ok, [string]$detail) {
    $results.Add([pscustomobject]@{ step = $step; ok = $ok; detail = $detail })
}

try {
    $mods = Invoke-Json -Method "GET" -Url "$BaseUrl/api/control-plane/modules" -Headers $auth
    Add-Result "modules" $true ("count=" + (@($mods.modules).Count))

    $ctx = Invoke-Json -Method "GET" -Url "$BaseUrl/api/control-plane/runtime-context" -Headers $auth
    Add-Result "runtime-context" $true ("tenant=" + [string]$ctx.tenant_id)

    $summary = Invoke-Json -Method "GET" -Url "$BaseUrl/api/control-plane/summary?tenant_id=$TenantId" -Headers $auth
    Add-Result "summary" $true ("ok=" + [string]$summary.ok)

    $customer = Invoke-Json -Method "POST" -Url "$BaseUrl/api/control-plane/customers?tenant_id=$TenantId" -Headers $auth -Body @{
        customer_id = $customerId
        company_name = "Smoke Customer $stamp"
        contact_email = "smoke+$stamp@trustnode.local"
        status = "active"
        metadata = @{ source = "smoke-control-plane.ps1" }
    }
    Add-Result "upsert-customer" $true ("id=" + [string]$customer.row.customer_id)

    $edge = Invoke-Json -Method "POST" -Url "$BaseUrl/api/control-plane/edges?tenant_id=$TenantId" -Headers $auth -Body @{
        edge_id = $edgeId
        edge_name = "Smoke Edge $stamp"
        customer_id = $customerId
        site = "SmokeSite"
        area = "SmokeArea"
        equipment = "SmokeEq"
        status = "active"
        metadata = @{ source = "smoke-control-plane.ps1" }
    }
    Add-Result "upsert-edge" $true ("id=" + [string]$edge.row.edge_id)

    $hb = Invoke-Json -Method "POST" -Url "$BaseUrl/api/control-plane/edges/heartbeat?tenant_id=$TenantId&edge_id=$edgeId" -Headers $auth -Body @{
        status = "active"
        note = "heartbeat from smoke script"
    }
    Add-Result "edge-heartbeat" $true ("last_heartbeat_utc=" + [string]$hb.row.last_heartbeat_utc)

    $license = Invoke-Json -Method "POST" -Url "$BaseUrl/api/control-plane/licenses?tenant_id=$TenantId" -Headers $auth -Body @{
        license_id = $licenseId
        customer_id = $customerId
        plan_code = "standard"
        status = "active"
        start_utc = (Get-Date).ToUniversalTime().ToString("s") + "Z"
        end_utc = (Get-Date).ToUniversalTime().AddDays(30).ToString("s") + "Z"
        max_edges = 2
        max_users = 5
        metadata = @{ source = "smoke-control-plane.ps1" }
    }
    Add-Result "upsert-license" $true ("id=" + [string]$license.row.license_id)

    $modsSet = Invoke-Json -Method "PUT" -Url "$BaseUrl/api/control-plane/licenses/$licenseId/modules" -Headers $auth -Body @{
        modules = @(
            @{ module_key = "dashboard"; enabled = $true },
            @{ module_key = "historian"; enabled = $true },
            @{ module_key = "reporting"; enabled = $true }
        )
    }
    Add-Result "set-license-modules" $true ("updated=" + [string]$modsSet.updated)

    $activation = Invoke-Json -Method "POST" -Url "$BaseUrl/api/control-plane/activation-code/issue?tenant_id=$TenantId" -Headers $auth -Body @{
        customer_id = $customerId
        edge_name = "Smoke Activation Edge"
        ttl_minutes = 20
        metadata = @{ source = "smoke-control-plane.ps1" }
    }
    $code = [string]$activation.row.activation_code
    Add-Result "issue-activation-code" ([bool]$code) ("code=" + $code)

    if ($code) {
        $applied = Invoke-Json -Method "POST" -Url "$BaseUrl/api/control-plane/activation-code/apply" -Headers $auth -Body @{
            activation_code = $code
            edge_id = "$edgeId-applied"
            edge_name = "Smoke Applied Edge"
            site = "SmokeSite"
            area = "SmokeArea"
            equipment = "SmokeEq"
        }
        Add-Result "apply-activation-code" $true ("edge_id=" + [string]$applied.row.edge_id)
    }

    $users = Invoke-Json -Method "GET" -Url "$BaseUrl/api/control-plane/users?tenant_id=$TenantId" -Headers $auth
    Add-Result "list-users" $true ("count=" + (@($users.rows).Count))
}
catch {
    Add-Result "fatal" $false $_.Exception.Message
}

$results | Format-Table -AutoSize
$failed = @($results | Where-Object { -not $_.ok }).Count
if ($failed -gt 0) {
    Write-Host "Control-plane smoke FAILED ($failed step(s))." -ForegroundColor Red
    exit 1
}
Write-Host "Control-plane smoke PASSED." -ForegroundColor Green
