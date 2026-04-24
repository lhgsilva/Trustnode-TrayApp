Param(
  [string]$BaseUrl = "http://127.0.0.1:8000",
  [string]$Username = "admin",
  [string]$Password = "admin"
)

$ErrorActionPreference = "Stop"

function Step($name, [scriptblock]$action) {
  Write-Host "==> $name" -ForegroundColor Cyan
  & $action
}

function Invoke-JsonGet($url, $headers = @{}) {
  return Invoke-RestMethod -Method Get -Uri $url -Headers $headers
}

function Invoke-JsonPost($url, $body, $headers = @{}) {
  return Invoke-RestMethod -Method Post -Uri $url -Headers $headers -ContentType "application/json" -Body ($body | ConvertTo-Json -Depth 8)
}

Step "Health" {
  $health = Invoke-JsonGet "$BaseUrl/api/health"
  if (-not $health.status) { throw "Health status missing" }
  Write-Host ("health: {0}" -f $health.status) -ForegroundColor Green
}

$token = $null

Step "Login" {
  $login = Invoke-JsonPost "$BaseUrl/api/auth/login" @{ username = $Username; password = $Password }
  if (-not $login.token) { throw "Token not returned" }
  $script:token = $login.token
  Write-Host ("login ok: {0}" -f $login.user.username) -ForegroundColor Green
}

$authHeaders = @{ Authorization = "Bearer $token" }

Step "Runtime Context" {
  $ctx = Invoke-JsonGet "$BaseUrl/api/control-plane/runtime-context" $authHeaders
  if (-not $ctx.tenant_id) { throw "tenant_id missing in runtime-context" }
  Write-Host ("runtime-context tenant: {0}" -f $ctx.tenant_id) -ForegroundColor Green
}

Step "Edge Bootstrap Status" {
  $status = Invoke-JsonGet "$BaseUrl/api/control-plane/edge-bootstrap-status" $authHeaders
  if ($null -eq $status.ingest_ready) { throw "ingest_ready missing" }
  Write-Host ("ingest_ready={0} outbox_depth={1}" -f $status.ingest_ready, $status.outbox_depth) -ForegroundColor Green
}

Step "Control Plane Summary" {
  $sum = Invoke-JsonGet "$BaseUrl/api/control-plane/summary" $authHeaders
  if (-not $sum.counts) { throw "summary counts missing" }
  Write-Host ("edges={0} users={1} licenses={2}" -f $sum.counts.edges, $sum.counts.users, $sum.counts.licenses) -ForegroundColor Green
}

Write-Host ""
Write-Host "Seven-phase smoke checks passed." -ForegroundColor Green
