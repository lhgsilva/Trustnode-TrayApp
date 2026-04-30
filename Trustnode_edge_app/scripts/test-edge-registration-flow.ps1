param(
  [string]$BaseUrl = "http://127.0.0.1:8000",
  [string]$AdminUser = "admin",
  [string]$AdminPass = "admin",
  [string]$TenantId = "default",
  [string]$CustomerId = "cust-default"
)

$ErrorActionPreference = "Stop"

function Invoke-Json {
  param(
    [string]$Method,
    [string]$Url,
    [object]$Body = $null,
    [hashtable]$Headers = @{}
  )
  if ($null -ne $Body) {
    return Invoke-RestMethod -Method $Method -Uri $Url -Headers $Headers -ContentType "application/json" -Body ($Body | ConvertTo-Json -Depth 20)
  }
  return Invoke-RestMethod -Method $Method -Uri $Url -Headers $Headers
}

Write-Host "1) Login as admin..."
$login = Invoke-Json -Method POST -Url "$BaseUrl/api/auth/login" -Body @{ username = $AdminUser; password = $AdminPass }
$token = [string]$login.token
if (-not $token) { throw "No token returned from login." }
$auth = @{ Authorization = "Bearer $token" }

Write-Host "2) Issue activation code..."
$issue = Invoke-Json -Method POST -Url "$BaseUrl/api/control-plane/activation-code/issue?tenant_id=$TenantId" -Headers $auth -Body @{
  customer_id = $CustomerId
  edge_name   = "Test Edge Register Flow"
  ttl_minutes = 30
}
$code = [string]$issue.row.activation_code
if (-not $code) { throw "No activation code returned." }

$edgeId = "edge-test-" + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
Write-Host "3) Register edge-link + local admin bootstrap..."
$reg = Invoke-Json -Method POST -Url "$BaseUrl/api/control-plane/edge-link/register" -Body @{
  activation_code = $code
  edge_id = $edgeId
  edge_name = "Test Edge Register Flow"
  site = "Test Site"
  area = "Test Area"
  equipment = "Test Equipment"
  admin_username = "edge_admin"
  admin_password = "edge_admin"
}
$reg | ConvertTo-Json -Depth 10

Write-Host "4) Login with registered local admin..."
$edgeLogin = Invoke-Json -Method POST -Url "$BaseUrl/api/auth/login" -Body @{ username = "edge_admin"; password = "edge_admin" }
if (-not [string]$edgeLogin.token) { throw "Edge admin login failed." }
Write-Host "Edge admin login OK."

Write-Host "5) Unlink edge..."
$edgeAuth = @{ Authorization = "Bearer $($edgeLogin.token)" }
$unlink = Invoke-Json -Method POST -Url "$BaseUrl/api/control-plane/edge-link/unlink" -Headers $edgeAuth
$unlink | ConvertTo-Json -Depth 10

Write-Host "Done."
