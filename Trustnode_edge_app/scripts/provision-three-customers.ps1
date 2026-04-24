param(
  [string]$BaseUrl = "https://trustnode.lsapps.app",
  [string]$AdminUser = "admin",
  [string]$AdminPass = "admin",
  [string]$CustomerAAdminPass = "ChangeMe-A1!",
  [string]$CustomerBAdminPass = "ChangeMe-B1!",
  [string]$CustomerCAdminPass = "ChangeMe-C1!"
)

$ErrorActionPreference = "Stop"

function Login-Token {
  param([string]$Url, [string]$User, [string]$Pass)
  $body = @{ username = $User; password = $Pass } | ConvertTo-Json
  $res = Invoke-RestMethod "$Url/api/auth/login" -Method Post -ContentType "application/json" -Body $body
  return $res.token
}

function Provision-Bundle {
  param(
    [string]$Url,
    [hashtable]$Headers,
    [string]$TenantId,
    [string]$TenantName,
    [string]$Domain,
    [string]$CustomerId,
    [string]$CompanyName,
    [string]$Email,
    [string]$User,
    [string]$Pass
  )
  $payload = @{
    tenant_id = $TenantId
    tenant_name = $TenantName
    primary_domain = $Domain
    timezone = "Europe/Dublin"
    customer_id = $CustomerId
    company_name = $CompanyName
    contact_email = $Email
    admin_username = $User
    admin_password = $Pass
    license_id = "lic-$TenantId"
    plan_code = "standard"
    max_edges = 10
    max_users = 50
  } | ConvertTo-Json -Depth 6

  Invoke-RestMethod "$Url/api/control-plane/provision/customer-bundle" `
    -Method Post `
    -ContentType "application/json" `
    -Headers $Headers `
    -Body $payload | Out-Null
}

$token = Login-Token -Url $BaseUrl -User $AdminUser -Pass $AdminPass
$headers = @{ Authorization = "Bearer $token" }

Provision-Bundle -Url $BaseUrl -Headers $headers `
  -TenantId "customer_a" `
  -TenantName "Customer A" `
  -Domain "customer-a-trustnode.lsapps.app" `
  -CustomerId "cust-a" `
  -CompanyName "Customer A" `
  -Email "admin-a@customer.local" `
  -User "admin_a" `
  -Pass $CustomerAAdminPass

Provision-Bundle -Url $BaseUrl -Headers $headers `
  -TenantId "customer_b" `
  -TenantName "Customer B" `
  -Domain "customer-b-trustnode.lsapps.app" `
  -CustomerId "cust-b" `
  -CompanyName "Customer B" `
  -Email "admin-b@customer.local" `
  -User "admin_b" `
  -Pass $CustomerBAdminPass

Provision-Bundle -Url $BaseUrl -Headers $headers `
  -TenantId "customer_c" `
  -TenantName "Customer C" `
  -Domain "customer-c-trustnode.lsapps.app" `
  -CustomerId "cust-c" `
  -CompanyName "Customer C" `
  -Email "admin-c@customer.local" `
  -User "admin_c" `
  -Pass $CustomerCAdminPass

Write-Host "Provisioned customer_a, customer_b, customer_c on $BaseUrl" -ForegroundColor Green
