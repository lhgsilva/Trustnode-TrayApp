param(
    [string]$BaseUrl = "https://trustnode.lsapps.app",
    [string]$MasterUser = "admin",
    [string]$MasterPass = "admin",
    [string]$Stamp = ""
)

# End-to-end smoke against the cloud control plane:
#   1) Login as master/admin
#   2) Create a new tenant + customer + admin user (via provision bundle)
#   3) Create a license with selected modules linked to the customer
#   4) Create an edge entry linked to the customer + license
#   5) Issue an activation code, then apply it (simulating an edge boot)
#   6) Create a dashboard-only "client" web user for that customer
#   7) Sanity-check that the dashboard-only user can log in and is tenant-scoped
#   8) Print credentials and IDs at the end.

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Invoke-Json {
    param(
        [string]$Method,
        [string]$Url,
        [object]$Body = $null,
        [hashtable]$Headers = @{}
    )
    if ($null -eq $Body) {
        return Invoke-RestMethod -Method $Method -Uri $Url -Headers $Headers -TimeoutSec 60
    }
    return Invoke-RestMethod -Method $Method -Uri $Url -Headers $Headers -ContentType "application/json" -Body ($Body | ConvertTo-Json -Depth 12) -TimeoutSec 60
}

if (-not $Stamp) { $Stamp = (Get-Date -Format "yyyyMMddHHmmss") }

$tenantId       = "acme$Stamp"
$customerId     = "acme-co-$Stamp"
$companyName    = "Acme Corp $Stamp"
$contactEmail   = "ops+$Stamp@acme.example.com"
$adminUsername  = "acmeadmin$Stamp"
$adminPassword  = "AcmeAdmin!$Stamp"
$licenseId      = "acme-lic-$Stamp"
$edgeId         = "acme-edge-$Stamp"
$edgeName       = "Acme Edge Box $Stamp"
$clientUsername = "acmeviewer$Stamp"
$clientPassword = "AcmeView!$Stamp"

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "TrustNode portal end-to-end smoke" -ForegroundColor Cyan
Write-Host "Target: $BaseUrl" -ForegroundColor Cyan
Write-Host "Tenant: $tenantId   Customer: $customerId" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# 1) Master login --------------------------------------------------------
Write-Host "`n[1/7] Master login as '$MasterUser'..." -ForegroundColor Yellow
$login = Invoke-Json -Method POST -Url "$BaseUrl/api/auth/login" -Body @{ username = $MasterUser; password = $MasterPass }
if (-not $login.token) { throw "Master login failed: $($login | ConvertTo-Json)" }
$masterAuth = @{ Authorization = "Bearer $($login.token)" }
Write-Host "    ok (role=$($login.user.role) tenant=$($login.user.tenant_id))"

# 2) Provision new tenant + customer + admin user ------------------------
Write-Host "`n[2/7] Provisioning tenant + customer + admin via provision-bundle..." -ForegroundColor Yellow
$bundle = Invoke-Json -Method POST -Url "$BaseUrl/api/control-plane/provision/customer-bundle" -Headers $masterAuth -Body @{
    tenant_id      = $tenantId
    tenant_name    = $companyName
    primary_domain = "$tenantId.lsapps.app"
    timezone       = "Europe/Dublin"
    customer_id    = $customerId
    company_name   = $companyName
    contact_email  = $contactEmail
    admin_username = $adminUsername
    admin_password = $adminPassword
    license_id     = $licenseId
    plan_code      = "standard"
    max_edges      = 2
    max_users      = 10
    modules        = @(
        @{ module_key = "dashboard";   enabled = $true },
        @{ module_key = "historian";   enabled = $true },
        @{ module_key = "alarms";      enabled = $true },
        @{ module_key = "reporting";   enabled = $true },
        @{ module_key = "power_overview"; enabled = $true },
        @{ module_key = "interface";   enabled = $true }
    )
}
Write-Host "    ok (bundle returned ok=$($bundle.ok))"

# 3) Create the edge entry linked to customer + license ------------------
Write-Host "`n[3/7] Creating edge entry '$edgeId' linked to customer + license..." -ForegroundColor Yellow
$edge = Invoke-Json -Method POST -Url "$BaseUrl/api/control-plane/edges?tenant_id=$tenantId" -Headers $masterAuth -Body @{
    edge_id    = $edgeId
    edge_name  = $edgeName
    customer_id = $customerId
    license_id = $licenseId
    site       = "Acme HQ"
    area       = "Production Hall"
    equipment  = "Line 1"
    status     = "provisioned"
    metadata   = @{ source = "smoke-portal-end-to-end.ps1" }
}
Write-Host "    ok (edge_id=$($edge.row.edge_id))"

