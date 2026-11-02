# Create a self-signed code-signing certificate for TrustNode.
#
# 2026-10-30, asked for: no publisher warning when installing, and no cost.
# Those two cannot both be fully true - a certificate Windows trusts out of
# the box comes from a CA and is paid for. This is the honest middle:
#
#   SIGNED WITH THIS, ON A MACHINE THAT TRUSTS IT
#     - no "unknown publisher" on the UAC prompt
#     - the installer and the app show "TrustNode AB" as the publisher
#     - the firewall rules the installer creates stop re-prompting
#
#   ON ANY OTHER MACHINE
#     - no change at all. Windows has never heard of this certificate.
#     - SmartScreen still warns, Smart App Control still blocks.
#
# So it is worth doing for customers whose PCs you set up, and worth nothing
# for a download link on a web page. For that you need a real certificate -
# Microsoft Trusted Signing is about $10/month and is the cheapest route.
#
#     powershell -ExecutionPolicy Bypass -File scripts\make-selfsigned-cert.ps1
#
# Produces, in scripts\certs\ (which must NOT be committed):
#   trustnode-codesign.pfx   the signing key - keep it secret
#   trustnode-codesign.cer   the public half - this is what customers trust
#
# Then build with:
#   $env:TRUSTNODE_CSC_LINK     = "<full path>\trustnode-codesign.pfx"
#   $env:TRUSTNODE_CSC_PASSWORD = "<the password you chose>"
#   powershell -ExecutionPolicy Bypass -File scripts\build-release.ps1

$ErrorActionPreference = "Stop"

$subject = "CN=TrustNode AB, O=TrustNode AB, C=SE"
$outDir = Join-Path $PSScriptRoot "certs"
New-Item -ItemType Directory -Force $outDir | Out-Null

$pfxPath = Join-Path $outDir "trustnode-codesign.pfx"
$cerPath = Join-Path $outDir "trustnode-codesign.cer"

if (Test-Path $pfxPath) {
    Write-Host "A certificate already exists at $pfxPath"
    Write-Host "Delete it first if you really want a new one - replacing it"
    Write-Host "means every machine that trusts the old one must be updated."
    exit 1
}

# A password is required: a .pfx holds the private key, and an unprotected
# private key on a build machine is the whole signature given away.
$pwd = Read-Host -AsSecureString "Choose a password for the signing key"
if (-not $pwd -or $pwd.Length -eq 0) { throw "A password is required." }

Write-Host "Creating a code-signing certificate for $subject ..."
# 3 years: long enough not to be a chore, short enough that a leaked key
# stops being useful. Signatures stay valid past expiry only if timestamped,
# which build-release.ps1 does.
$cert = New-SelfSignedCertificate `
    -Subject $subject `
    -Type CodeSigningCert `
    -KeyExportPolicy Exportable `
    -KeyLength 3072 `
    -KeyAlgorithm RSA `
    -HashAlgorithm SHA256 `
    -CertStoreLocation "Cert:\CurrentUser\My" `
    -NotAfter (Get-Date).AddYears(3)

Export-PfxCertificate -Cert $cert -FilePath $pfxPath -Password $pwd | Out-Null
Export-Certificate  -Cert $cert -FilePath $cerPath -Type CERT | Out-Null

# The build machine does not need it in a store afterwards - the .pfx is the
# input. Leaving it behind just means one more copy of a private key.
Remove-Item ("Cert:\CurrentUser\My\" + $cert.Thumbprint) -Force

Write-Host ""
Write-Host "Done."
Write-Host "  signing key (SECRET) : $pfxPath"
Write-Host "  public cert (SHARE)  : $cerPath"
Write-Host "  thumbprint           : $($cert.Thumbprint)"
Write-Host ""
Write-Host "Next:"
Write-Host "  1. Build with TRUSTNODE_CSC_LINK / TRUSTNODE_CSC_PASSWORD set."
Write-Host "  2. On each customer PC, run scripts\trust-trustnode-cert.ps1"
Write-Host "     as administrator, with trustnode-codesign.cer beside it."
Write-Host ""
Write-Host "Keep the .pfx off the repository and off shared drives. Anyone"
Write-Host "holding it can sign software as TrustNode AB."
