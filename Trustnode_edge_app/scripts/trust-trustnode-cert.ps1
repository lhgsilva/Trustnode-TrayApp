# Trust the TrustNode signing certificate on THIS machine.
#
# Run once per customer PC, as administrator, with trustnode-codesign.cer in
# the same folder (or pass -CerPath). After this, TrustNode installers signed
# with that certificate show "TrustNode AB" as the publisher instead of
# "Unknown publisher".
#
#     powershell -ExecutionPolicy Bypass -File trust-trustnode-cert.ps1
#
# WHAT THIS DOES, PLAINLY, because it is a security decision and the person
# running it deserves to know:
#
#   It tells Windows that software signed by TrustNode AB is from a known
#   publisher. It affects THIS machine only. It does not disable SmartScreen,
#   Smart App Control, Defender or any other protection, and it does not make
#   Windows trust anything else.
#
#   The trade: anyone who obtains the TrustNode signing key could sign
#   software that this machine would then treat as a known publisher. That is
#   the same trade every corporate code-signing certificate makes, and it is
#   why the key is kept off shared drives.
#
# On a managed fleet, prefer Group Policy:
#   Computer Configuration > Policies > Windows Settings > Security Settings
#     > Public Key Policies > Trusted Publishers    (import the .cer)
# which does the same thing for every machine without visiting them.

[CmdletBinding()]
param(
    [string]$CerPath = (Join-Path $PSScriptRoot "trustnode-codesign.cer"),
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$admin = ([Security.Principal.WindowsPrincipal] `
          [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $admin) {
    Write-Host "This needs administrator rights (it writes to the machine's"
    Write-Host "certificate stores). Right-click PowerShell > Run as administrator."
    exit 2
}

if (-not (Test-Path $CerPath)) {
    Write-Host "Certificate not found: $CerPath"
    Write-Host "Copy trustnode-codesign.cer next to this script, or pass -CerPath."
    exit 2
}

$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 $CerPath
Write-Host "Certificate : $($cert.Subject)"
Write-Host "Thumbprint  : $($cert.Thumbprint)"
Write-Host "Valid until : $($cert.NotAfter)"
Write-Host ""

# TrustedPublisher is what silences "unknown publisher"; Root is what makes
# the self-signed chain verify at all. A CA-issued certificate would need
# only the first - this needs both precisely because nobody vouches for it
# but us.
$stores = @("TrustedPublisher", "Root")

if ($Remove) {
    foreach ($name in $stores) {
        $store = New-Object System.Security.Cryptography.X509Certificates.X509Store($name, "LocalMachine")
        $store.Open("ReadWrite")
        $found = $store.Certificates | Where-Object { $_.Thumbprint -eq $cert.Thumbprint }
        foreach ($c in $found) { $store.Remove($c) }
        $store.Close()
        Write-Host ("removed from LocalMachine\{0}: {1}" -f $name, (@($found).Count))
    }
    Write-Host "Done. This machine no longer treats TrustNode as a known publisher."
    exit 0
}

foreach ($name in $stores) {
    $store = New-Object System.Security.Cryptography.X509Certificates.X509Store($name, "LocalMachine")
    $store.Open("ReadWrite")
    $already = $store.Certificates | Where-Object { $_.Thumbprint -eq $cert.Thumbprint }
    if ($already) {
        Write-Host ("already present in LocalMachine\{0}" -f $name)
    } else {
        $store.Add($cert)
        Write-Host ("added to LocalMachine\{0}" -f $name)
    }
    $store.Close()
}

Write-Host ""
Write-Host "Done. TrustNode installers signed with this certificate will now"
Write-Host "show TrustNode AB as the publisher on this machine."
Write-Host ""
Write-Host "Note: this does NOT satisfy Smart App Control, which only accepts"
Write-Host "software Microsoft itself recognises. If a machine has Smart App"
Write-Host "Control on, it will still block the installer, and the only fixes"
Write-Host "are a CA-issued certificate or turning that feature off in"
Write-Host "Windows Security - which is the machine owner's decision, not"
Write-Host "something TrustNode will do for them."
Write-Host ""
Write-Host "To undo:  trust-trustnode-cert.ps1 -Remove"