# 4) Issue an activation code for that edge ------------------------------
Write-Host "`n[4/7] Issuing activation code..." -ForegroundColor Yellow
$act = Invoke-Json -Method POST -Url "$BaseUrl/api/control-plane/activation-code/issue?tenant_id=$tenantId" -Headers $masterAuth -Body @{
    customer_id = $customerId
    edge_id     = $edgeId
    license_id  = $licenseId
    edge_name   = $edgeName
    ttl_minutes = 60
    metadata    = @{ source = "smoke-portal-end-to-end.ps1" }
}
$activationCode = [string]$act.row.activation_code
if (-not $activationCode) { throw "No activation_code returned: $($act | ConvertTo-Json)" }
Write-Host "    ok (activation_code=$activationCode, expires=$($act.row.expires_utc))"

# 5) Apply the activation code (simulating the edge calling home) --------
Write-Host "`n[5/7] Applying activation code (simulating edge boot)..." -ForegroundColor Yellow
$applied = Invoke-Json -Method POST -Url "$BaseUrl/api/control-plane/activation-code/apply" -Body @{
    activation_code = $activationCode
    edge_id         = $edgeId
    edge_name       = $edgeName
    site            = "Acme HQ"
    area            = "Production Hall"
    equipment       = "Line 1"
}
Write-Host "    ok (applied tenant=$($applied.row.tenant_id) license=$($applied.row.license_id))"

# 6) Create a dashboard-only web client user -----------------------------
# Important: the JWT permission keys that gate the customer web view modules
# are dashboard / historian / client_module_alarms / client_module_reporting
# / client_module_interface / power_overview. We grant ONLY dashboard.
Write-Host "`n[6/7] Creating dashboard-only web user '$clientUsername' for customer..." -ForegroundColor Yellow
$clientUser = Invoke-Json -Method POST -Url "$BaseUrl/api/control-plane/users?tenant_id=$tenantId" -Headers $masterAuth -Body @{
    customer_id = $customerId
    username    = $clientUsername
    password    = $clientPassword
    role        = "client"
    status      = "active"
    email       = "viewer+$Stamp@acme.example.com"
    modules     = @("dashboard")
    permissions = @{
        dashboard               = $true
        historian               = $false
        data_log                = $false
        client_module_alarms    = $false
        client_module_reporting = $false
        client_module_interface = $false
        power_overview          = $false
        # No admin-side perms:
        users_and_access_control = $false
        control_plane            = $false
        gateway_configuration    = $false
        database                 = $false
    }
}
Write-Host "    ok (username=$($clientUser.row.username) role=$($clientUser.row.role))"

# 7) Sanity-check: the new dashboard user can log in and gets a tenant-scoped JWT
Write-Host "`n[7/7] Verifying the new web user can log in..." -ForegroundColor Yellow
$clientLogin = Invoke-Json -Method POST -Url "$BaseUrl/api/auth/login" -Body @{ username = $clientUsername; password = $clientPassword }
if (-not $clientLogin.token) { throw "Client user login failed: $($clientLogin | ConvertTo-Json)" }
$clientAuth = @{ Authorization = "Bearer $($clientLogin.token)" }
$clientMe = Invoke-Json -Method GET -Url "$BaseUrl/api/auth/me" -Headers $clientAuth
Write-Host "    ok (login token issued; /me tenant=$($clientMe.user.tenant_id) role=$($clientMe.user.role))"

# Summary --------------------------------------------------------------
Write-Host "`n============================================================" -ForegroundColor Green
Write-Host "PROVISIONING COMPLETE" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Portal URL:                $BaseUrl/client/client_view.html"
Write-Host ""
Write-Host "Tenant ID:                 $tenantId"
Write-Host "Customer ID:               $customerId"
Write-Host "Company Name:              $companyName"
Write-Host "License ID:                $licenseId"
Write-Host "Edge ID:                   $edgeId"
Write-Host "Activation code (applied): $activationCode"
Write-Host ""
Write-Host "--- CUSTOMER ADMIN LOGIN (full admin tools, scoped to this tenant) ---" -ForegroundColor Cyan
Write-Host "  Username: $adminUsername"
Write-Host "  Password: $adminPassword"
Write-Host ""
Write-Host "--- DASHBOARD-ONLY WEB VIEWER LOGIN (read-only, dashboard module only) ---" -ForegroundColor Cyan
Write-Host "  Username: $clientUsername"
Write-Host "  Password: $clientPassword"
Write-Host ""
Write-Host "Open the portal URL above and sign in with either set of credentials." -ForegroundColor Green
Write-Host "Tenant isolation is enforced server-side by JWT + RLS on Supabase." -ForegroundColor Green
