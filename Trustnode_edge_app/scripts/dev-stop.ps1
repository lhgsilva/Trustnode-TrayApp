<#
.SYNOPSIS
  Stop the local DEV backend (uvicorn) and frontend (Vite) started by dev-run.ps1.
#>
$stopped = 0
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -match 'uvicorn|app\.main' } |
  ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force; $stopped++ } catch {} }
Get-CimInstance Win32_Process -Filter "Name='node.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -match 'vite' } |
  ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force; $stopped++ } catch {} }
Write-Host "Stopped $stopped dev process(es)."
