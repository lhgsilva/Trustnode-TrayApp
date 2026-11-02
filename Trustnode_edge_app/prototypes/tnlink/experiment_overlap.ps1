# TN-Link: can we reach a plant network that collides with the client's own?
#
# 2026-10-28. The operator's case, reproduced on the development machine:
#
#     client WiFi      192.168.0.161/24   default gateway 192.168.0.1
#     plant (server)   192.168.0.0/24     PLC at 192.168.0.1
#
# The remote PLC address is identical to the address this PC sends ALL its
# internet traffic to. TN-Link refuses the subnet today and tells the
# operator to move a machine off the range - which is the one thing they
# cannot do, because it is their home router.
#
# Two designs can work without touching the router. This script MEASURES
# both on the machine it runs on, changes nothing permanently, and prints
# which are available. Run it elevated (route and NAT changes need it);
# every change is made in the ACTIVE store only and removed again, so a
# reboot cannot inherit anything from this test.
#
#     powershell -ExecutionPolicy Bypass -File experiment_overlap.ps1
#
# A: HOST ROUTE (/32)
#    One device, its REAL address, over the tunnel. Longest-prefix match
#    should beat the on-link /24. This is what lets RSLinx keep the real
#    PLC address - which matters, because the address is written into the
#    PLC project.
#
# B: VIRTUAL SUBNET (1:1 NAT)
#    A free range the client routes into the tunnel, translated to the real
#    plant range at the server. Nothing on the client collides, so the
#    internet is untouched - but the device answers on a DIFFERENT address,
#    so the PLC project would have to use it.

$ErrorActionPreference = 'Continue'

function Say($t) { Write-Host $t }
function Head($t) { Write-Host ""; Write-Host "=== $t" }

$elevated = ([Security.Principal.WindowsPrincipal] `
  [Security.Principal.WindowsIdentity]::GetCurrent()
  ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)

Head "this machine"
$gw = (Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
       Sort-Object RouteMetric | Select-Object -First 1)
Say ("  default gateway : {0} via {1}" -f $gw.NextHop, $gw.InterfaceAlias)
Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
  ForEach-Object { Say ("  {0,-16}/{1,-3} {2}" -f $_.IPAddress, $_.PrefixLength, $_.InterfaceAlias) }
Say ("  elevated        : {0}" -f $elevated)
if (-not $elevated) {
  Say "  Route and NAT tests need administrator - re-run from an elevated prompt."
  exit 2
}

# A spare adapter to aim test routes at. Loopback is index 1 and always
# present; nothing is actually sent, we only ask Windows what it WOULD do.
$testIf = (Get-NetIPInterface -AddressFamily IPv4 |
           Where-Object { $_.InterfaceAlias -notlike 'Loopback*' } |
           Sort-Object InterfaceMetric | Select-Object -Last 1).InterfaceIndex

# ---------------------------------------------------------------- A
Head "A. does a /32 host route beat the on-link /24?"
# Pick an address on the colliding subnet that nothing is using.
$probe = '192.168.0.222'
$before = (Find-NetRoute -RemoteIPAddress $probe -ErrorAction SilentlyContinue |
           Select-Object -First 1)
Say ("  before : {0} -> {1}" -f $probe, $before.InterfaceAlias)
$added = $false
try {
  New-NetRoute -DestinationPrefix "$probe/32" -InterfaceIndex $testIf -NextHop 0.0.0.0 `
    -PolicyStore ActiveStore -Confirm:$false -ErrorAction Stop | Out-Null
  $added = $true
} catch { Say ("  could not add the test route: {0}" -f $_.Exception.Message) }
if ($added) {
  $after = (Find-NetRoute -RemoteIPAddress $probe -ErrorAction SilentlyContinue |
            Select-Object -First 1)
  Say ("  after  : {0} -> {1}" -f $probe, $after.InterfaceAlias)
  $wins = ($after.InterfaceIndex -eq $testIf)
  Say ("  VERDICT: a host route {0} the on-link subnet" -f $(if ($wins) {"OVERRIDES"} else {"does NOT override"}))

  # The internet must be untouched while that route exists.
  $net = (Find-NetRoute -RemoteIPAddress '8.8.8.8' -ErrorAction SilentlyContinue |
          Select-Object -First 1)
  Say ("  internet still via : {0} ({1})" -f $net.InterfaceAlias, $net.NextHop)

  Remove-NetRoute -DestinationPrefix "$probe/32" -InterfaceIndex $testIf `
    -PolicyStore ActiveStore -Confirm:$false -ErrorAction SilentlyContinue
  Say "  cleaned up"
}

Head "A2. the exception that matters"
Say ("  The PLC in this case is {0}, which is ALSO this PC's default" -f $gw.NextHop)
Say "  gateway. A /32 for it would send the gateway's own address down the"
Say "  tunnel and take the internet with it. Test that claim:"
$probe2 = $gw.NextHop
$cur = (Find-NetRoute -RemoteIPAddress $probe2 -ErrorAction SilentlyContinue | Select-Object -First 1)
Say ("  {0} currently resolves via {1}" -f $probe2, $cur.InterfaceAlias)
Say "  (not overridden here on purpose - breaking the gateway is the point)"

# ---------------------------------------------------------------- B
Head "B. is a virtual subnet (1:1 NAT) available on this Windows?"
$natName = 'TNLinkExperiment'
$virt = '10.88.77.0/24'
Remove-NetNat -Name $natName -Confirm:$false -ErrorAction SilentlyContinue
try {
  New-NetNat -Name $natName -InternalIPInterfaceAddressPrefix $virt -ErrorAction Stop | Out-Null
  Say "  New-NetNat        : AVAILABLE"
  $n = Get-NetNat -Name $natName -ErrorAction SilentlyContinue
  Say ("  created           : {0} -> {1}" -f $n.Name, $n.InternalIPInterfaceAddressPrefix)
  Remove-NetNat -Name $natName -Confirm:$false -ErrorAction SilentlyContinue
  Say "  cleaned up"
} catch {
  Say ("  New-NetNat        : NOT available - {0}" -f $_.Exception.Message)
  Say "  (WinNAT is absent on some Home editions; the tunnel would have to"
  Say "   translate in userspace instead, which TN-Link can do itself.)"
}

Head "what this means"
Say "  * A single device by its REAL address works whenever that address is"
Say "    not one this PC already needs. That covers most PLCs."
Say "  * When the PLC's address IS the client's gateway, no routing trick"
Say "    can give both. The device has to be reached on a virtual address,"
Say "    or the client must not hold that address locally."
Say "  * A virtual subnet never collides, so it always works - at the cost"
Say "    of the device answering on a different address."
