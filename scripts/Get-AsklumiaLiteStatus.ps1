[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot 'AsklumiaLite.Common.ps1')

$paths = Get-AsklumiaLitePaths
$settings = Get-AsklumiaLiteSettings -SettingsPath $paths.SettingsFile
$process = Get-AsklumiaLiteProcess -Paths $paths -Settings $settings

if ($process) {
    Write-Host 'Status : running'
    Write-Host "PID    : $($process.ProcessId)"
    Write-Host "Source : $($process.Source)"
} else {
    Write-Host 'Status : stopped'
    Write-Host 'PID    : -'
}

Write-Host "API    : http://$(Get-MailpitSmtpEndpoint -BindAddress $settings.apiListen)"
Write-Host "AUTH   : http://$(Get-MailpitSmtpEndpoint -BindAddress $settings.authListen)"
Write-Host "Logs   : $($paths.LogDir)"
