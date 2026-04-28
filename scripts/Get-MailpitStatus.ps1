[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot 'Common.ps1')

$paths = Get-MailpitPaths
$settings = Get-MailpitSettings -SettingsPath $paths.SettingsFile
$process = Get-MailpitProcess -Paths $paths
$installedVersion = if (Test-Path -LiteralPath $paths.VersionFile) {
    (Get-Content -LiteralPath $paths.VersionFile -Raw).Trim()
} else {
    'not installed'
}

if ($process) {
    Write-Host 'Status : running'
    Write-Host "PID    : $($process.Id)"
} else {
    Write-Host 'Status : stopped'
    Write-Host 'PID    : -'
}

Write-Host "Version: $installedVersion"
Write-Host "UI     : $(Get-MailpitUiUrl -BindAddress $settings.listen)"
Write-Host "SMTP   : $(Get-MailpitSmtpEndpoint -BindAddress $settings.smtp)"
Write-Host "Logs   : $($paths.LogDir)"
