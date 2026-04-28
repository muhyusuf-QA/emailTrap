[CmdletBinding()]
param(
    [switch]$Force
)

. (Join-Path $PSScriptRoot 'Common.ps1')

$paths = Get-MailpitPaths
$result = Install-Mailpit -Paths $paths -Force:$Force

$version = if ([string]::IsNullOrWhiteSpace($result.tag_name)) { 'unknown' } else { $result.tag_name }
Write-Host "Mailpit installed in $($paths.BinDir)"
Write-Host "Version: $version"
