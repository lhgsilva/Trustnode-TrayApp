$ErrorActionPreference = "SilentlyContinue"
Write-Host "=== TrustNode backend process snapshot ==="
$procs = Get-Process -Name "trustnode-service","TrustNode","TrustNode-0.1.0-portable" -ErrorAction SilentlyContinue
foreach ($p in $procs) {
    $hCount = (Get-Process -Id $p.Id).HandleCount
    $wsMB = [math]::Round($p.WorkingSet64 / 1MB, 1)
    $cpu = [math]::Round($p.CPU, 1)
    Write-Host ("{0,-32} pid={1,-6} threads={2,-3} handles={3,-5} rss={4,7}MB cpu={5}s" -f $p.ProcessName,$p.Id,$p.Threads.Count,$hCount,$wsMB,$cpu)
}
Write-Host ""
Write-Host "=== port 8000 owner ==="
$conn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($conn) {
    Write-Host "  pid=$($conn.OwningProcess) listening on $($conn.LocalAddress):$($conn.LocalPort)"
}
Write-Host ""
Write-Host "=== last 12 wedge-watchdog lines ==="
Get-Content "$env:APPDATA\trustnode-edge-desktop\backend.log" -Tail 500 |
    Select-String -Pattern "wedge-watchdog|Backend exited" |
    Select-Object -Last 12
